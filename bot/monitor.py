"""bot/monitor.py — dual-stream cross-venue edge MONITOR (persistence layer; decisions 0003 + 0005).

READ-ONLY. Streams order books from BOTH venues, recomputes the cross-venue edge on every book delta,
and logs edge STATE-TRANSITIONS (open / close / flip / widen / narrow) to JSONL — the persistence data
that drives the ledger's layer-vs-rotate rule (bot/ledger.py, decision 0004). It does NOT place orders.

  python bot/monitor.py              # runs the OFFLINE self-test of the transition core (no network)
  python bot/monitor.py --live       # GATED: opens live streams (needs creds + the co-listed map)

DEPLOYMENT: intended to run as a long-lived logger on a DigitalOcean droplet. Per the owner's
2026-06-08 instruction, DO NOT deploy this anywhere without consulting them first (decision 0006).

polymarket.us WS protocol — VERIFIED 2026-06-08 via scripts/probe_pmus_ws_auth.py:
  • connect  wss://api.polymarket.us/v1/ws/markets  with Ed25519 headers signing "{ts}GET/v1/ws/markets"
  • subscribe {"subscribe":{"requestId":..,"subscriptionType":"SUBSCRIPTION_TYPE_MARKET_DATA","marketSlugs":[..]}}
  • frame     {"marketData":{"marketSlug":..,"bids":[{"px":{"value":".."},"qty":".."}],"offers":[..],"state":".."}}
  • limit     ≤100 slugs per subscription  →  shard the ~200-market universe across ≥2 subs
Kalshi WS (orderbook_delta) is the second stream — RSA-PSS auth + snapshot/delta merge VALIDATED
(bot/kalshi_book.py); both streams feed the same MarketTracker. See research/kalshi-venue-audit.md.
"""
import os, sys, json, time
sys.path.insert(0, os.path.dirname(__file__))
from ledger import signal, pfee, kfee  # cross-venue edge + fee models (single source of truth)
from kalshi_book import KalshiBook, SeqTracker, kalshi_ws_headers  # Kalshi snapshot/delta merge

# ============================================================================================
# TRANSITION CORE  (pure, fully unit-tested offline — this is the load-bearing logic)
# ============================================================================================
def best_px(levels):
    """Best price off a [{px:{value}, qty}] ladder (already sorted best-first), or None if empty."""
    return float(levels[0]["px"]["value"]) if levels else None

def make_px(p_bids, p_offers, k_bids, k_offers):
    """Build the {p_yb,p_ya,k_yb,k_ya} YES bid/ask quad ledger.signal() expects, from both venues'
    YES-side books. Returns None if any of the four touches is missing (can't price an arb)."""
    p_yb, p_ya = best_px(p_bids), best_px(p_offers)
    k_yb, k_ya = best_px(k_bids), best_px(k_offers)
    if None in (p_yb, p_ya, k_yb, k_ya):
        return None
    return {"p_yb": p_yb, "p_ya": p_ya, "k_yb": k_yb, "k_ya": k_ya}

def edge_state(px):
    """Reduce a price quad to the comparable edge state: arb present? which direction? net edge."""
    s = signal(px)
    return {"arb": s["net_edge"] > 0 and not s.get("no_arb"), "dir": s["dir"], "net": s["net_edge"]}

def classify(prev, new, eps=0.002):
    """The transition between two consecutive edge states. None = no loggable transition.
    OPEN  no-arb→arb · CLOSE arb→no-arb · FLIP direction reversed · WIDEN/NARROW same-dir net change."""
    if new is None:
        return None                      # incomplete book; ignore
    if prev is None or not prev["arb"]:
        return "OPEN" if new["arb"] else None
    if not new["arb"]:
        return "CLOSE"
    if new["dir"] != prev["dir"]:
        return "FLIP"
    d = new["net"] - prev["net"]
    return "WIDEN" if d > eps else "NARROW" if d < -eps else None


class MarketTracker:
    """Holds the latest YES book per venue for ONE co-listed market and emits transitions.
    `evaluate()` classifies the current COMPLETE dual-venue state against the last complete state."""
    def __init__(self, key):
        self.key = key
        self.books = {"P": (None, None), "K": (None, None)}   # venue -> (bids, offers)
        self.state = None

    def set_book(self, venue, bids, offers):
        self.books[venue] = (bids, offers)

    def evaluate(self):
        """Classify the current complete state vs the last; advance state; return (label, state)."""
        (pb, po), (kb, ko) = self.books["P"], self.books["K"]
        px = make_px(pb, po, kb, ko)
        new = edge_state(px) if px else None
        label = classify(self.state, new)
        if new is not None:
            self.state = new
        return label, new

    def update(self, venue, bids, offers):
        """Live single-frame entry: apply one venue's book, then evaluate. NOTE: per-frame, a genuine
        direction reversal surfaces as CLOSE then OPEN across two frames (the legs move one at a time);
        coalescing those into one FLIP within a short window is a live-layer debounce TODO."""
        self.set_book(venue, bids, offers)
        return self.evaluate()


def game_edge(pm_bid, pm_ask, kA_ask, kB_ask):
    """2-outcome cross-venue edge for a GAME (polymarket market YES = team A). Cheapest venue per side:
    back A = min(pm_ask, Kalshi-A ask); back B = min(1 - pm_bid, Kalshi-B ask). Returns {arb,dir,net} or
    None. dir = (venue backing A)+(venue backing B), e.g. 'PK' = back A on polymarket, B on Kalshi."""
    if None in (pm_bid, pm_ask, kA_ask, kB_ask):
        return None
    pm_backB = round(1 - pm_bid, 4)
    aA, vA = (pm_ask, "P") if pm_ask <= kA_ask else (kA_ask, "K")
    aB, vB = (pm_backB, "P") if pm_backB <= kB_ask else (kB_ask, "K")
    feeA = pfee(aA) if vA == "P" else kfee(aA)
    feeB = pfee(aB) if vB == "P" else kfee(aB)
    net = round((1 - (aA + aB)) - feeA - feeB, 4)
    return {"arb": net > 0, "dir": vA + vB, "net": net}


class GameTracker:
    """2-outcome cross-venue tracker for ONE game: the polymarket game market (YES = team A) + the TWO
    Kalshi single-team markets (ticker_a = 'A wins', ticker_b = 'B wins'). Emits the same transitions as
    MarketTracker; a FLIP here = the cheapest-execution config (which side is bought on which venue) flips."""
    def __init__(self, slug, ticker_a, ticker_b):
        self.slug, self.ta, self.tb = slug, ticker_a, ticker_b
        self.pm = (None, None)          # polymarket (best YES bid, best YES ask)
        self.ka = self.kb = None        # Kalshi best YES ask on ticker_a / ticker_b
        self.state = None

    def set_pm(self, bid, ask):
        self.pm = (bid, ask)

    def set_kalshi(self, side, yes_ask):   # side in {"A","B"}
        if side == "A": self.ka = yes_ask
        else: self.kb = yes_ask

    def evaluate(self):
        new = game_edge(self.pm[0], self.pm[1], self.ka, self.kb)
        label = classify(self.state, new)
        if new is not None:
            self.state = new
        return label, new


class TransitionLogger:
    """Append-only JSONL sink. One line per logged edge transition."""
    def __init__(self, path):
        self.path = path
    def write(self, market, label, state, t):
        rec = {"t": t, "market": market, "transition": label,
               "dir": state["dir"], "net_edge": round(state["net"], 4)}
        with open(self.path, "a") as f:
            f.write(json.dumps(rec) + "\n")
        return rec


# ============================================================================================
# SELF-TEST  (no network; mirrors bot/ledger.py's self-verifying harness)
# ============================================================================================
def _selftest():
    print("transition-core self-test")
    # one venue pair, books evolving over time; only the touch matters here.
    def bk(bid, ask): return ([{"px": {"value": f"{bid}"}, "qty": "100"}],
                              [{"px": {"value": f"{ask}"}, "qty": "100"}])
    trk = MarketTracker("NYC-high-72")
    seq = [   # (P bid/ask, K bid/ask, expected transition)
        (bk(0.59, 0.61), bk(0.60, 0.62), None),    # venues agree → no arb, first obs → no transition
        (bk(0.59, 0.61), bk(0.67, 0.70), "OPEN"),  # P cheap → arb dir P opens
        (bk(0.57, 0.59), bk(0.68, 0.71), "WIDEN"), # gap widens
        (bk(0.70, 0.72), bk(0.60, 0.62), "FLIP"),  # K now cheap → direction reverses
        (bk(0.59, 0.61), bk(0.60, 0.62), "CLOSE"), # venues agree again → arb closes
    ]
    got = []
    for (pb, po), (kb, ko), expect in seq:
        trk.set_book("P", pb, po)                   # a "tick" = a complete dual-venue snapshot
        trk.set_book("K", kb, ko)
        label, _ = trk.evaluate()
        got.append(label)
        assert label == expect, f"expected {expect}, got {label} (state {trk.state})"
        print(f"  P{po[0]['px']['value']}/{pb[0]['px']['value']}  K{ko[0]['px']['value']}/{kb[0]['px']['value']}  -> {label}")
    assert [g for g in got if g] == ["OPEN", "WIDEN", "FLIP", "CLOSE"]
    print("OK - weather 1:1: OPEN / WIDEN / FLIP / CLOSE all detected; no spurious transitions.")

    # --- GameTracker: 2-outcome sports (polymarket game YES=A; two Kalshi team books) ---
    g = GameTracker("nyy-bos", "K-NYY", "K-BOS")
    gseq = [   # (pm_bid, pm_ask, kA_ask, kB_ask, expected)
        (0.53, 0.55, 0.56, 0.48, None),    # backA min(.55,.56)=.55P + backB min(.47,.48)=.47P > $1 -> no arb
        (0.48, 0.50, 0.55, 0.45, "OPEN"),  # A@P .50 + B@K .45 = .95 -> arb (dir PK)
        (0.48, 0.48, 0.55, 0.44, "WIDEN"), # both legs cheaper
        (0.56, 0.58, 0.46, 0.50, "FLIP"),  # A@K .46 + B@P .44 -> dir KP
        (0.53, 0.55, 0.56, 0.48, "CLOSE"), # back to no arb
    ]
    ggot = []
    for pb, pa, ka, kb, expect in gseq:
        g.set_pm(pb, pa); g.set_kalshi("A", ka); g.set_kalshi("B", kb)
        label, _ = g.evaluate(); ggot.append(label)
        assert label == expect, f"game expected {expect}, got {label} (state {g.state})"
    assert [x for x in ggot if x] == ["OPEN", "WIDEN", "FLIP", "CLOSE"]
    print("OK - sports 2-outcome: OPEN / WIDEN / FLIP / CLOSE via cheapest-venue-per-side (3 books)")


# ============================================================================================
# LIVE LAYER  (both venue streams validated; weather=MarketTracker, sports=GameTracker; deploy gated)
# ============================================================================================
PMUS_WS = "wss://api.polymarket.us/v1/ws/markets"
PMUS_WS_PATH = "/v1/ws/markets"
KALSHI_WS = "wss://api.elections.kalshi.com/trade-api/ws/v2"   # orderbook_delta channel

def _pmus_auth_headers():
    """Ed25519 headers for the WS upgrade — signs '{ts}GET/v1/ws/markets' (VERIFIED 2026-06-08)."""
    import base64
    from cryptography.hazmat.primitives.asymmetric import ed25519
    env = {}
    for line in open(os.path.join(os.path.dirname(__file__), "..", "scripts", ".env")):
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            k, v = s.split("=", 1); env[k.strip()] = v.strip()
    priv = ed25519.Ed25519PrivateKey.from_private_bytes(base64.b64decode(env["PMUS_SECRET"])[:32])
    ts = str(int(time.time() * 1000))
    sig = base64.b64encode(priv.sign(f"{ts}GET{PMUS_WS_PATH}".encode())).decode()
    return {"X-PM-Access-Key": env["PMUS_ACCESS_KEY"], "X-PM-Timestamp": ts, "X-PM-Signature": sig}

async def run_live(logger, refresh_sec=300):
    """Open both venue WS streams, route each book delta to the right tracker, log transitions. Self-
    discovers the co-listed universe (bot/colisted_map.py): WEATHER via the 1:1 MarketTracker, SPORTS via
    the 2-outcome GameTracker (pm game market + two Kalshi team tickers). Re-audits coverage + churn on the
    heartbeat. Read-only (no orders); droplet DEPLOY is gated (decision 0006)."""
    import asyncio, websockets
    from colisted_map import build_colisted_map
    shard_size = 100
    colisted, rep = build_colisted_map()
    for kind in ("weather_cities_UNMAPPED", "sports_leagues_UNMAPPED"):
        if rep[kind]:
            print(f"[coverage] WARNING unmapped {kind}: {rep[kind]} (MISSED until added to colisted_map.py)")

    def emit(label, state, key):
        if label:
            rec = logger.write(key, label, state, int(time.time()))
            print(f"[{rec['t']}] {key} {label} dir={rec['dir']} net={rec['net_edge']}")

    # dispatch: pmus slug -> fn(bids, offers) ;  Kalshi ticker -> fn(KalshiBook)
    pm_targets, k_targets = {}, {}
    for e in colisted["weather"]:                         # WEATHER = 1:1 binary MarketTracker
        trk = MarketTracker(e["slug"])
        def pm_fn(b, o, trk=trk, key=e["slug"]):
            trk.set_book("P", b, o); emit(*trk.evaluate(), key)
        def k_fn(book, trk=trk, key=e["slug"]):
            trk.set_book("K", book.yes_bid_ladder(), book.yes_offer_ladder()); emit(*trk.evaluate(), key)
        pm_targets[e["slug"]] = pm_fn; k_targets[e["kalshi"]] = k_fn
    for e in colisted["sports"]:                          # SPORTS = 2-outcome GameTracker (pm game + 2 K tickers)
        g = GameTracker(e["slug"], e["kalshi_a"], e["kalshi_b"])
        def pm_fn(b, o, g=g, key=e["slug"]):
            g.set_pm(best_px(b), best_px(o)); emit(*g.evaluate(), key)
        def ka_fn(book, g=g, key=e["slug"]):
            g.set_kalshi("A", book.best()[1]); emit(*g.evaluate(), key)
        def kb_fn(book, g=g, key=e["slug"]):
            g.set_kalshi("B", book.best()[1]); emit(*g.evaluate(), key)
        pm_targets[e["slug"]] = pm_fn
        k_targets[e["kalshi_a"]] = ka_fn; k_targets[e["kalshi_b"]] = kb_fn
    print(f"[discovery] tracking {len(colisted['weather'])} weather + {len(colisted['sports'])} sports "
          f"({len(pm_targets)} pmus slugs, {len(k_targets)} Kalshi tickers)")

    async def pmus_stream():
        slugs = list(pm_targets)
        async with websockets.connect(PMUS_WS, additional_headers=_pmus_auth_headers()) as ws:
            for i in range(0, len(slugs), shard_size):          # ≤100 slugs per subscription
                await ws.send(json.dumps({"subscribe": {
                    "requestId": f"md-{i}", "subscriptionType": "SUBSCRIPTION_TYPE_MARKET_DATA",
                    "marketSlugs": slugs[i:i + shard_size]}}))
            async for msg in ws:
                md = (json.loads(msg) or {}).get("marketData")
                fn = pm_targets.get((md or {}).get("marketSlug"))
                if fn:
                    fn(md.get("bids", []), md.get("offers", []))

    async def kalshi_stream():
        # RSA-PSS handshake + orderbook_delta, merged via KalshiBook (bot/kalshi_book.py; validated
        # offline + live). seq is ONE per-connection counter -> a gap means resubscribe everything.
        tickers = list(k_targets)
        async with websockets.connect(KALSHI_WS, additional_headers=kalshi_ws_headers()) as ws:
            async def subscribe():
                await ws.send(json.dumps({"id": 1, "cmd": "subscribe",
                    "params": {"channels": ["orderbook_delta"], "market_tickers": tickers}}))
            await subscribe()
            books, st = {}, SeqTracker()
            async for raw in ws:
                o = json.loads(raw)
                if "seq" in o and not st.check(o["seq"]):       # connection-level gap -> resync all
                    books.clear(); st.reset(); await subscribe(); continue
                typ, msg = o.get("type"), o.get("msg", {}); tk = msg.get("market_ticker")
                if typ == "orderbook_snapshot":
                    books[tk] = KalshiBook(tk); books[tk].apply_snapshot(msg)
                elif typ == "orderbook_delta" and tk in books:
                    books[tk].apply_delta(msg)
                else:
                    continue
                fn = k_targets.get(tk)
                if fn:
                    fn(books[tk])

    async def rest_heartbeat():     # periodic FULL re-discovery: coverage audit + churn (decision 0003)
        while True:
            await asyncio.sleep(refresh_sec)
            fresh, r2 = await asyncio.to_thread(build_colisted_map)
            fresh_slugs = {e["slug"] for e in fresh["weather"] + fresh["sports"]}
            added, gone = fresh_slugs - set(pm_targets), set(pm_targets) - fresh_slugs
            for kind in ("weather_cities_UNMAPPED", "sports_leagues_UNMAPPED"):
                if r2[kind]: print(f"[coverage] unmapped {kind}: {r2[kind]}")
            if added or gone:
                print(f"[discovery] churn: +{len(added)} new / -{len(gone)} settled markets")
            # TODO: (un)subscribe added/gone on both live WS connections + spin up/down their trackers.

    await asyncio.gather(pmus_stream(), kalshi_stream(), rest_heartbeat())


if __name__ == "__main__":
    if "--live" in sys.argv:
        import asyncio
        secs = next((int(a) for a in sys.argv[1:] if a.isdigit()), 60)
        out = os.path.join(os.path.dirname(__file__), "..", "scripts", "_data", "transitions.jsonl")
        print(f"LIVE read-only dual-stream ~{secs}s -> {out}  (no orders; droplet DEPLOY still gated, 0006)")
        try:
            asyncio.run(asyncio.wait_for(run_live(TransitionLogger(out)), timeout=secs))
        except (asyncio.TimeoutError, KeyboardInterrupt):
            print(f"stopped after ~{secs}s")
    else:
        _selftest()
