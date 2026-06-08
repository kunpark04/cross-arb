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
Kalshi WS (orderbook_delta) is the second stream — endpoint wired below; its auth is the next TODO
(not validated in this session). See research/polymarketus-api-auth.md §3c.
"""
import os, sys, json, time
sys.path.insert(0, os.path.dirname(__file__))
from ledger import signal  # reuse the cross-venue best-edge calculation (single source of truth)

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
    print("OK - OPEN / WIDEN / FLIP / CLOSE all detected; no spurious transitions.")


# ============================================================================================
# LIVE LAYER  (protocol verified for polymarket.us; Kalshi auth = next TODO; deploy = gated)
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

async def run_live(market_map, logger, shard_size=100):
    """Open both streams, route deltas through MarketTrackers, log transitions. GATED — needs the
    co-listed market_map {pmus_slug: kalshi_ticker} from the matcher (scripts/scan_all.py output).

    Wiring status:
      • polymarket.us markets WS — protocol VERIFIED; sharded subscribe + frame parse below.
      • Kalshi orderbook_delta WS — endpoint known; AUTH + delta-merge are the next implementation TODO.
    """
    import asyncio, websockets
    trackers = {slug: MarketTracker(slug) for slug in market_map}

    def on_book(venue, slug, bids, offers):
        trk = trackers.get(slug)
        if not trk:
            return
        label, state = trk.update(venue, bids, offers)
        if label:
            rec = logger.write(slug, label, state, int(time.time()))
            print(f"[{rec['t']}] {slug} {label} dir={rec['dir']} net={rec['net_edge']}")

    async def pmus_stream():
        slugs = list(market_map)
        async with websockets.connect(PMUS_WS, additional_headers=_pmus_auth_headers()) as ws:
            for i in range(0, len(slugs), shard_size):          # ≤100 slugs per subscription
                await ws.send(json.dumps({"subscribe": {
                    "requestId": f"md-{i}", "subscriptionType": "SUBSCRIPTION_TYPE_MARKET_DATA",
                    "marketSlugs": slugs[i:i + shard_size]}}))
            async for msg in ws:
                md = (json.loads(msg) or {}).get("marketData")
                if md:
                    on_book("P", md.get("marketSlug"), md.get("bids", []), md.get("offers", []))

    async def kalshi_stream():
        # Auth VERIFIED with the read-only key (scripts/probe_kalshi_ws.py): RSA-PSS over
        # "{ts}GET/trade-api/ws/v2" + 3 KALSHI-ACCESS-* headers -> 101 -> subscribe orderbook_delta.
        # TODO: apply the snapshot+delta merge (Kalshi sends an orderbook_snapshot then seq-ordered
        # orderbook_delta frames; book is bids-only per YES/NO, so YES ask = 1 - best NO bid), then
        # feed on_book("K", slug-mapped-ticker, yes_bids, derived_yes_offers).
        raise NotImplementedError("Kalshi orderbook_delta: snapshot/delta merge pending (auth verified; see scripts/probe_kalshi_ws.py)")

    async def rest_heartbeat():     # slow full-universe resync + coverage check (decision 0003)
        while True:
            await asyncio.sleep(120)
            # TODO: REST-sweep every market, reconcile against tracker state, warn on drift/gaps.

    await asyncio.gather(pmus_stream(), kalshi_stream(), rest_heartbeat())


if __name__ == "__main__":
    if "--live" in sys.argv:
        raise SystemExit("--live is GATED: wire the co-listed market_map + Kalshi WS auth, and "
                         "consult the owner before deploying (decision 0006). Run with no args for the self-test.")
    _selftest()
