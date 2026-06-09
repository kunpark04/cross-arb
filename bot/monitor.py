"""bot/monitor.py — dual-stream cross-venue edge MONITOR (persistence layer; decisions 0003 + 0005).

READ-ONLY. Streams order books from BOTH venues, recomputes the cross-venue edge on every book delta,
and logs edge STATE-TRANSITIONS (open / close / flip / widen / narrow) to JSONL — the persistence data
that drives the ledger's layer-vs-rotate rule (bot/ledger.py, decision 0004). It does NOT place orders.

  python bot/monitor.py              # runs the OFFLINE self-test of the transition core (no network)
  python bot/monitor.py --live 75    # GATED: bounded live run ~75s (needs creds + the co-listed map)
  python bot/monitor.py --forever    # GATED: unbounded live run for systemd supervision (droplet deploy)

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
import os, sys, re, json, time
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
    """Build the {p_yb,p_ya,k_yb,k_ya} YES bid/ask quad signal() consumes. A touch may be None (one-sided
    book) — signal() prices each direction on the two quotes it needs. Returns None ONLY if NEITHER
    direction is fully quoted (nothing priceable). Crossed-book rejection is per-direction in signal()."""
    px = {"p_yb": best_px(p_bids), "p_ya": best_px(p_offers),
          "k_yb": best_px(k_bids), "k_ya": best_px(k_offers)}
    dir_p = px["p_ya"] is not None and px["k_yb"] is not None     # YES@P + NO@K  needs P-ask + K-bid
    dir_k = px["k_ya"] is not None and px["p_yb"] is not None     # YES@K + NO@P  needs K-ask + P-bid
    return px if (dir_p or dir_k) else None

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


# --- DEPTH: how much SIZE the arb supports, not just the touch edge. An arb = buy 1 unit of leg-A +
#     1 unit of leg-B (YES on the cheap venue + NO on the dear venue; or back-A + back-B for a game).
#     depth_curve walks BOTH legs' ask ladders in lockstep and reports cumulative fillable PAIRS while
#     the marginal GROSS pair edge (1 - a_ask - b_ask) stays >= each threshold (gross; pick a threshold
#     that absorbs fees). This is what answers "how big a clip / how much capital per arb". --------------
DEPTH_THRESHOLDS = (0.02, 0.01, 0.0)

def _pairs(levels):                                  # [{px:{value},qty}] -> [(price, qty)] (keeps order)
    out = []
    for l in (levels or []):
        try: out.append((float(l["px"]["value"]), float(l["qty"])))
        except (KeyError, ValueError, TypeError): pass
    return out

def _no_ask_pairs(bid_levels):                       # YES bids (desc px) -> NO-asks (asc px): 1 - yes_bid
    return [(round(1.0 - p, 6), q) for p, q in _pairs(bid_levels)]

def depth_curve(a_ladder, b_ladder, thresholds=DEPTH_THRESHOLDS):
    """Cumulative fillable contract-PAIRS while the marginal gross pair edge stays >= each threshold.
    a_ladder/b_ladder are [(ask_price, qty)] ascending by price (the asks you'd consume). Returns
    {threshold: pairs}. Pure; two-pointer merge over both ask ladders."""
    out = {t: 0.0 for t in thresholds}
    lo, i, j = min(thresholds), 0, 0
    a_rem = a_ladder[0][1] if a_ladder else 0.0
    b_rem = b_ladder[0][1] if b_ladder else 0.0
    while i < len(a_ladder) and j < len(b_ladder):
        edge = 1.0 - a_ladder[i][0] - b_ladder[j][0]
        if edge < lo: break
        step = min(a_rem, b_rem)
        if step <= 1e-9: break
        for t in thresholds:
            if edge >= t: out[t] += step
        a_rem -= step; b_rem -= step
        if a_rem <= 1e-9: i += 1; a_rem = a_ladder[i][1] if i < len(a_ladder) else 0.0
        if b_rem <= 1e-9: j += 1; b_rem = b_ladder[j][1] if j < len(b_ladder) else 0.0
    return out

def _depth_dict(a_ladder, b_ladder):                 # labelled, JSON-safe (contracts at gross >= 2c/1c/0c)
    d = depth_curve(a_ladder, b_ladder)
    return {"c2": round(d[0.02]), "c1": round(d[0.01]), "c0": round(d[0.0])}


class MarketTracker:
    """Holds the latest YES book per venue for ONE co-listed market and emits transitions.
    `evaluate()` classifies the current COMPLETE dual-venue state against the last complete state."""
    def __init__(self, key):
        self.key = key
        self.books = {"P": (None, None), "K": (None, None)}   # venue -> (bids, offers)
        self.state = None
        self.ts = {"P": 0.0, "K": 0.0}                        # last-update wall-clock per venue (staleness)

    def set_book(self, venue, bids, offers):
        self.books[venue] = (bids, offers)
        self.ts[venue] = time.time()

    def _depth(self, direction):
        """Fillable PAIRS for the SIGNALLED direction (always cross-venue): 'P' = YES@P + NO@K,
        'K' = YES@K + NO@P. {c2,c1,c0} = contracts at gross marginal edge >= 2c/1c/0c, or None."""
        (pb, po), (kb, ko) = self.books["P"], self.books["K"]
        yes_off, no_bids = (po, kb) if direction == "P" else (ko, pb)
        if not (yes_off and no_bids): return None
        return _depth_dict(_pairs(yes_off), _no_ask_pairs(no_bids))

    def _age(self):                                           # seconds since each venue's book last changed
        now = time.time()
        return {"p": round(now - self.ts["P"], 1), "k": round(now - self.ts["K"], 1)}

    def evaluate(self):
        """Classify the current complete state vs the last; advance state; return (label, state)."""
        (pb, po), (kb, ko) = self.books["P"], self.books["K"]
        px = make_px(pb, po, kb, ko)
        new = edge_state(px) if px else None
        if new and new["arb"]:
            try:                                   # additive instrumentation; never crash the collector
                new["depth"] = self._depth(new["dir"])
                new["age"] = self._age()
            except Exception:
                new["depth"] = None
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
    """2-outcome CROSS-VENUE edge for a game (polymarket YES = team A; two Kalshi single-team YES asks).
    Considers ONLY the two cross-venue hedges and picks the best — 'PK' = back A@polymarket + B@Kalshi,
    'KP' = back A@Kalshi + B@polymarket. Returns {arb,dir,net} or None. Same-venue configs are excluded
    by construction: a complete pm set (A@P+B@P) costs >=$1, and both-on-Kalshi is an intra-Kalshi arb
    (out of scope for a CROSS-venue strategy). A strictly-crossed pm book is rejected as stale (C3)."""
    if pm_bid is not None and pm_ask is not None and pm_bid > pm_ask:   # strictly-crossed pm book -> stale
        return None
    if pm_ask is not None and kA_ask is not None and abs(pm_ask - kA_ask) > 0.40:   # C3 orientation/identity guard:
        return None   # pm-YES(=A) ask and Kalshi-A ask price the SAME team -> a >40c gap is a flip/mismatch (L1/L12),
                      # NOT edge (real cross-venue arb is a few cents). Quarantines a mis-oriented YES=teamA pair.
    opts = []   # DETECTION uses the at-scale marginal Kalshi fee (no ceil) — capture any arb +EV at size
    if pm_ask is not None and kB_ask is not None:                       # PK: back A@P + B@K
        opts.append(("PK", round((1 - (pm_ask + kB_ask)) - pfee(pm_ask) - kfee(kB_ask, marginal=True), 4)))
    if kA_ask is not None and pm_bid is not None:                       # KP: back A@K + B@P
        pm_backB = round(1 - pm_bid, 4)
        opts.append(("KP", round((1 - (kA_ask + pm_backB)) - kfee(kA_ask, marginal=True) - pfee(pm_backB), 4)))
    if not opts:
        return None
    best = max(opts, key=lambda o: o[1])
    return {"arb": best[1] > 0, "dir": best[0], "net": best[1]}


class GameTracker:
    """2-outcome cross-venue tracker for ONE game: the polymarket game market (YES = team A) + the TWO
    Kalshi single-team markets (ticker_a = 'A wins', ticker_b = 'B wins'). Emits the same transitions as
    MarketTracker; a FLIP here = the cheapest-execution config (which side is bought on which venue) flips."""
    def __init__(self, slug, ticker_a, ticker_b):
        self.slug, self.ta, self.tb = slug, ticker_a, ticker_b
        self.pm = (None, None)          # polymarket (best YES bid, best YES ask)
        self.ka = self.kb = None        # Kalshi best YES ask on ticker_a / ticker_b
        self.state = None
        self.pm_bids = self.pm_offers = None       # full ladders (for depth; additive — edge uses scalars)
        self.ka_book = self.kb_book = None         # KalshiBook per team (for depth)
        self.ts = {"P": 0.0, "K": 0.0}             # last-update wall-clock (staleness)

    def set_pm(self, bid, ask):
        self.pm = (bid, ask); self.ts["P"] = time.time()

    def set_kalshi(self, side, yes_ask):   # side in {"A","B"}
        if side == "A": self.ka = yes_ask
        else: self.kb = yes_ask
        self.ts["K"] = time.time()

    def feed_pm(self, bids, offers):       # full polymarket ladders for depth (optional)
        self.pm_bids, self.pm_offers = bids, offers

    def feed_kalshi(self, side, book):     # the KalshiBook for depth (optional)
        if side == "A": self.ka_book = book
        else: self.kb_book = book

    def _depth(self, direction):
        """Fillable PAIRS for the chosen cross-venue config: dir 'PK' = back A@P + B@K, 'KP' = A@K + B@P.
        Returns {c2,c1,c0} or None."""
        if self.pm_bids is None or self.ka_book is None or self.kb_book is None: return None
        a = _pairs(self.pm_offers) if direction[0] == "P" else self.ka_book.offer_pairs()       # back A
        b = _no_ask_pairs(self.pm_bids) if direction[1] == "P" else self.kb_book.offer_pairs()  # back B
        return _depth_dict(a, b)

    def _age(self):
        now = time.time()
        return {"p": round(now - self.ts["P"], 1), "k": round(now - self.ts["K"], 1)}

    def evaluate(self):
        new = game_edge(self.pm[0], self.pm[1], self.ka, self.kb)
        if new and new["arb"]:
            try:                                   # additive; guarded so it can't crash collection
                new["depth"] = self._depth(new["dir"])
                new["age"] = self._age()
            except Exception:
                new["depth"] = None
        label = classify(self.state, new)
        if new is not None:
            self.state = new
        return label, new


class FlipDebouncer:
    """Live per-frame flicker guard. A genuine direction reversal arrives as CLOSE then OPEN across two
    frames (the legs move one at a time), and a half-updated book can momentarily drop the arb. This
    holds a CLOSE for `window` seconds: if an OPPOSITE-direction OPEN follows it becomes one FLIP; a
    SAME-direction reopen is a flicker and is suppressed; otherwise the CLOSE is flushed once the window
    elapses. feed()/flush() return the list of (label, state, key) tuples to actually log."""
    def __init__(self, window=1.0):
        self.window = window
        self.last_arb_dir = {}                 # key -> dir while arb'd
        self.pending = {}                      # key -> (t, closed_from_dir, close_state)
    def feed(self, label, state, key, now):
        if not label:
            return []
        if label == "CLOSE":
            self.pending[key] = (now, self.last_arb_dir.get(key), state)
            return []                          # hold; a flip may complete on the next frame
        if label == "OPEN":
            pc = self.pending.pop(key, None)
            if pc and now - pc[0] <= self.window:
                if pc[1] and pc[1] != state["dir"]:
                    label = "FLIP"             # close + opposite-direction open = a flip
                else:
                    self.last_arb_dir[key] = state["dir"]
                    return []                  # same-direction reopen = flicker, suppress
        self.last_arb_dir[key] = state["dir"]
        return [(label, state, key)]
    def flush(self, now):
        out = []
        for key in [k for k, (t, _, _) in self.pending.items() if now - t > self.window]:
            t, _, st = self.pending.pop(key); self.last_arb_dir.pop(key, None)
            out.append(("CLOSE", st, key))
        return out
    def forget(self, key):                     # drop a settled market's debounce state (called on prune)
        self.pending.pop(key, None)
        self.last_arb_dir.pop(key, None)


def event_partition(market):
    """Partition key = the event date embedded in the market slug (YYYY-MM-DD): e.g.
    'aec-mlb-sea-bal-2026-06-10' -> '2026-06-10', 'tc-temp-laxhigh-2026-06-09-...' -> '2026-06-09'.
    Partitioning by the EVENT date (not the wall-clock day) keeps a market's whole edge lifecycle in
    ONE file even when it straddles UTC midnight — so nothing is ever cut off. No date -> 'misc'."""
    m = re.search(r"\d{4}-\d{2}-\d{2}", str(market))
    return m.group(0) if m else "misc"


class TransitionLogger:
    """Append-only JSONL sink, PARTITIONED BY EVENT-DATE: one file per event date
    (transitions-<YYYY-MM-DD>.jsonl) so a market's full lifecycle is never split at wall-clock midnight.
    sessions.jsonl records each monitor boot (session_start) so restart-aware analysis won't mistake a
    post-restart re-OPEN for a genuine new edge. The droplet copy is the append-only SOURCE OF TRUTH;
    pull-data.ps1 mirrors it read-only (copy-keep, checksum-verified) and never mutates these files."""
    def __init__(self, data_dir):
        self.dir = data_dir
        os.makedirs(data_dir, exist_ok=True)    # _data/ is gitignored => may not exist on a fresh host
    def write(self, market, label, state, t):
        rec = {"t": t, "market": market, "transition": label,
               "dir": state["dir"], "net_edge": round(state["net"], 4)}
        d = state.get("depth")
        if d: rec["depth"] = d                  # {c2,c1,c0} contracts fillable at gross >= 2c/1c/0c
        a = state.get("age")
        if a: rec["age"] = a                    # {p,k} seconds since each venue's book last changed (staleness)
        with open(os.path.join(self.dir, f"transitions-{event_partition(market)}.jsonl"), "a") as f:
            f.write(json.dumps(rec) + "\n")
        return rec
    def resync(self, info):
        """Mark a Kalshi seq-gap resync in sessions.jsonl. A resync clears + re-snapshots all Kalshi
        books, which momentarily drops every Kalshi-derived edge then re-opens it — analysis force-closes
        episodes here (like a restart) so those spurious re-OPENs aren't counted as real."""
        rec = {"t": int(time.time()), "event": "kalshi_resync", **info}
        with open(os.path.join(self.dir, "sessions.jsonl"), "a") as f:
            f.write(json.dumps(rec) + "\n")
        return rec
    def cli(self, rec):
        """Append one NWS CLI issuance reading to cli.jsonl (settlement timing/revision measurement)."""
        rec = {"t": int(time.time()), **rec}
        with open(os.path.join(self.dir, "cli.jsonl"), "a") as f:
            f.write(json.dumps(rec) + "\n")
        return rec
    def session_start(self, info):
        """Append a session_start marker (one per monitor boot) to sessions.jsonl."""
        rec = {"t": int(time.time()), "event": "session_start", **info}
        with open(os.path.join(self.dir, "sessions.jsonl"), "a") as f:
            f.write(json.dumps(rec) + "\n")
        return rec
    def health(self, info):
        """Atomically overwrite health.json — a liveness beacon (timestamp + counts) the off-box
        healthcheck reads to catch a dead/hung monitor (a hung-but-'active' process stops freshening it)."""
        rec = {"t": int(time.time()), **info}
        tmp = os.path.join(self.dir, "health.json.tmp")
        with open(tmp, "w") as f:
            f.write(json.dumps(rec))
        os.replace(tmp, os.path.join(self.dir, "health.json"))   # atomic: a reader never sees a partial file
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

    # --- FlipDebouncer: CLOSE + opposite-OPEN within window = FLIP; same-dir = flicker (suppressed) ---
    A = {"arb": True, "dir": "PK", "net": 0.03}; Ac = {"arb": False, "dir": "PK", "net": -0.01}
    B = {"arb": True, "dir": "KP", "net": 0.04}
    d = FlipDebouncer(1.0)
    assert d.feed("OPEN", A, "m", 100.0) == [("OPEN", A, "m")]
    assert d.feed("CLOSE", Ac, "m", 100.2) == []                  # held
    assert d.feed("OPEN", B, "m", 100.3) == [("FLIP", B, "m")]    # opposite within window -> FLIP
    d2 = FlipDebouncer(1.0); d2.feed("OPEN", A, "m", 0.0); d2.feed("CLOSE", Ac, "m", 0.1)
    assert d2.feed("OPEN", A, "m", 0.2) == []                     # same dir within window -> flicker
    d3 = FlipDebouncer(1.0); d3.feed("OPEN", A, "m", 0.0); d3.feed("CLOSE", Ac, "m", 0.1)
    assert d3.flush(0.5) == [] and d3.flush(2.0) == [("CLOSE", Ac, "m")]   # real close flushes post-window
    print("OK - FlipDebouncer: FLIP coalesce, flicker suppress, CLOSE flush")

    # --- idle-market pruning: 2-miss debounce, then symmetric teardown frees every reference ---
    absent = {}
    assert prune_decision({"a", "b", "c"}, {"a"}, absent) == set()        # round 1: b,c missing once -> hold
    assert absent == {"a": 0, "b": 1, "c": 1}
    assert prune_decision({"a", "b", "c"}, {"a"}, absent) == {"b", "c"}   # round 2: missing twice -> prune
    prune_decision({"a", "b"}, {"a", "b"}, absent)                        # b reappears -> miss count resets
    assert absent["b"] == 0
    # teardown removes the pmus closure + BOTH Kalshi tickers/books + slug map row + debouncer state
    pmt = {"g1": lambda *a: None}; kt = {"KA": 1, "KB": 2}; bks = {"KA": object(), "KB": object()}
    sk = {"g1": ["KA", "KB"]}; ab = {"g1": 2}; d = FlipDebouncer(1.0)
    d.feed("OPEN", {"arb": True, "dir": "PK", "net": 0.03}, "g1", 0.0)    # seed debouncer state for g1
    teardown("g1", pmt, kt, bks, sk, d, ab)
    assert pmt == {} and kt == {} and bks == {} and sk == {} and ab == {}
    assert "g1" not in d.last_arb_dir and "g1" not in d.pending
    print("OK - prune: 2-miss debounce + symmetric teardown (closures, books, map, debounce) all freed")

    # --- TransitionLogger: event-date partitioning (a lifecycle stays in ONE file) + sessions.jsonl ---
    import tempfile, glob, shutil
    td = tempfile.mkdtemp()
    lg = TransitionLogger(td)
    lg.write("aec-mlb-sea-bal-2026-06-10", "OPEN",  {"dir": "PK", "net": 0.03}, 1)
    lg.write("tc-temp-laxhigh-2026-06-09-gte73", "OPEN", {"dir": "K", "net": 0.02}, 2)
    lg.write("aec-mlb-sea-bal-2026-06-10", "CLOSE", {"dir": "PK", "net": -0.01}, 9)  # SAME file as its OPEN
    lg.write("freeform-no-date", "OPEN", {"dir": "P", "net": 0.01}, 3)
    lg.session_start({"weather": 1, "sports": 1})
    lg.health({"weather": 1, "sports": 1})
    assert json.load(open(os.path.join(td, "health.json")))["t"] > 0   # liveness beacon written atomically
    names = sorted(os.path.basename(p) for p in glob.glob(os.path.join(td, "*.jsonl")))
    assert names == ["sessions.jsonl", "transitions-2026-06-09.jsonl",
                     "transitions-2026-06-10.jsonl", "transitions-misc.jsonl"], names
    body = open(os.path.join(td, "transitions-2026-06-10.jsonl")).read().strip().split("\n")
    assert len(body) == 2, body          # OPEN + CLOSE of one market land together (no midnight split)
    shutil.rmtree(td, ignore_errors=True)
    print("OK - TransitionLogger: event-date partition keeps a lifecycle whole; misc fallback; sessions.jsonl")

    # --- DEPTH: two-pointer fillable-pairs walk + both trackers' binding-direction logic ---
    a = [(0.40, 10), (0.42, 20)]; b = [(0.50, 5), (0.55, 30)]
    assert depth_curve(a, b, (0.04, 0.02, 0.0)) == {0.04: 10, 0.02: 30, 0.0: 30}     # .03-edge pair excluded at .04
    # weather MarketTracker: YES cheap on P (.59/.60 offers), NO cheap on K (.68/.67 bids)
    def lv(rows): return [{"px": {"value": f"{p}"}, "qty": f"{q}"} for p, q in rows]
    wt = MarketTracker("dtest")
    wt.set_book("P", lv([(0.57, 99)]), lv([(0.59, 10), (0.60, 40)]))   # P yes-cheap -> dir P (YES@P+NO@K)
    wt.set_book("K", lv([(0.68, 20), (0.67, 50)]), lv([(0.70, 99)]))
    assert wt._depth("P") == {"c2": 50, "c1": 50, "c0": 50}, wt._depth("P")
    # sports GameTracker: dir PK = back A@P (.50 offers) + B@K (yes_ask .44 from a .56 NO bid)
    g = GameTracker("gd", "KA", "KB")
    g.set_pm(0.48, 0.50); g.set_kalshi("A", 0.55); g.set_kalshi("B", 0.44)
    g.feed_pm(lv([(0.48, 10)]), lv([(0.50, 8)]))
    ka = KalshiBook("KA"); ka.apply_snapshot({"no_dollars_fp": [["0.45", "30"]]})    # yes ask 1-.45=.55
    kb = KalshiBook("KB"); kb.apply_snapshot({"no_dollars_fp": [["0.56", "30"]]})    # yes ask 1-.56=.44
    g.feed_kalshi("A", ka); g.feed_kalshi("B", kb)
    assert g._depth("PK") == {"c2": 8, "c1": 8, "c0": 8}, g._depth("PK")             # min(pm 8, kB 30) = 8 pairs
    # crossed-book reject (now per-direction in signal/game_edge) + ONE-SIDED books still price the valid side
    cross = make_px(lv([(0.70, 9)]), lv([(0.60, 9)]), lv([(0.50, 9)]), lv([(0.52, 9)]))   # P crossed (bid>ask)
    cs = edge_state(cross); assert cs is not None and not cs["arb"]                        # crossed venue -> no arb
    assert game_edge(0.70, 0.60, 0.55, 0.44) is None                                       # crossed pm -> None
    one = make_px([], lv([(0.55, 9)]), lv([(0.62, 9)]), lv([(0.70, 9)]))                   # P has NO bid (one-sided)
    os1 = edge_state(one); assert os1 and os1["arb"] and os1["dir"] == "P"                 # dir P still prices (was dropped before)
    assert game_edge(None, 0.50, 0.55, 0.44)["dir"] == "PK"                                # one-sided pm (no bid) -> PK still prices
    print("OK - depth: signalled-direction pairs walk; crossed reject + ONE-SIDED books price per-direction")

    # --- NWS CLI parse (settlement timing/revision measurement) ---
    cli_sample = ("CDUS41 KOKX 090619\nCLINYC\nCLIMATE REPORT\nNATIONAL WEATHER SERVICE NEW YORK, NY\n"
                  "219 AM EDT TUE JUN 09 2026\n...THE CENTRAL PARK NY CLIMATE SUMMARY FOR JUNE 8 2026...\n"
                  "TEMPERATURE (F)\n  MAXIMUM         75   1200 PM  95    1933  78     -3       76\n"
                  "  MINIMUM         60    459 AM\n MAXIMUM TEMPERATURE (F)   78        97      1933\n")
    pc = parse_cli(cli_sample, "NYC")
    assert pc and pc["max"] == 75 and pc["report_date"] == "2026-06-08" and pc["wmo"] == "CDUS41 KOKX 090619", pc
    assert _cli_iso("JUNE 8 2026") == "2026-06-08"
    print("OK - NWS CLI parse: daily max + report-date + WMO id (settlement-revision rate logging)")


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


# --- idle-market pruning: keep the working set = the LIVE co-listed universe, so a long run's memory
#     stays FLAT instead of growing as markets settle (decision 0003's event-driven discovery is the
#     source of truth for "still live"). Both pure; offline-tested in _selftest. -----------------------
PRUNE_THRESHOLD = 2          # free a market after it's missing from discovery this many heartbeats (debounce)

def prune_decision(tracked, current, absent, threshold=PRUNE_THRESHOLD):
    """Which tracked pmus slugs to free. Settlement removes a market from discovery, so a slug absent
    from `current` for `threshold` consecutive heartbeats is settled. The debounce stops a transient
    discovery blip (API hiccup / pagination) from dropping a still-live market (it re-appears → reset).
    Mutates `absent` (slug -> consecutive-miss count) and returns the set of slugs to tear down."""
    to_prune = set()
    for slug in tracked:
        if slug in current:
            absent[slug] = 0
        else:
            absent[slug] = absent.get(slug, 0) + 1
            if absent[slug] >= threshold:
                to_prune.add(slug)
    return to_prune

def teardown(slug, pm_targets, k_targets, books, slug_k, deb, absent):
    """Symmetrically remove EVERY reference to a settled market so it can be GC'd: the pmus closure, the
    1-or-2 Kalshi closures + their KalshiBooks, the slug↔ticker map row, and the debouncer state."""
    pm_targets.pop(slug, None)
    for tk in slug_k.pop(slug, []):
        k_targets.pop(tk, None)
        books.pop(tk, None)
    deb.forget(slug)
    absent.pop(slug, None)


# --- NWS CLI revision logging: MEASURE how often the morning preliminary daily-max is later CORRECTED.
#     Both venues settle ~8am ET off the SAME morning NWS Climatological Report (CLI); a *downward* morning
#     correction can split a bucket-boundary day (Kalshi delays + takes the lower value, polymarket.us locks
#     at 8am) -> the settlement timing/revision risk (research 2026-06-09 / research/settlement-verification.md).
#     We log every DISTINCT CLI issuance per station to quantify the rate. Read-only public NWS product. ------
CLI_STATIONS = ["NYC", "LAX", "MDW", "MIA", "SFO"]    # Central Park / LA / Chicago-Midway / Miami / SF (= WX cities)
_MONTHS = {m: i + 1 for i, m in enumerate(
    ["JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE", "JULY", "AUGUST", "SEPTEMBER", "OCTOBER", "NOVEMBER", "DECEMBER"])}

def _cli_iso(s):                                       # "JUNE 8 2026" -> "2026-06-08"
    p = str(s).upper().split()
    try: return f"{int(p[2]):04d}-{_MONTHS[p[0]]:02d}-{int(p[1]):02d}"
    except Exception: return str(s)

def parse_cli(txt, station):
    """Parse an NWS Daily Climate Report -> {station, report_date, max, issued, wmo} or None. The first
    'MAXIMUM <n>' line is the daily observed max; 'SUMMARY FOR <MONTH D YEAR>' is the date the data is FOR."""
    txt = re.sub(r"<[^>]+>", "", txt)
    wmo = re.search(r"^([A-Z]{4}\d{2}[ \t]+[A-Z]{4}[ \t]+\d{6}(?:[ \t]+[A-Z]{3})?)", txt, re.M)  # BBB stays on-line
    iss = re.search(r"^\s*(\d{1,4}\s+(?:AM|PM)\s+[A-Z]{2,4}\s+[A-Z]{3}\s+[A-Z]{3}\s+\d{1,2}\s+\d{4})", txt, re.M)
    rd = re.search(r"SUMMARY FOR\s+([A-Z]+\s+\d{1,2}\s+\d{4})", txt, re.I)
    mx = re.search(r"^\s*MAXIMUM\s+(-?\d+)\b", txt, re.M)
    if not (mx and rd):
        return None
    return {"station": station, "report_date": _cli_iso(rd.group(1)), "max": int(mx.group(1)),
            "issued": (iss.group(1).strip() if iss else None), "wmo": (wmo.group(1).strip() if wmo else None)}

def fetch_cli(station):
    """Fetch + parse the live CLI for a station id ('NYC','LAX',...). Returns the dict or None on any error."""
    import urllib.request
    url = (f"https://forecast.weather.gov/product.php?site=NWS&issuedby={station}"
           f"&product=CLI&format=TXT&version=1&glossary=0")
    try:
        html = urllib.request.urlopen(
            urllib.request.Request(url, headers={"User-Agent": "cross-arb/1.0"}), timeout=20).read().decode("utf-8", "replace")
        m = re.search(r"<pre[^>]*>(.*?)</pre>", html, re.S | re.I)
        return parse_cli(m.group(1) if m else html, station)
    except Exception:
        return None


async def run_live(logger, refresh_sec=300, debounce=1.0):
    """Open both venue WS streams, route each book delta to the right tracker, log transitions. Self-
    discovers the co-listed universe (bot/colisted_map.py): WEATHER via the 1:1 MarketTracker, SPORTS via
    the 2-outcome GameTracker (pm game market + two Kalshi team tickers). Re-audits coverage + churn on the
    heartbeat. Read-only (no orders); droplet DEPLOY is gated (decision 0006)."""
    import asyncio, websockets
    from colisted_map import build_colisted_map
    shard_size = 100
    pm_targets, k_targets, conns = {}, {}, {}     # slug->fn(bids,offers) ; ticker->fn(book) ; "pm"/"k"->ws
    books = {}                                    # Kalshi ticker -> KalshiBook (shared so prune can free it)
    slug_k, absent = {}, {}                        # slug->[Kalshi tickers] for teardown ; slug->missed-heartbeats
    deb = FlipDebouncer(debounce)
    last_rx = {"pm": 0.0, "k": 0.0}               # epoch of the LAST frame received per venue (stream liveness,
                                                  # distinct from book-change `age`) -> beacon can expose a half-dead stream

    def _write(label, state, key):
        rec = logger.write(key, label, state, int(time.time()))
        print(f"[{rec['t']}] {key} {label} dir={rec['dir']} net={rec['net_edge']}")
    def emit(label, state, key):
        for lab, st, k in deb.feed(label, state, key, time.time()):
            _write(lab, st, k)

    def register(colisted):                       # add trackers for NEW markets; return (new slugs, new tickers)
        new_pm, new_k = [], []
        for e in colisted["weather"]:             # WEATHER = 1:1 binary MarketTracker
            if e["slug"] in pm_targets: continue
            trk = MarketTracker(e["slug"])
            def pm_fn(b, o, trk=trk, key=e["slug"]): trk.set_book("P", b, o); emit(*trk.evaluate(), key)
            def k_fn(book, trk=trk, key=e["slug"]): trk.set_book("K", book.yes_bid_ladder(), book.yes_offer_ladder()); emit(*trk.evaluate(), key)
            pm_targets[e["slug"]] = pm_fn; k_targets[e["kalshi"]] = k_fn
            slug_k[e["slug"]] = [e["kalshi"]]
            new_pm.append(e["slug"]); new_k.append(e["kalshi"])
        for e in colisted["sports"]:              # SPORTS = 2-outcome GameTracker (pm game + 2 K tickers)
            if e["slug"] in pm_targets: continue
            g = GameTracker(e["slug"], e["kalshi_a"], e["kalshi_b"])
            def pm_fn(b, o, g=g, key=e["slug"]): g.set_pm(best_px(b), best_px(o)); g.feed_pm(b, o); emit(*g.evaluate(), key)
            def ka_fn(book, g=g, key=e["slug"]): g.set_kalshi("A", book.best()[1]); g.feed_kalshi("A", book); emit(*g.evaluate(), key)
            def kb_fn(book, g=g, key=e["slug"]): g.set_kalshi("B", book.best()[1]); g.feed_kalshi("B", book); emit(*g.evaluate(), key)
            pm_targets[e["slug"]] = pm_fn; k_targets[e["kalshi_a"]] = ka_fn; k_targets[e["kalshi_b"]] = kb_fn
            slug_k[e["slug"]] = [e["kalshi_a"], e["kalshi_b"]]
            new_pm.append(e["slug"]); new_k += [e["kalshi_a"], e["kalshi_b"]]
        return new_pm, new_k

    colisted, rep = build_colisted_map()
    for kind in ("weather_cities_UNMAPPED", "sports_leagues_UNMAPPED"):
        if rep[kind]:
            print(f"[coverage] WARNING unmapped {kind}: {rep[kind]} (MISSED until added to colisted_map.py)")
    if rep.get("weather_bucket_MISALIGNED"):                      # C4: non-identical degF buckets are NOT paired
        print(f"[coverage] WARNING {len(rep['weather_bucket_MISALIGNED'])} weather bucket misalignments "
              f"(NOT paired - settlement-identity guard): {rep['weather_bucket_MISALIGNED'][:3]}")
    register(colisted)
    print(f"[discovery] tracking {len(colisted['weather'])} weather + {len(colisted['sports'])} sports "
          f"({len(pm_targets)} pmus slugs, {len(k_targets)} Kalshi tickers)")
    logger.session_start({"weather": len(colisted["weather"]), "sports": len(colisted["sports"]),
                          "pmus": len(pm_targets), "kalshi": len(k_targets)})   # restart marker
    logger.health({"weather": len(colisted["weather"]), "sports": len(colisted["sports"]),
                   "pmus": len(pm_targets), "kalshi": len(k_targets)})           # liveness beacon (startup)

    async def pmus_stream():
        # SUPERVISED reconnect loop: a CLEAN close (1000/1001 idle/LB-cycle) ends `async for` WITHOUT raising;
        # without this loop the coroutine would just return and gather() would keep the process alive with this
        # venue's book frozen (silent half-dead collector that no alarm catches). An abnormal close raises and is
        # caught here too. Either way we re-subscribe the CURRENT targets (markets added during the outage included).
        backoff = 1
        while True:
            try:
                async with websockets.connect(PMUS_WS, additional_headers=_pmus_auth_headers()) as ws:
                    conns["pm"] = ws
                    slugs = list(pm_targets)
                    for i in range(0, len(slugs), shard_size):          # ≤100 slugs per subscription
                        await ws.send(json.dumps({"subscribe": {
                            "requestId": f"md-{i}", "subscriptionType": "SUBSCRIPTION_TYPE_MARKET_DATA",
                            "marketSlugs": slugs[i:i + shard_size]}}))
                    backoff = 1                                         # connected OK -> reset backoff
                    async for msg in ws:
                        last_rx["pm"] = time.time()
                        md = (json.loads(msg) or {}).get("marketData")
                        fn = pm_targets.get((md or {}).get("marketSlug"))
                        if fn:
                            fn(md.get("bids", []), md.get("offers", []))
            except Exception as e:
                print(f"[pmus] stream dropped ({e!r}); reconnect in {backoff}s")
            else:
                print(f"[pmus] stream closed cleanly; reconnect in {backoff}s")   # the dangerous case made non-fatal
            finally:
                conns.pop("pm", None)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

    async def kalshi_stream():
        # RSA-PSS handshake + orderbook_delta, merged via KalshiBook (bot/kalshi_book.py; validated
        # offline + live). seq is ONE per-connection counter -> a gap means resubscribe everything.
        # SUPERVISED reconnect loop (see pmus_stream): on (re)connect, clear+rebuild books from fresh snapshots
        # and re-subscribe the CURRENT tickers. Tracker `state` is intentionally NOT reset (retained state avoids a
        # phantom CLOSE on every edge; the tracker's K-book cache is separate from the global `books` dict).
        backoff = 1
        while True:
            try:
                async with websockets.connect(KALSHI_WS, additional_headers=kalshi_ws_headers()) as ws:
                    conns["k"] = ws
                    async def subscribe(tickers):
                        await ws.send(json.dumps({"id": 1, "cmd": "subscribe",
                            "params": {"channels": ["orderbook_delta"], "market_tickers": tickers}}))
                    books.clear()                              # fresh connection -> rebuild every book from snapshots
                    await subscribe(list(k_targets))
                    st = SeqTracker()                          # books is shared (run_live scope) so prune can free it
                    backoff = 1
                    async for raw in ws:
                        last_rx["k"] = time.time()
                        o = json.loads(raw)
                        if "seq" in o and not st.check(o["seq"]):       # connection-level gap -> resync all
                            books.clear(); st.reset(); await subscribe(list(k_targets))
                            logger.resync({"seq": o["seq"]})            # mark it so analysis censors the re-OPENs
                            continue
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
            except Exception as e:
                print(f"[kalshi] stream dropped ({e!r}); reconnect in {backoff}s")
            else:
                print(f"[kalshi] stream closed cleanly; reconnect in {backoff}s")
            finally:
                conns.pop("k", None)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

    async def rest_heartbeat():     # periodic FULL re-discovery: subscribe NEW markets + coverage audit
        while True:
            await asyncio.sleep(refresh_sec)
            fresh, r2 = await asyncio.to_thread(build_colisted_map)
            for kind in ("weather_cities_UNMAPPED", "sports_leagues_UNMAPPED"):
                if r2[kind]: print(f"[coverage] unmapped {kind}: {r2[kind]}")
            if r2.get("weather_bucket_MISALIGNED"):     # C4: surface newly-listed misaligned buckets each heartbeat
                print(f"[coverage] {len(r2['weather_bucket_MISALIGNED'])} weather bucket misalignments (NOT paired)")
            new_pm, new_k = register(fresh)             # trackers for new weather days / games
            if new_pm and conns.get("pm"):
                for i in range(0, len(new_pm), shard_size):
                    await conns["pm"].send(json.dumps({"subscribe": {
                        "requestId": f"md-add-{int(time.time())}-{i}",
                        "subscriptionType": "SUBSCRIPTION_TYPE_MARKET_DATA",
                        "marketSlugs": new_pm[i:i + shard_size]}}))
            if new_k and conns.get("k"):
                await conns["k"].send(json.dumps({"id": 2, "cmd": "subscribe",
                    "params": {"channels": ["orderbook_delta"], "market_tickers": new_k}}))
            if new_pm:
                print(f"[discovery] +{len(new_pm)} new markets subscribed")
            # FREE settled markets: anything gone from discovery for PRUNE_THRESHOLD heartbeats. Keeps the
            # working set = the live universe, so memory stays flat over a multi-week run (no leak).
            current = {e["slug"] for e in fresh["weather"]} | {e["slug"] for e in fresh["sports"]}
            stale = prune_decision(set(pm_targets), current, absent)
            for slug in stale:
                teardown(slug, pm_targets, k_targets, books, slug_k, deb, absent)
            if stale:
                print(f"[prune] freed {len(stale)} settled markets; now tracking "
                      f"{len(pm_targets)} pmus / {len(k_targets)} Kalshi ({len(books)} live books)")
            now = time.time()                                                   # per-venue stream-liveness in the beacon:
            logger.health({"weather": len(fresh["weather"]), "sports": len(fresh["sports"]),
                           "pmus": len(pm_targets), "kalshi": len(k_targets),
                           "rx_age": {"pm": round(now - last_rx["pm"], 1) if last_rx["pm"] else None,
                                      "k": round(now - last_rx["k"], 1) if last_rx["k"] else None}})  # off-box check can
            # alert when one venue's rx_age stays high (stream silent/wedged) even though the process + beacon are live

    async def flusher():            # emit debounced CLOSEs whose flip-window elapsed
        while True:
            await asyncio.sleep(0.5)
            for lab, st, k in deb.flush(time.time()):
                _write(lab, st, k)

    async def cli_stream():         # poll NWS CLI per station every 30min; log each DISTINCT issuance
        last = {}                   # station -> (report_date, max); log only when the daily MAX changes (a revision),
        while True:                 # not on issuance-time flap (NWS version=1 can re-serve a same-max issuance)
            try:
                for st in CLI_STATIONS:
                    rec = await asyncio.to_thread(fetch_cli, st)
                    if not rec:
                        continue
                    key = (rec["report_date"], rec["max"])
                    if last.get(st) != key:
                        last[st] = key
                        logger.cli(rec)
                        print(f"[cli] {st} {rec['report_date']} max={rec['max']} ({rec['issued']})")
            except Exception as e:
                print(f"[cli] poll error (continuing): {e!r}")     # never let CLI polling crash collection
            await asyncio.sleep(1800)

    # return_exceptions: belt-and-suspenders. Each task above is now an infinite supervised loop, so a single
    # stream drop never tears down the others; if an unexpected error still escapes a task it's collected here
    # (logged) rather than cancelling the whole collector. (A truly unrecoverable error -> task ends -> if all end
    # the process exits -> systemd Restart=always; the silent clean-return hole is closed by the while-loops.)
    results = await asyncio.gather(pmus_stream(), kalshi_stream(), rest_heartbeat(), flusher(), cli_stream(),
                                   return_exceptions=True)
    for name, r in zip(("pmus", "kalshi", "heartbeat", "flusher", "cli"), results):
        if isinstance(r, Exception):
            print(f"[run_live] task {name} exited with {r!r}")


if __name__ == "__main__":
    if "--forever" in sys.argv or "--live" in sys.argv:
        import asyncio
        nums = [int(a) for a in sys.argv[1:] if a.isdigit()]
        out = os.path.join(os.path.dirname(__file__), "..", "scripts", "_data")  # dir; partitioned by event-date
        if "--forever" in sys.argv:                         # systemd-supervised continuous run (droplet)
            refresh = nums[0] if nums else 300              # optional arg: re-discovery interval (sec)
            print(f"LIVE read-only dual-stream FOREVER (refresh {refresh}s) -> {out}/transitions-<date>.jsonl  (no orders; deploy gated, 0006)")
            try:
                asyncio.run(run_live(TransitionLogger(out), refresh_sec=refresh))
            except KeyboardInterrupt:
                print("stopped (signal)")                   # systemd SIGTERM; each line is durable (open/append/close)
        else:                                               # bounded local run: --live [secs] [refresh]
            secs = nums[0] if nums else 60
            refresh = nums[1] if len(nums) > 1 else 300     # optional 2nd arg: re-discovery interval (sec)
            print(f"LIVE read-only dual-stream ~{secs}s (refresh {refresh}s) -> {out}/transitions-<date>.jsonl  (no orders; deploy gated, 0006)")
            try:
                asyncio.run(asyncio.wait_for(run_live(TransitionLogger(out), refresh_sec=refresh), timeout=secs))
            except (asyncio.TimeoutError, KeyboardInterrupt):
                print(f"stopped after ~{secs}s")
    else:
        _selftest()
