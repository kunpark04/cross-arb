"""probe_account_readonly.py — answer ONE question with venue data: did THIS account trade, and
what explains a portfolio P&L change? READ-ONLY: signed GETs against /portfolio/* with the
read-only key (decision 0007 — the key cannot place/cancel orders; venue-side ACL). No writes.

Prints: balance, today's + recent fills, resting/recent orders, current positions, recent
settlements. Raw responses -> scripts/_data/account_readonly_<ts>.json (gitignored).
"""
import os, sys, json, time, datetime, urllib.request, urllib.parse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bot"))
from kalshi_book import kalshi_ws_headers  # signs "{ts}GET{path}" with the read-only key

HOST = "https://api.elections.kalshi.com"
DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_data")


def get(path, **params):
    """Signed read-only GET. Signature covers the path WITHOUT the query string."""
    qs = ("?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})) if params else ""
    req = urllib.request.Request(HOST + path + qs, headers={
        **kalshi_ws_headers(path), "Accept": "application/json", "User-Agent": "cross-arb-readonly/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {"_http_error": e.code, "_body": e.read().decode("utf-8", "replace")[:400], "_path": path}


def cents(v):
    return f"${(v or 0) / 100:,.2f}"


def main():
    now = time.time()
    day_ago, week_ago = int(now - 26 * 3600), int(now - 7 * 86400)
    out = {}

    out["balance"] = get("/trade-api/v2/portfolio/balance")
    out["fills_48h"] = get("/trade-api/v2/portfolio/fills", min_ts=day_ago, limit=200)
    out["orders_48h"] = get("/trade-api/v2/portfolio/orders", min_ts=day_ago, limit=200)
    out["positions"] = get("/trade-api/v2/portfolio/positions", limit=200)
    out["settlements_7d"] = get("/trade-api/v2/portfolio/settlements", min_ts=week_ago, limit=200)

    os.makedirs(DATA, exist_ok=True)
    dump = os.path.join(DATA, f"account_readonly_{time.strftime('%Y%m%d-%H%M%S')}.json")
    json.dump(out, open(dump, "w"), indent=1)

    b = out["balance"]
    print(f"balance: {cents(b.get('balance'))}  (payout pending: {cents(b.get('payout'))})"
          if "_http_error" not in b else f"balance: HTTP {b['_http_error']} {b['_body']}")

    fills = (out["fills_48h"] or {}).get("fills") or []
    print(f"\nFILLS last 26h: {len(fills)}")
    for f in fills[:20]:
        print(f"  {f.get('created_time','?')[:19]}  {f.get('ticker'):28} {f.get('action','?'):4} "
              f"{f.get('side','?'):3} x{f.get('count')} @ {f.get('yes_price')}c  taker={f.get('is_taker')}")

    orders = (out["orders_48h"] or {}).get("orders") or []
    print(f"\nORDERS last 26h: {len(orders)}")
    for o in orders[:20]:
        print(f"  {o.get('created_time','?')[:19]}  {o.get('ticker'):28} {o.get('action','?'):4} "
              f"{o.get('side','?'):3} x{o.get('initial_count')} status={o.get('status')}")

    poss = (out["positions"] or {}).get("market_positions") or []
    open_pos = [p for p in poss if p.get("position")]
    print(f"\nOPEN POSITIONS: {len(open_pos)} (of {len(poss)} returned)")
    for p in open_pos[:25]:
        print(f"  {p.get('ticker'):28} pos={p.get('position'):>5}  exposure={cents(p.get('market_exposure'))}  "
              f"realized={cents(p.get('realized_pnl'))}  fees={cents(p.get('fees_paid'))}")

    setts = (out["settlements_7d"] or {}).get("settlements") or []
    print(f"\nSETTLEMENTS last 7d: {len(setts)}")
    for s in setts[:25]:
        print(f"  {s.get('settled_time','?')[:19]}  {s.get('ticker'):28} {s.get('market_result','?'):4} "
              f"yes={s.get('yes_count')} no={s.get('no_count')}  revenue={cents(s.get('revenue'))}")

    print(f"\nraw -> {dump}")


if __name__ == "__main__":
    main()
