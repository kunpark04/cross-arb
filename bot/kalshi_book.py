"""bot/kalshi_book.py — Kalshi order-book MERGE: reconstruct a live book from an orderbook_snapshot
plus a stream of orderbook_delta messages. The Kalshi half of the dual-stream monitor (decision 0005).

Why a merge (vs polymarket.us): Kalshi sends the full book ONCE on subscribe, then only incremental
changes. You keep a local copy and apply each delta. Verified live 2026-06-08 (scripts probes):

  • snapshot msg: {market_ticker, market_id, yes_dollars_fp?, no_dollars_fp?}
       yes_dollars_fp / no_dollars_fp = [[price_dollars, qty_fp], ...]  (a side is ABSENT if empty)
  • delta msg:    {market_ticker, side: "yes"|"no", price_dollars, delta_fp (signed)}
  • Kalshi quotes resting BIDS per side; a YES ask is the reciprocal of a NO bid:  yes_ask = 1 - no_bid.
  • seq: ONE monotonic counter PER CONNECTION (single sid; covers snapshots, acks, deltas). A gap means
       a missed message -> the whole subscription is stale -> resubscribe (SeqTracker handles detection).

  python bot/kalshi_book.py          # OFFLINE self-test (no network)
  python bot/kalshi_book.py --live   # connect with the READ-ONLY key, stream real books (READ-ONLY)
"""
import os, sys, json, time, base64
try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass

KALSHI_WS = "wss://api.elections.kalshi.com/trade-api/ws/v2"
KALSHI_WS_PATH = "/trade-api/ws/v2"

# ============================================================================================
# MERGE CORE  (pure; offline self-tested)
# ============================================================================================
class KalshiBook:
    """Local Kalshi book for ONE market. yes/no hold {price($float): qty(float)} of resting bids."""
    def __init__(self, ticker):
        self.ticker = ticker
        self.yes, self.no = {}, {}

    def apply_snapshot(self, msg):
        self.yes = {float(p): float(q) for p, q in (msg.get("yes_dollars_fp") or [])}
        self.no  = {float(p): float(q) for p, q in (msg.get("no_dollars_fp")  or [])}

    def apply_delta(self, msg):
        book = self.yes if msg["side"] == "yes" else self.no
        p = float(msg["price_dollars"])
        book[p] = book.get(p, 0.0) + float(msg["delta_fp"])
        if book[p] <= 1e-9:                      # level emptied -> drop it
            book.pop(p, None)

    # --- views the monitor consumes: YES bid/offer ladders in the shared {px:{value},qty} shape ---
    def yes_bid_ladder(self):                    # YES bids = the yes book, best (highest) first
        return [{"px": {"value": f"{p:.4f}"}, "qty": f"{q:.2f}"}
                for p, q in sorted(self.yes.items(), reverse=True)]

    def yes_offer_ladder(self):                  # YES asks = 1 - NO bid; best (lowest) ask first
        return [{"px": {"value": f"{1.0 - p:.4f}"}, "qty": f"{q:.2f}"}
                for p, q in sorted(self.no.items(), reverse=True)]

    def best(self):
        yb = max(self.yes) if self.yes else None
        nb = max(self.no) if self.no else None
        return yb, (round(1.0 - nb, 4) if nb is not None else None)   # (best YES bid, best YES ask)

    def offer_pairs(self):
        """YES-ask ladder as [(ask_price, qty)] ascending (best/lowest ask first) — for depth walks.
        YES ask = 1 - NO bid, so the highest NO bid is the lowest (best) YES ask."""
        return [(round(1.0 - p, 6), q) for p, q in sorted(self.no.items(), reverse=True)]


class SeqTracker:
    """Connection-level monotonic-seq guard. check(seq) is False on a gap (=> caller must resubscribe)."""
    def __init__(self):
        self.expected = None
    def reset(self):
        self.expected = None
    def check(self, seq):
        ok = self.expected is None or seq == self.expected
        self.expected = seq + 1
        return ok


# ============================================================================================
# SELF-TEST  (no network; mirrors bot/ledger.py + bot/monitor.py)
# ============================================================================================
def _selftest():
    print("kalshi merge self-test")
    b = KalshiBook("T")
    b.apply_snapshot({"yes_dollars_fp": [["0.67", "100.00"], ["0.66", "50.00"]],
                      "no_dollars_fp":  [["0.31", "40.00"]]})
    assert b.best() == (0.67, 0.69), b.best()                 # YES bid 0.67; YES ask = 1 - 0.31
    b.apply_delta({"side": "yes", "price_dollars": "0.68", "delta_fp": "30.00"})
    assert b.best()[0] == 0.68                                # new best YES bid
    b.apply_delta({"side": "yes", "price_dollars": "0.68", "delta_fp": "-30.00"})
    assert b.best()[0] == 0.67                                # level emptied -> back to 0.67
    b.apply_delta({"side": "no", "price_dollars": "0.33", "delta_fp": "10.00"})
    assert b.best()[1] == 0.67                                # better NO bid 0.33 -> YES ask = 0.67
    assert b.yes_bid_ladder()[0]["px"]["value"] == "0.6700"
    assert b.yes_offer_ladder()[0]["px"]["value"] == "0.6700"  # derived from best NO bid 0.33
    print(f"  book best (yes_bid, yes_ask) = {b.best()}; ladders feed MarketTracker as venue 'K'")

    st = SeqTracker()
    assert st.check(1) and st.check(2) and st.check(3)        # contiguous -> OK
    assert not st.check(7)                                    # gap (4 expected) -> resubscribe
    assert st.check(8)                                        # resyncs from the gap
    print("  seq-gap detection OK (contiguous pass, gap flagged)")
    print("OK - snapshot/delta merge, yes-ask-from-no-bid, ladders, seq-gap all correct")


# ============================================================================================
# AUTH + LIVE  (RSA-PSS; read-only key; validates the merge against real deltas)
# ============================================================================================
def kalshi_ws_headers(path=KALSHI_WS_PATH):
    """RSA-PSS-SHA256 headers for the Kalshi WS handshake (signs '{ts}GET{path}'). Read-only key."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    env = {}
    for line in open(os.path.join(os.path.dirname(__file__), "..", "scripts", ".env")):
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            k, v = s.split("=", 1); env[k.strip()] = v.strip()
    pem = env["KALSHI_PRIVATE_KEY_PATH"]
    pem = pem if os.path.isabs(pem) else os.path.join(os.path.dirname(__file__), "..", "scripts", pem)
    priv = serialization.load_pem_private_key(open(pem, "rb").read(), password=None)
    ts = str(int(time.time() * 1000))
    sig = base64.b64encode(priv.sign(f"{ts}GET{path}".encode(),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256())).decode()
    return {"KALSHI-ACCESS-KEY": env["KALSHI_ACCESS_KEY"], "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": sig}

def _live(seconds=18):
    """Subscribe to a few liquid markets and drive real KalshiBooks; print best YES bid/ask + seq gaps."""
    import urllib.request, websocket
    req = urllib.request.Request("https://api.elections.kalshi.com/trade-api/v2/markets?status=open&limit=200",
                                 headers={"User-Agent": "cross-arb/1.0"})
    mkts = json.load(urllib.request.urlopen(req, timeout=20)).get("markets", [])
    mkts.sort(key=lambda m: -(m.get("volume_24h") or m.get("volume") or 0))
    tickers = [m["ticker"] for m in mkts[:8] if m.get("ticker")]
    print(f"live merge test on {len(tickers)} markets")
    ws = websocket.create_connection(KALSHI_WS, timeout=15,
                                     header=[f"{k}: {v}" for k, v in kalshi_ws_headers().items()])
    ws.send(json.dumps({"id": 1, "cmd": "subscribe",
                        "params": {"channels": ["orderbook_delta"], "market_tickers": tickers}}))
    books, st = {}, SeqTracker()
    ws.settimeout(2); end = time.time() + seconds; deltas = 0
    while time.time() < end:
        try: o = json.loads(ws.recv())
        except Exception: continue
        if "seq" in o and not st.check(o["seq"]):
            print(f"  !! SEQ GAP at {o['seq']} -> would resubscribe+reset"); st.reset()
        typ, msg = o.get("type"), o.get("msg", {}); tk = msg.get("market_ticker")
        if typ == "orderbook_snapshot":
            books[tk] = KalshiBook(tk); books[tk].apply_snapshot(msg)
        elif typ == "orderbook_delta" and tk in books:
            books[tk].apply_delta(msg); deltas += 1
            yb, ya = books[tk].best()
            print(f"  delta {tk[-14:]} side={msg['side']} px={msg['price_dollars']} d={msg['delta_fp']:>8}  -> best yes_bid={yb} yes_ask={ya}")
    ws.close()
    live_books = sum(1 for b in books.values() if b.yes or b.no)
    print(f"\nRESULT: merged {len(books)} snapshots, applied {deltas} deltas, {live_books} books with depth. "
          f"{'PASS' if books else 'NO DATA (markets quiet) - rerun'}")

if __name__ == "__main__":
    if "--live" in sys.argv:
        _live()
    else:
        _selftest()
