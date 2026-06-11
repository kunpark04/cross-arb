"""scripts/maker_feasibility.py - maker-side WEATHER execution study (read-only; todo maker-study probe).

Three questions, answered from the EXISTING archive only (no orders, no deploys, no new logging):

  1. FEES (exact): per-contract round-trip fee for taker-taker / maker-taker (each direction) /
     maker-maker on a weather pair. Kalshi weather series are fee_type="quadratic" => resting orders
     pay $0 (research/fee-pin-2026-06-10.md; ledger.kfee(taker=False) is documented series-blind, so
     the per-series model lives HERE, not in ledger.py). pmus maker REBATE -0.0125*p*(1-p). Contrast
     column: a quadratic_with_maker_fees series (MLB) where the Kalshi maker leg pays 0.0175*p*(1-p).

  2. MAKER FILL BOUNDS: the monitor logs touches only AT TRANSITIONS (OPEN/CLOSE/WIDEN/NARROW), not
     every tick - so maker fill-rate is a BOUND, not a measurement. The sound inference: a book can
     never rest crossed, so if venue V's opposing touch at a LATER observation is strictly through a
     hypothetical resting level placed at an EARLIER observation, that resting order was provably
     filled in between (LOWER bound on fills; an == touch is queue-ambiguous and counted separately).
     What sampling hides: a touch that dipped through the level and came back BETWEEN observations is
     invisible -> the true fill rate is >= the definite-crossing rate (upper bound unobservable).

  3. ADVERSE-SELECTION BOUND: conditional on a proxy fill, where is the HEDGE leg's touch at the fill
     observation? -> hedge-slippage distribution -> bounded net EV/contract for maker-then-taker-hedge
     and maker-maker, vs the all-taker baseline (the ~3c taker fee wall).

Data: the pulled archive (default ../../data/cross-arb/): transitions-*.jsonl[.gz] + sessions.jsonl.
Weather = market key 'tc-temp-<city>high-...' (bot/colisted_map.py); weather px = {p_yb,p_ya,k_yb,k_ya}
per-venue YES touches; dir 'P' = YES@P+NO@K, 'K' = YES@K+NO@P (bot/monitor.py MarketTracker).
Pre-0013 records (t < 1781082189) carry int-second stamps and a ~1.25s CLOSE flush lag - negligible for
price-level crossings, +-1.5s on time-to-fill; --post-only restricts to the clean epoch.

  python scripts/maker_feasibility.py --selftest
  python scripts/maker_feasibility.py [--data-dir PATH] [--post-only] [--no-dump]
"""
import os, sys, re, gzip, json, glob, bisect, argparse, statistics, collections

try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "bot"))
from ledger import kfee, pfee            # taker formulas: the single source of truth (L15) - not copied

DATA_DIR = os.path.join(HERE, "..", "..", "data", "cross-arb")
EPOCH_0013 = 1781082189                  # post-0013 droplet epoch (ms stamps, detection-time CLOSEs)
WEATHER_PFX = "tc-"
TICK_C = 1                               # both venues quote weather on a 1-cent grid

# ============================================================================================
# 1. FEES - per-series Kalshi fee_type model (fee-pin 2026-06-10; primary-confirmed)
# ============================================================================================
# fee_type by series (live API pull in the fee-pin brief): ALL 5 weather series (KXHIGHTSFO/LAX/NY/
# MIA/CHI) are "quadratic" -> resting orders pay $0. MLB/WNBA/NBA/NHL/ATP/WTA + all econ are
# "quadratic_with_maker_fees" -> maker pays ceil(0.0175*N*P*(1-P)) (marginal 0.0175*p*(1-p)).
FEE_TYPE_WEATHER = "quadratic"
FEE_TYPE_MAKERFEE = "quadratic_with_maker_fees"   # e.g. KXMLBGAME - the contrast column

def kalshi_fee_c(p, taker=True, fee_type=FEE_TYPE_WEATHER):
    """Marginal per-contract Kalshi fee in CENTS at price p ($). Taker = ledger.kfee marginal.
    Maker: $0 on quadratic series (weather/esports/ITF/UFC); 0.0175*p*(1-p) on _with_maker_fees."""
    if taker:
        return kfee(p, marginal=True) * 100
    if fee_type == "quadratic":
        return 0.0
    return kfee(p, marginal=True, taker=False) * 100

def pmus_fee_c(p, taker=True):
    """Marginal per-contract pmus fee in CENTS at price p ($). Taker 0.05*p*(1-p) (= ledger.pfee);
    maker is a REBATE -0.0125*p*(1-p) (25% of taker, paid to the maker; fee-pin 2026-06-10 - ledger
    models it as 0 conservatively, the study uses the real rebate)."""
    if not 0 < p < 1:
        return 0.0
    if taker:
        return pfee(p) * 100                       # 0.05*p*(1-p), unrounded - tie to ledger
    return -0.0125 * p * (1 - p) * 100

def leg_fee_c(venue, p, maker, fee_type=FEE_TYPE_WEATHER):
    if venue == "P":
        return pmus_fee_c(p, taker=not maker)
    return kalshi_fee_c(p, taker=not maker, fee_type=fee_type)

def fee_table(gross_c=2.0):
    """Round-trip (entry-only; hold to settlement - no settlement fee on either venue: Kalshi verbatim
    'There is no settlement fee', pmus fee doc lists trade fees only) fee in cents per contract-PAIR.
    Legs: YES at p on pmus + NO at (1-p-gross) on Kalshi (dir P). Dir K swaps venue roles; the
    difference is <0.05c at these prices (both formulas share p*(1-p))."""
    rows = []
    for p in (0.05, 0.10, 0.30, 0.50):
        yes_p, no_p = p, round(1 - p - gross_c / 100, 4)
        tt = leg_fee_c("P", yes_p, False) + leg_fee_c("K", no_p, False)
        m_pm = leg_fee_c("P", yes_p, True) + leg_fee_c("K", no_p, False)    # maker pmus leg + taker K
        m_k = leg_fee_c("P", yes_p, False) + leg_fee_c("K", no_p, True)     # taker pmus + maker K ($0)
        mm = leg_fee_c("P", yes_p, True) + leg_fee_c("K", no_p, True)       # both resting
        mm_mlb = leg_fee_c("P", yes_p, True) + leg_fee_c("K", no_p, True, fee_type=FEE_TYPE_MAKERFEE)
        rows.append({"yes_p": yes_p, "no_p": no_p, "taker_taker": round(tt, 4),
                     "maker_pmus_taker_k": round(m_pm, 4), "taker_pmus_maker_k": round(m_k, 4),
                     "maker_maker_weather": round(mm, 4), "maker_maker_mlb": round(mm_mlb, 4)})
    return rows


# ============================================================================================
# LOAD - weather px observations + per-venue censor timelines
# ============================================================================================
def _open_any(path):
    return gzip.open(path, "rt", encoding="utf-8") if path.endswith(".gz") else open(path, encoding="utf-8")

def load_weather(data_dir):
    """(weather records sorted by t, censors {'pm':[t..],'k':[t..]}). At-most-one file per event date
    (raw beats .gz - same rule as analyze_persistence.load). Censors: session_start -> both venues;
    kalshi_resync -> k; ws_reconnect -> its venue (a reconnect rebuilds that venue's books - px values
    AFTER it are fresh-snapshot-accurate, but the pre-drop quote may be stale, so scans get both a
    strict (censor-stopped) and a full read)."""
    chosen = {}
    for path in glob.glob(os.path.join(data_dir, "transitions-*.jsonl.gz")) + \
                glob.glob(os.path.join(data_dir, "transitions-*.jsonl")):
        m = re.search(r"transitions-(.+?)\.jsonl(?:\.gz)?$", os.path.basename(path))
        chosen[m.group(1) if m else path] = path
    recs = []
    for path in sorted(chosen.values()):
        with _open_any(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                r = json.loads(line)
                if str(r.get("market", "")).startswith(WEATHER_PFX):
                    recs.append(r)
    recs.sort(key=lambda r: r["t"])
    censors = {"pm": [], "k": []}
    spath = os.path.join(data_dir, "sessions.jsonl")
    if os.path.exists(spath):
        with open(spath, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                r = json.loads(line)
                ev = r.get("event")
                if ev == "session_start":
                    censors["pm"].append(r["t"]); censors["k"].append(r["t"])
                elif ev == "kalshi_resync":
                    censors["k"].append(r["t"])
                elif ev == "ws_reconnect":
                    censors.setdefault(r.get("venue", "pm"), censors["pm"]).append(r["t"])
    censors["pm"].sort(); censors["k"].sort()
    return recs, censors

def cents(v):
    return None if v is None else int(round(float(v) * 100))

def touches(px, venue):
    """(bid_c, ask_c) for one venue from a weather px record (either may be None)."""
    if venue == "P":
        return cents(px.get("p_yb")), cents(px.get("p_ya"))
    return cents(px.get("k_yb")), cents(px.get("k_ya"))

def censored_between(censor_ts, t0, t1):
    """True if any censor event lands in (t0, t1]."""
    i = bisect.bisect_right(censor_ts, t0)
    return i < len(censor_ts) and censor_ts[i] <= t1


# ============================================================================================
# 2. FILL BOUNDS - level-crossing detection on the sampled touch series
# ============================================================================================
def fill_check(side, level_c, bid_c, ask_c):
    """Resting order at level_c on `side`: 'def' = opposing touch strictly THROUGH the level (a book
    can't rest crossed, so every bid>=ask_new was consumed -> provable fill regardless of queue);
    'touch' = opposing touch == level (trades happened AT the level; fill depends on queue position
    -> ambiguous); None = no evidence at this observation."""
    if side == "bid":
        if ask_c is None:
            return None
        return "def" if ask_c < level_c else ("touch" if ask_c == level_c else None)
    if bid_c is None:
        return None
    return "def" if bid_c > level_c else ("touch" if bid_c == level_c else None)

def improve_level(side, bid_c, ask_c):
    """1 tick inside the touch, degenerating to the join level when the spread is 1 tick (improving
    across a 1-tick spread would cross the book = taker, not maker)."""
    if side == "bid":
        return min(bid_c + TICK_C, ask_c - TICK_C)
    return max(ask_c - TICK_C, bid_c + TICK_C)

def pegged_crossings(by_market, censors):
    """Unconditional activity bound: for CONSECUTIVE same-market observations (no venue censor between),
    would a join-the-touch rest placed at obs i be definitely filled by obs i+1? Counts per venue+side,
    plus per-market any-crossing flags and the summed observed span (the per-market-day denominator).
    NOTE the structural bias: observations exist only when the cross-venue EDGE STATE changed, so the
    series over-samples active periods and is blind between episodes - crossings are a LOWER bound and
    'per observed market-day' is the only honest rate denominator this data supports."""
    combos = [("P", "bid"), ("P", "ask"), ("K", "bid"), ("K", "ask")]
    cnt = {c: collections.Counter() for c in combos}        # def / touch / pairs / censored
    mkt_any = collections.Counter()                          # market -> definite crossings (any combo)
    span_days, mkts_obs = 0.0, 0
    for m, obs in by_market.items():
        if len(obs) < 2:
            continue
        mkts_obs += 1
        span_days += (obs[-1][0] - obs[0][0]) / 86400.0
        for (t0, px0, _r0), (t1, px1, _r1) in zip(obs, obs[1:]):
            for venue, side in combos:
                cv = censors["pm" if venue == "P" else "k"]
                b0, a0 = touches(px0, venue)
                if b0 is None or a0 is None or b0 >= a0:     # need a proper two-sided uncrossed touch
                    continue
                if censored_between(cv, t0, t1):
                    cnt[(venue, side)]["censored"] += 1
                    continue
                level = b0 if side == "bid" else a0
                b1, a1 = touches(px1, venue)
                r = fill_check(side, level, b1, a1)
                cnt[(venue, side)]["pairs"] += 1
                if r:
                    cnt[(venue, side)][r] += 1
                    if r == "def":
                        mkt_any[m] += 1
    return cnt, mkt_any, span_days, mkts_obs


# ============================================================================================
# 3. EPISODE-ANCHORED MAKER SIM + ADVERSE-SELECTION BOUND
# ============================================================================================
# At each weather OPEN (the moment a maker strategy would quote), rest each leg of the signalled
# direction passively and scan FORWARD observations for the first definite crossing; at that fill
# observation, price the hedge leg as taker. dir P: maker legs = YES bid on pmus + YES ask on Kalshi
# (selling YES = buying NO); dir K symmetric. Full scan keeps going across reconnects (px values at
# observations are real book states; the crossing inference needs only those two states) - the strict
# variant stops at the first resting-venue censor (robustness read).
HYGIENE_POST_CENSOR_S = 60      # skip anchors within 60s after any censor (book-rebuild window, L20)
HYGIENE_MAX_AGE_S = 600         # skip anchors whose RESTING venue book is older than this. NOTE: age
                                # = seconds since the book CHANGED, not stream liveness - a quiet
                                # weather book is still an accurate level to join (you BECOME the
                                # book), so this only guards multi-minute wedge risk; 60s would bias
                                # the sample toward active books and inflate fill rates.
HYGIENE_MAX_MID_DIV_C = 20      # |pm mid - k mid| > 20c = mis-join/stale tell (L1-style, on MIDS -
                                # the maker gross legitimately includes the full pmus spread, which
                                # on thin weather books can exceed 20c, so it must NOT be the gate)

def leg_specs(direction):
    """[(resting venue, side)] for the arb legs of `direction`, + the hedge spec per leg.
    Returns list of (venue, side, hedge_fn) where hedge_fn(px_fill) -> (gross_fn args)."""
    if direction == "P":                # YES@P + NO@K
        return [("P", "bid"), ("K", "ask")]
    return [("K", "bid"), ("P", "ask")]

def hedge_eval(direction, venue, side, level_c, px_open, px_fill):
    """Given a definite fill of the resting leg at `level_c`, price the OTHER leg as taker at the fill
    observation. Returns dict(gross_c, planned_c, slip_c, fee_maker_c, fee_hedge_c, net_c) or None if
    the hedge touch is unquoted at the fill observation. All cents/contract-pair, fees marginal."""
    if direction == "P" and venue == "P":            # rest YES bid q on P; hedge = NO@K at 1-k_yb
        h0, h1 = cents(px_open.get("k_yb")), cents(px_fill.get("k_yb"))
        if h1 is None:
            return None
        gross, planned = h1 - level_c, (h0 - level_c if h0 is not None else None)
        fm = pmus_fee_c(level_c / 100, taker=False)
        fh = kalshi_fee_c((100 - h1) / 100, taker=True)
    elif direction == "P":                            # rest YES ask r on K; hedge = YES@P at p_ya
        h0, h1 = cents(px_open.get("p_ya")), cents(px_fill.get("p_ya"))
        if h1 is None:
            return None
        gross, planned = level_c - h1, (level_c - h0 if h0 is not None else None)
        fm = kalshi_fee_c(level_c / 100, taker=False)            # weather: $0
        fh = pmus_fee_c(h1 / 100, taker=True)
    elif venue == "K":                                # dir K: rest YES bid q on K; hedge = NO@P at 1-p_yb
        h0, h1 = cents(px_open.get("p_yb")), cents(px_fill.get("p_yb"))
        if h1 is None:
            return None
        gross, planned = h1 - level_c, (h0 - level_c if h0 is not None else None)
        fm = kalshi_fee_c(level_c / 100, taker=False)
        fh = pmus_fee_c((100 - h1) / 100, taker=True)
    else:                                             # dir K: rest YES ask r on P; hedge = YES@K at k_ya
        h0, h1 = cents(px_open.get("k_ya")), cents(px_fill.get("k_ya"))
        if h1 is None:
            return None
        gross, planned = level_c - h1, (level_c - h0 if h0 is not None else None)
        fm = pmus_fee_c(level_c / 100, taker=False)
        fh = kalshi_fee_c(h1 / 100, taker=True)
    slip = (planned - gross) if planned is not None else None
    return {"gross_c": gross, "planned_c": planned, "slip_c": slip,
            "fee_maker_c": round(fm, 4), "fee_hedge_c": round(fh, 4),
            "net_c": round(gross - fh - fm, 4)}

def maker_sim(by_market, censors, post_only=False):
    """Anchor at every hygienic weather OPEN; for each maker leg x {join, improve}, scan forward for the
    first definite crossing (full scan; strict variant stops at the resting venue's first censor).
    Returns (per-leg-mode results list, hygiene counter, maker-maker per-anchor list)."""
    out, skipped = [], collections.Counter()
    mm = []
    all_censors = sorted(censors["pm"] + censors["k"])
    for m, obs in by_market.items():
        for i, (t0, px0, rec) in enumerate([(t, p, r) for t, p, r in obs]):
            if rec.get("transition") != "OPEN" or rec.get("dir") not in ("P", "K"):
                continue
            if post_only and t0 < EPOCH_0013:
                skipped["pre_epoch"] += 1
                continue
            # hygiene (counted, never silent)
            j = bisect.bisect_left(all_censors, t0)
            if j > 0 and t0 - all_censors[j - 1] <= HYGIENE_POST_CENSOR_S:
                skipped["post_censor_window"] += 1
                continue
            if any(touches(px0, v)[k] is None for v in ("P", "K") for k in (0, 1)):
                skipped["incomplete_touches"] += 1
                continue
            pb, pa = touches(px0, "P"); kb, ka = touches(px0, "K")
            if abs((pb + pa) - (kb + ka)) / 2 > HYGIENE_MAX_MID_DIV_C:   # mid divergence (cents)
                skipped["mid_divergence"] += 1
                continue
            age = rec.get("age") or {}
            d = rec["dir"]
            anchor_legs = leg_specs(d)
            rest_age = {"P": age.get("p"), "K": age.get("k")}
            mm_legs = {}
            for venue, side in anchor_legs:
                if rest_age.get(venue) is not None and rest_age[venue] > HYGIENE_MAX_AGE_S:
                    skipped["stale_resting_book"] += 1
                    continue
                b0, a0 = touches(px0, venue)
                if b0 >= a0:
                    skipped["crossed_resting_book"] += 1
                    continue
                cv = censors["pm" if venue == "P" else "k"]
                for mode in ("join", "improve"):
                    level = (b0 if side == "bid" else a0) if mode == "join" else improve_level(side, b0, a0)
                    strict_stop = cv[bisect.bisect_right(cv, t0)] if bisect.bisect_right(cv, t0) < len(cv) else None
                    res = {"market": m, "t0": t0, "dir": d, "venue": venue, "side": side, "mode": mode,
                           "level_c": level, "epoch": "post" if t0 >= EPOCH_0013 else "pre",
                           "fill": None, "fill_strict": None, "dt": None, "touch_only": False}
                    for t1, px1, _ in obs[i + 1:]:
                        b1, a1 = touches(px1, venue)
                        r = fill_check(side, level, b1, a1)
                        if r == "touch" and not res["touch_only"]:
                            res["touch_only"] = True              # queue-ambiguous contact seen first
                        if r == "def":
                            res["fill"] = True
                            res["dt"] = round(t1 - t0, 3)
                            res["fill_strict"] = strict_stop is None or t1 <= strict_stop
                            res.update(hedge_eval(d, venue, side, level, px0, px1) or {"net_c": None})
                            break
                    if res["fill"] is None:
                        res["fill"] = False
                        res["censored_at"] = strict_stop          # strict scan would have stopped here
                    out.append(res)
                    if mode == "join":
                        mm_legs[venue] = res
            # maker-maker: both join legs of the signalled direction
            if len(mm_legs) == 2:
                legs = list(mm_legs.values())
                both = all(l["fill"] for l in legs)
                q = next(l for l in legs if l["side"] == "bid")   # cheap-venue YES bid
                r_ = next(l for l in legs if l["side"] == "ask")  # dear-venue YES ask
                rec_mm = {"market": m, "t0": t0, "dir": d, "epoch": "post" if t0 >= EPOCH_0013 else "pre",
                          "both_filled": both, "one_filled": sum(l["fill"] for l in legs) == 1}
                if both:
                    gross = r_["level_c"] - q["level_c"]
                    fees = leg_fee_c("P" if q["venue"] == "P" else "K", q["level_c"] / 100, True) + \
                           leg_fee_c("P" if r_["venue"] == "P" else "K", r_["level_c"] / 100, True)
                    rec_mm.update({"gross_c": gross, "fees_c": round(fees, 4),
                                   "net_c": round(gross - fees, 4),
                                   "unhedged_s": round(abs(q["dt"] - r_["dt"]), 3),
                                   "dt_last": max(q["dt"], r_["dt"])})
                mm.append(rec_mm)
    return out, skipped, mm


# ============================================================================================
# REPORT
# ============================================================================================
def _pct(xs, q):
    if not xs:
        return float("nan")
    xs = sorted(xs)
    k = (len(xs) - 1) * q / 100.0
    f = int(k)
    return xs[f] if f == len(xs) - 1 else xs[f] + (xs[f + 1] - xs[f]) * (k - f)

def summarize(args):
    P = print
    recs, censors = load_weather(args.data_dir)
    pxr = [r for r in recs if "px" in r]
    if args.post_only:
        pxr = [r for r in pxr if r["t"] >= EPOCH_0013]
    by_market = collections.defaultdict(list)
    for r in pxr:
        by_market[r["market"]].append((r["t"], r["px"], r))
    for m in by_market:
        by_market[m].sort(key=lambda x: x[0])

    P("=" * 96)
    P("MAKER-SIDE WEATHER EXECUTION STUDY  (read-only bounds from transition-sampled touches)")
    P("=" * 96)
    n_pre = sum(1 for r in pxr if r["t"] < EPOCH_0013)
    P(f"weather px observations: {len(pxr)} ({n_pre} pre-0013 / {len(pxr) - n_pre} post) across "
      f"{len(by_market)} markets; censor events: pm={len(censors['pm'])} k={len(censors['k'])}")
    P("LIMITATION (binding): touches are logged only AT EDGE-STATE TRANSITIONS - everything below is a")
    P("bound from sparse, activity-biased sampling, not a tick-level measurement.\n")

    # ---- 1. fees ----
    P("-" * 96)
    P("1. ROUND-TRIP FEES, cents per contract-PAIR (marginal; legs YES@p pmus + NO@(0.98-p) Kalshi).")
    P("   Kalshi weather = fee_type 'quadratic' (maker $0); pmus maker = -0.0125*p*(1-p) REBATE.")
    P("   Per-order note: booking adds Kalshi's per-order cent-ceil (<=1c/order, ~0.01c/contract at")
    P("   100-lot; detection/sizing stays marginal per L10/L15).")
    ft = fee_table()
    P(f"   {'yes_p':>6} {'no_p':>6} | {'taker-taker':>11} {'mkr@pmus+tkr@K':>14} {'tkr@pmus+mkr@K':>14} "
      f"{'maker-maker':>11} | {'MM mlb-series':>13}")
    for r in ft:
        P(f"   {r['yes_p']:>6.2f} {r['no_p']:>6.2f} | {r['taker_taker']:>11.3f} "
          f"{r['maker_pmus_taker_k']:>14.3f} {r['taker_pmus_maker_k']:>14.3f} "
          f"{r['maker_maker_weather']:>11.3f} | {r['maker_maker_mlb']:>13.3f}")
    P("   (dir K swaps venue roles; same formulas in p*(1-p), differences <0.05c at these prices)")

    opens = [r for r in pxr if r["transition"] == "OPEN"]
    nets = sorted(r["net_edge"] for r in opens if r.get("net_edge", 0) > 0)
    if nets:
        P(f"\n   baseline: weather taker-net edge at OPEN (this archive): median {_pct(nets,50)*100:.2f}c  "
          f"p75 {_pct(nets,75)*100:.2f}c  p90 {_pct(nets,90)*100:.2f}c  (n={len(nets)} OPENs)")

    # ---- 2. pegged crossing bound ----
    P("\n" + "-" * 96)
    P("2. MAKER FILL BOUNDS - definite level-crossings (opposing touch strictly THROUGH a join-the-")
    P("   touch rest placed at the previous observation; consecutive same-market obs, censor-clean).")
    cnt, mkt_any, span_days, mkts_obs = pegged_crossings(by_market, censors)
    tot_def = sum(c["def"] for c in cnt.values())
    P(f"   observed span: {span_days:.2f} market-days over {mkts_obs} markets (activity-biased denominator)")
    for (venue, side), c in sorted(cnt.items()):
        vn = "pmus " if venue == "P" else "Kalshi"
        P(f"   {vn} {side:<4} rest: {c['def']:>4} definite + {c['touch']:>3} touch-only in {c['pairs']:>5} pairs"
          f" ({c['censored']} censored)  -> {c['def'] / span_days if span_days else 0:>6.1f} definite/market-day")
    share = sum(1 for m in by_market if mkt_any.get(m, 0) > 0 and len(by_market[m]) >= 2)
    P(f"   TOTAL definite crossings: {tot_def} ({tot_def / span_days if span_days else 0:.1f}/observed market-day); "
      f"markets with >=1: {share}/{mkts_obs} ({100 * share / mkts_obs if mkts_obs else 0:.0f}%)")
    P("   LOWER bound only: a dip-through-and-back between observations is invisible; the upper bound")
    P("   is unobservable in transition-sampled data (needs the ladder/trade logging spec).")

    # ---- 3. episode-anchored sim ----
    P("\n" + "-" * 96)
    P("3. REST-AT-OPEN MAKER SIM + ADVERSE-SELECTION BOUND (anchor = each hygienic weather OPEN;")
    P("   resting leg scans forward for its first definite crossing; hedge leg priced taker AT the")
    P("   same dual-venue snapshot where the crossing first becomes visible - the true fill happened")
    P("   somewhere in the preceding gap, so dt UPPER-bounds time-to-fill and the slippage is the")
    P("   sampled post-move hedge state (the adverse-selection-inclusive read we want to bound).")
    P("   Pre-0013 stamps add <=1.5s noise to dt (negligible vs the minutes-hours horizons below);")
    P(f"   {100 * (len(pxr) - n_pre) / len(pxr) if pxr else 0:.0f}% of observations are post-0013 anyway.")
    sims, skipped, mm = maker_sim(by_market, censors, post_only=False)
    P(f"   anchors skipped by hygiene: {dict(skipped) or 'none'}")
    grouped = collections.defaultdict(list)
    for s in sims:
        grouped[(s["venue"], s["side"], s["mode"])].append(s)
    table = {}
    for key in sorted(grouped):
        g = grouped[key]
        fills = [s for s in g if s["fill"]]
        strict = [s for s in fills if s.get("fill_strict")]
        dts = [s["dt"] for s in fills]
        nets = [s["net_c"] for s in fills if s.get("net_c") is not None]
        slips = [s["slip_c"] for s in fills if s.get("slip_c") is not None]
        vn = "pmus" if key[0] == "P" else "Kalshi"
        P(f"   rest {vn:<6} {key[1]:<4} {key[2]:<8}: n={len(g):>3}  filled {len(fills):>3} "
          f"({100 * len(fills) / len(g) if g else 0:>4.0f}%; strict-censored {100 * len(strict) / len(g) if g else 0:>4.0f}%)"
          f"  dt med {_pct(dts, 50):>7.1f}s p90 {_pct(dts, 90):>8.1f}s")
        per_attempt = (sum(nets) / len(g)) if g and nets else None     # unfilled = cancel = 0c
        f1h = [s for s in fills if s["dt"] <= 3600 and s.get("net_c") is not None]   # capped-rest policy cut:
        n1h = [s["net_c"] for s in f1h]                                # a real maker cancels before bucket-death
        if nets:
            P(f"        hedge slippage med {_pct(slips,50):>5.2f}c p75 {_pct(slips,75):>5.2f}c p90 {_pct(slips,90):>5.2f}c | "
              f"net|fill med {_pct(nets,50):>5.2f}c mean {statistics.mean(nets):>5.2f}c p25 {_pct(nets,25):>5.2f}c  "
              f"+EV {100 * sum(1 for n in nets if n > 0) / len(nets):>3.0f}%  per-ATTEMPT {per_attempt:>5.2f}c (n={len(nets)})")
            P(f"        cancel-after-1h policy cut: fills {len(f1h)}/{len(g)} ({100 * len(f1h) / len(g):.0f}%)  "
              f"net|fill med {_pct(n1h,50):>5.2f}c mean {statistics.mean(n1h) if n1h else float('nan'):>5.2f}c  "
              f"per-ATTEMPT {sum(n1h) / len(g):>5.2f}c" if f1h else
              f"        cancel-after-1h policy cut: 0/{len(g)} fills within 1h")
        table[" ".join(map(str, key))] = {
            "n": len(g), "filled": len(fills), "fill_rate": round(len(fills) / len(g), 4) if g else None,
            "strict_fill_rate": round(len(strict) / len(g), 4) if g else None,
            "dt_med_s": round(_pct(dts, 50), 1) if dts else None,
            "dt_p90_s": round(_pct(dts, 90), 1) if dts else None,
            "slip_med_c": round(_pct(slips, 50), 3) if slips else None,
            "net_med_c": round(_pct(nets, 50), 3) if nets else None,
            "net_mean_c": round(statistics.mean(nets), 3) if nets else None,
            "per_attempt_ev_c": round(per_attempt, 3) if per_attempt is not None else None,
            "pos_ev_share": round(sum(1 for n in nets if n > 0) / len(nets), 3) if nets else None,
            "cancel_1h": {"fills": len(f1h), "fill_rate": round(len(f1h) / len(g), 4) if g else None,
                          "net_med_c": round(_pct(n1h, 50), 3) if n1h else None,
                          "per_attempt_ev_c": round(sum(n1h) / len(g), 3) if g and n1h else None}}

    both = [x for x in mm if x["both_filled"]]
    P(f"\n   MAKER-MAKER (both join legs of the signalled dir): anchors n={len(mm)}, both-legs filled "
      f"{len(both)} ({100 * len(both) / len(mm) if mm else 0:.0f}%), exactly-one (naked) "
      f"{sum(1 for x in mm if x['one_filled'])} ({100 * sum(1 for x in mm if x['one_filled']) / len(mm) if mm else 0:.0f}%)")
    if both:
        nets = [x["net_c"] for x in both]
        unh = [x["unhedged_s"] for x in both]
        P(f"        net|joint-fill (spread-to-spread gross - fees): med {_pct(nets,50):.2f}c p25 {_pct(nets,25):.2f}c "
          f"p90 {_pct(nets,90):.2f}c   NOTE: positivity is BY CONSTRUCTION (a joint fill locks the")
        P(f"        open gap + both spreads; fees are negative) - the information is the 32% joint RATE,")
        P(f"        the {100 * sum(1 for x in mm if x['one_filled']) / len(mm) if mm else 0:.0f}% one-leg-naked rate "
          f"(unpriced directional tail; in practice the taker-hedge fallback above), and")
        P(f"        the unhedged window: med {_pct(unh,50):.0f}s p90 {_pct(unh,90):.0f}s between leg fills.")

    # ---- dump ----
    if not args.no_dump:
        os.makedirs(os.path.join(HERE, "_data"), exist_ok=True)
        out = os.path.join(HERE, "_data", "maker_feasibility_summary.json")
        with open(out, "w", encoding="utf-8") as f:
            json.dump({"generated_from": "scripts/maker_feasibility.py", "post_only": args.post_only,
                       "n_px_obs": len(pxr), "n_markets": len(by_market),
                       "span_market_days": round(span_days, 3),
                       "fee_table_c": ft,
                       "taker_net_open_median_c": round(_pct([r["net_edge"] for r in opens if r.get("net_edge", 0) > 0], 50) * 100, 3) if opens else None,
                       "pegged_crossings": {f"{v}{s}": dict(c) for (v, s), c in cnt.items()},
                       "rest_at_open": table,
                       "maker_maker": {"n": len(mm), "both": len(both),
                                       "naked_one_leg": sum(1 for x in mm if x["one_filled"]),
                                       "net_med_c": round(_pct([x["net_c"] for x in both], 50), 3) if both else None},
                       "hygiene_skips": dict(skipped)}, f, indent=1)
        P(f"\n   numeric dump -> {out}")


# ============================================================================================
# SELF-TEST (offline; no data dir needed)
# ============================================================================================
def _selftest():
    print("maker_feasibility self-test")
    # fees: pinned coefficients through the per-series fee_type model
    assert abs(kalshi_fee_c(0.5, taker=True) - 1.75) < 1e-9                      # 0.07*.25
    assert kalshi_fee_c(0.5, taker=False) == 0.0                                 # weather maker $0
    assert abs(kalshi_fee_c(0.5, taker=False, fee_type=FEE_TYPE_MAKERFEE) - 0.4375) < 1e-9
    assert abs(pmus_fee_c(0.5, taker=True) - 1.25) < 1e-9                        # 0.05*.25
    assert abs(pmus_fee_c(0.5, taker=False) + 0.3125) < 1e-9                     # REBATE
    ft = fee_table()
    r50 = next(r for r in ft if r["yes_p"] == 0.50)
    assert abs(r50["taker_taker"] - (1.25 + 0.07 * 0.48 * 0.52 * 100)) < 1e-6    # ~2.998c wall
    assert abs(r50["maker_maker_weather"] + 0.3125) < 1e-6                       # ~-0.31c (net rebate)
    assert r50["maker_maker_mlb"] > 0                                            # contrast series pays
    print(f"  OK fees: TT@.50 {r50['taker_taker']:.3f}c  MM weather {r50['maker_maker_weather']:.3f}c  "
          f"MM mlb {r50['maker_maker_mlb']:.3f}c")
    # crossing detection
    assert fill_check("bid", 50, None, 49) == "def" and fill_check("bid", 50, None, 50) == "touch"
    assert fill_check("bid", 50, None, 51) is None and fill_check("bid", 50, None, None) is None
    assert fill_check("ask", 52, 53, None) == "def" and fill_check("ask", 52, 52, None) == "touch"
    assert improve_level("bid", 40, 42) == 41 and improve_level("bid", 40, 41) == 40   # 1-tick spread -> join
    assert improve_level("ask", 40, 42) == 41 and improve_level("ask", 40, 41) == 41
    assert censored_between([100.0], 50, 150) and not censored_between([100.0], 100, 150)
    assert not censored_between([100.0], 50, 99)
    print("  OK crossings: strict-through=def, ==touch, 1-tick improve degenerates to join, censor (t0,t1]")
    # hedge math, dir P, maker on pmus bid: q=40, open k_yb=45, fill obs k_yb=44
    px0 = {"p_yb": 0.40, "p_ya": 0.42, "k_yb": 0.45, "k_ya": 0.47}
    px1 = {"p_yb": 0.37, "p_ya": 0.39, "k_yb": 0.44, "k_ya": 0.46}
    h = hedge_eval("P", "P", "bid", 40, px0, px1)
    assert h["gross_c"] == 4 and h["planned_c"] == 5 and h["slip_c"] == 1
    assert abs(h["fee_maker_c"] + 0.0125 * 0.40 * 0.60 * 100) < 1e-9             # rebate at q
    assert abs(h["fee_hedge_c"] - 0.07 * 0.56 * 0.44 * 100) < 1e-9               # K taker at 1-k_yb=0.56
    assert abs(h["net_c"] - (4 - 0.07 * 0.56 * 0.44 * 100 + 0.0125 * 0.4 * 0.6 * 100)) < 1e-3
    # dir P, maker on Kalshi ask r=47: hedge = YES@P taker at p_ya
    h2 = hedge_eval("P", "K", "ask", 47, px0, px1)
    assert h2["gross_c"] == 47 - 39 and h2["fee_maker_c"] == 0.0                 # weather K maker $0
    assert abs(h2["fee_hedge_c"] - 0.05 * 0.39 * 0.61 * 100) < 1e-9
    print("  OK hedge eval: gross/planned/slip + rebate & $0-maker fees wired to the right legs")
    # end-to-end mini sim: dir P OPEN, P-bid join fills on the 2nd obs (ask 0.39 < bid level 0.40)
    obs = [(100.0, px0, {"transition": "OPEN", "dir": "P", "net_edge": 0.02, "age": {"p": 0.1, "k": 0.2}}),
           (160.0, px1, {"transition": "NARROW", "dir": "P", "net_edge": 0.01})]
    sims, skipped, mm = maker_sim({"tc-temp-test-2026-06-10-gte70lt71f": obs}, {"pm": [], "k": []})
    pj = next(s for s in sims if s["venue"] == "P" and s["mode"] == "join")
    assert pj["fill"] and pj["dt"] == 60.0 and pj["gross_c"] == 4
    kj = next(s for s in sims if s["venue"] == "K" and s["mode"] == "join")
    assert kj["fill"] is False                                                   # k_yb fell, never rose >47
    assert len(mm) == 1 and mm[0]["one_filled"] and not mm[0]["both_filled"]     # naked maker-maker leg
    # hygiene: an anchor 30s after a censor event is skipped
    sims2, skipped2, _ = maker_sim({"m": obs}, {"pm": [70.0], "k": []})
    assert skipped2["post_censor_window"] >= 1 and not sims2
    print("  OK rest-at-OPEN sim: P-bid join fill @60s gross 4c; K-ask unfilled; MM naked; censor hygiene")
    # pegged crossings consume the same (t, px, rec) triples the sim does
    pc, any_, days, nm = pegged_crossings({"m": obs}, {"pm": [], "k": []})
    assert pc[("P", "bid")]["def"] == 1 and pc[("K", "ask")]["def"] == 0   # ask .39 < bid level .40 only
    assert nm == 1 and abs(days - 60.0 / 86400) < 1e-9 and any_["m"] == 1
    pc2, _, _, _ = pegged_crossings({"m": obs}, {"pm": [130.0], "k": []})  # pm censor between the pair
    assert pc2[("P", "bid")]["censored"] == 1 and pc2[("P", "bid")]["def"] == 0
    print("  OK pegged crossings: P-bid definite counted, span/market flags, pm-censor drops the pair")
    print("all self-tests passed")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--data-dir", default=DATA_DIR)
    ap.add_argument("--post-only", action="store_true", help="restrict to post-0013 records (clean stamps)")
    ap.add_argument("--no-dump", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        _selftest()
    else:
        summarize(args)
