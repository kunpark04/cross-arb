"""scripts/venue_split_backtest.py - honest total-return backtest under a PER-VENUE capital split.

THE QUESTION (owner, 2026-06-13): with $250 on Kalshi + $250 on polymarket.us, what total return does the
strategy produce on ALL the data we have?

WHY A PER-VENUE MODEL (not one $500 pool): a cross-venue arb deploys capital on BOTH venues at once - the
YES leg on one, the NO leg on the other. With $250/$250 the BINDING constraint is whichever venue's pool
fills first, and leg prices are asymmetric (a 95c-YES + 3c-NO arb drains the YES venue ~30x faster). So a
$250/$250 split can bind harder than a single $500 pool, and one pool can sit idle while the other is full.
This models the two pools explicitly (per-leg costs reconstructed from the at-open px touches).

WHAT "HONEST" FORCES (read before the number):
  * DATA = ALL available days (Jun 9-13). load() quarantines ONLY the pre-epoch threshold-ECON off-by-one
    phantom (L21/0013), NOT pre-epoch weather/sports - so Jun 9 IS included (its durations carry the
    pre-0013 debounce-stamp inflation). Flat-ladder book-init phantoms (c2==c1==c0, L20) are dropped.
  * EFFECTIVE-N ~ 1 (~91% of arbs on one event-date). PAPER/GROSS: fees+spread are in net_edge; latency,
    leg-fill-failure (~55% naked @1s), slippage, adverse-selection NOT netted -> realized would be LOWER.
    The number is a method-demo, NOT a validated return. Do not size off it.
  * TWO universes, both reported: WEATHER-ONLY = what the bot can actually trade TODAY (sports/econ
    settlement-unverified until recon ~Jun 23 / Jul 2); ALL-VERIFIED = the full strategy, a POST-RECON
    PREVIEW. The honest "total return" is the weather-only one today; all-verified is the aspiration.
  * Strategy gates applied = the bot's live stack INCLUDING the now-live velocity gates (proximity 2d +
    edge-rate 1.0, decision 0017 - inert here). The staged-rollout SIZE caps ($1/pair, $20 total, 1
    ctr/pair, 5-concurrent) are LIFTED - else $20 binds and the $250/$250 split is moot; the run is bound
    by the per-venue bankroll + crossable depth. Concentration is reported so that lift is visible.

REUSES the proven harness ([L20]). Does NOT modify backtest_current_strategy.py / account_sim / 0014 scripts.
READ-ONLY. `--selftest` is offline.

  python scripts/venue_split_backtest.py --selftest
  python scripts/venue_split_backtest.py [--data-dir PATH] [--kalshi 250] [--pmus 250]
"""
import os, sys, argparse, glob, datetime as dt

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from analyze_persistence import load, build_episodes
from capital_sim import settle_t, void_haircut, DEPTH_BOUNDARY_NET
from backtest_current_strategy import (
    BotConfig, SETTLE_VERIFIED_NOW, SETTLE_VERIFIED_ALL, run_funnel,
    open_direction_index, open_px_index, cohort_edge_stats, _fmt_catmix, _catmix, _eff_n_days,
)
from velocity_gate_experiment import passes_proximity, passes_edge_rate, PROX_DAYS

MAX_CLIP = 1000
LIVE_EDGE_RATE = 1.0          # the now-live MIN_EDGE_RATE_CPD (decision 0017, enabled 2026-06-13)
SETTLE_OFFSET_H = 28.0
SEP = "=" * 92


# ---- per-leg cost split from the at-open px touches ----------------------------------------------
def leg_split(e, px):
    """Kalshi's SHARE of the per-contract cost (0..1). The arb buys YES on the cheap venue + NO on the
    dear one; cost of YES@V = V's YES ask, cost of NO@V = 1 - V's YES bid. We take the ratio of the two
    venue leg costs and apply it to the episode's fee-netted cost_per (so totals stay consistent with the
    rest of the harness). 0.5 fallback when px / a needed touch is missing. dir: PK = cheap pmus / dear
    kalshi; KP = cheap kalshi / dear pmus."""
    if not px:
        return 0.5
    # Direction is logged as a SINGLE letter for binary (weather/econ): "P" = buy YES on pmus (cheap pmus),
    # "K" = buy YES on Kalshi; and TWO letters for sports: "PK" = teamA@pmus, "KP" = teamA@Kalshi. Both
    # forms collapse to "is pmus the cheap (YES) side?" (audit CRITICAL: the old `d=="PK"` test sent every
    # single-letter "P" binary leg into the wrong branch).
    cheap_pmus = e.get("dir") in ("P", "PK")
    if "pm_a" in px or "pm_b" in px or "ka" in px or "kb" in px:        # sports shape
        if cheap_pmus:         # PK: buy YES teamA @ pmus (pm_a), buy YES teamB @ kalshi (kb)
            k, p = px.get("kb"), px.get("pm_a")
        else:                  # KP: buy YES teamA @ kalshi (ka), buy NO pmus = YES teamB (1 - pm_b)
            k, p = px.get("ka"), (None if px.get("pm_b") is None else 1.0 - px["pm_b"])
    else:                                                              # binary (weather/econ)
        if cheap_pmus:         # P: buy YES @ pmus (p_ya), buy NO @ kalshi (1 - k_yb)
            p = px.get("p_ya")
            k = None if px.get("k_yb") is None else 1.0 - px["k_yb"]
        else:                  # K: buy YES @ kalshi (k_ya), buy NO @ pmus (1 - p_yb)
            k = px.get("k_ya")
            p = None if px.get("p_yb") is None else 1.0 - px["p_yb"]
    if k is None or p is None or (k + p) <= 0:
        return 0.5
    return min(1.0, max(0.0, k / (k + p)))


# ---- the two-pool capital walk -------------------------------------------------------------------
def two_pool_walk(cohort, kalshi0, pmus0, px_idx, void_mult, window_end):
    """Walk arrivals; each arb deducts its kalshi leg $ from the Kalshi pool + its pmus leg $ from the pmus
    pool; both freed at settlement. Size bound ONLY by per-venue bankroll + crossable depth (staged-rollout
    size/concurrency caps lifted). Returns (entered, peak_k_deployed, peak_p_deployed)."""
    kcash, pcash = float(kalshi0), float(pmus0)
    held, entered = [], []
    peak_k = peak_p = 0.0

    def settle_due(now):
        nonlocal kcash, pcash
        still = []
        for p in held:
            if p["settle_t"] <= now:
                kcash += p["kcost"]; pcash += p["pcost"]
            else:
                still.append(p)
        held[:] = still

    for e in sorted(cohort, key=lambda x: x["open_t"]):
        settle_due(e["open_t"])
        cost_per = max(0.1, 1.0 - e["open_net"])
        kfrac = leg_split(e, px_idx.get((e["market"], e["open_t"])))
        k_unit = cost_per * kfrac                 # $ per contract tied up on Kalshi
        p_unit = cost_per * (1.0 - kfrac)         # $ per contract tied up on pmus
        size = e["open_c2"]                       # crossable displayed depth
        if k_unit > 0:
            size = min(size, int(kcash // k_unit))
        if p_unit > 0:
            size = min(size, int(pcash // p_unit))
        if size < 1:
            continue                              # a venue pool can't fund even 1 contract
        # fat-edge haircut (risk.rs step 6): above the knee, fat edges are ~66% toxic -> size DOWN (the bot
        # does this; omitting it would inflate concentrated fat-sports positions). Floored at 1, never zeroed.
        if e["open_net"] * 100.0 >= BotConfig.fat_edge_knee_cents and BotConfig.fat_edge_size_factor < 1.0:
            size = max(1, int(size * BotConfig.fat_edge_size_factor))
        avg_edge = max(DEPTH_BOUNDARY_NET, (e["open_net"] + DEPTH_BOUNDARY_NET) / 2.0)  # book-avg (trapezoid)
        profit = size * max(0.0, avg_edge - void_haircut(e["market"], void_mult))
        kcost, pcost = size * k_unit, size * p_unit
        kcash -= kcost; pcash -= pcost
        peak_k = max(peak_k, kalshi0 - kcash); peak_p = max(peak_p, pmus0 - pcash)
        held.append({"settle_t": settle_t(e["market"], e["open_t"], SETTLE_OFFSET_H),
                     "kcost": kcost, "pcost": pcost, "profit": profit, "market": e["market"],
                     "cat": e["cat"], "size": size, "open_t": e["open_t"], "capital": kcost + pcost})
        entered.append(held[-1])
    return entered, peak_k, peak_p


def _live_cohort(eps, cfg, settle_verified, dir_idx, px_idx, capital0):
    """The bot's live tradeable cohort (full risk.rs stack) THEN the now-live velocity gates (prox 2d +
    edge-rate 1.0). run_funnel gives the post settlement+mid-div+2c-floor+H1 cohort; we add the velocity gates."""
    stages, _ = run_funnel(eps, cfg, settle_verified, dir_idx, px_idx, MAX_CLIP, 0.0, 1.0, capital0)
    base = stages["directional"]
    # DROP flat-ladder book-init phantoms (c2==c1==c0, [L20]): the stats audit found the top-2 all-verified
    # "positions" were 0-duration flat ITF arbs carrying ~57% of the headline PnL. run_funnel's `capturable`
    # drops restart-censored but NOT flat (drop_flat default off), so we drop them here. Then the live
    # velocity gates (proximity 2d + edge-rate 1.0).
    return [e for e in base if (not e.get("open_flat"))
            and passes_proximity(e, PROX_DAYS) and passes_edge_rate(e, LIVE_EDGE_RATE)]


# ---- report --------------------------------------------------------------------------------------
def build_report(eps, recs, sessions, cfg, kalshi0, pmus0, data_dir):
    L = []; P = L.append
    total0 = kalshi0 + pmus0
    t0, t1 = recs[0]["t"], recs[-1]["t"]
    span_d = (t1 - t0) / 86400.0
    window_end = t1
    dir_idx, _ = open_direction_index(recs, sessions)
    px_idx = open_px_index(recs)

    P(SEP)
    P("CROSS-ARB - VENUE-SPLIT BACKTEST  ($%.0f Kalshi / $%.0f polymarket.us)" % (kalshi0, pmus0))
    P(SEP)
    P(f"generated : {dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')}")
    P(f"data dir  : {data_dir}")
    days = sorted({dt.datetime.utcfromtimestamp(r["t"]).strftime("%m-%d") for r in recs})
    P(f"span      : {span_d:.2f} d   transitions {len(recs)}   restarts {len(sessions)}")
    P(f"data days : {', '.join(days)}  (ALL available; load() quarantines only the pre-epoch THRESHOLD-ECON")
    P("            off-by-one phantoms, NOT pre-epoch weather/sports - so Jun 9 IS included; its durations")
    P("            carry the pre-0013 debounce-stamp inflation. Flat-ladder phantoms dropped, L20.)")
    P("")
    P("*** HONEST READ FIRST - the number is a method-demo, not a validated return: ***")
    P(f"  * EFFECTIVE-N ~ 1-2 ({span_d:.1f} d; arbs cluster on a couple of event-dates). PAPER/GROSS (fees+")
    P("    spread in net_edge; latency / ~55%-naked-leg-@1s / slippage / adverse-selection NOT netted -> LOWER).")
    P("  * Staged-rollout SIZE caps LIFTED (else the $20 total cap binds and $250/$250 is moot); bound by the")
    P("    per-venue bankroll + crossable depth. Strategy gates KEPT incl. the now-live velocity gates")
    P(f"    (proximity {PROX_DAYS:.0f}d + edge-rate {LIVE_EDGE_RATE:.1f}c/$-day, 0017 - inert on this data).")
    P("  * WEATHER-ONLY = tradeable TODAY (sports/econ unverified until recon). ALL-VERIFIED = post-recon preview.")

    def section(title, settle_verified):
        cohort = _live_cohort(eps, cfg, settle_verified, dir_idx, px_idx, total0)
        ce = cohort_edge_stats(cohort)
        entered, peak_k, peak_p = two_pool_walk(cohort, kalshi0, pmus0, px_idx, 1.0, window_end)
        realized = sum(p["profit"] for p in entered if p["settle_t"] <= window_end)
        unreal = sum(p["profit"] for p in entered if p["settle_t"] > window_end)
        locked = sum(p["capital"] for p in entered if p["settle_t"] > window_end)
        big = max((p["capital"] for p in entered), default=0.0)
        P(""); P(SEP); P(title); P(SEP)
        P(f"  tradeable cohort (one/market, full stack + velocity gates): {ce['n']}  ({_fmt_catmix(ce['by_cat'])})")
        P(f"  effective event-days: {_eff_n_days(cohort)}")
        P(f"  ENTERED positions   : {len(entered)}  ({_fmt_catmix(_catmix(entered))})")
        P(f"  peak deployed       : Kalshi ${peak_k:.2f}/{kalshi0:.0f} ({100*peak_k/kalshi0:.0f}%)  |  "
          f"pmus ${peak_p:.2f}/{pmus0:.0f} ({100*peak_p/pmus0:.0f}%)")
        P(f"  largest single pos  : ${big:.2f} ({100*big/total0:.0f}% of ${total0:.0f}) - concentration check")
        P("")
        P(f"  >>> TOTAL RETURN on ${total0:.0f}:  REALIZED ${realized:.2f} ({100*realized/total0:+.2f}%)")
        P(f"      + unrealized (mark-to-edge, still open) ${unreal:.2f} ({100*unreal/total0:+.2f}%); "
          f"capital still locked ${locked:.2f}")
        return {"realized": realized, "ret_pct": 100 * realized / total0, "entered": len(entered),
                "peak_k": peak_k, "peak_p": peak_p, "big": big}

    wx = section("A.  WEATHER-ONLY  (what the bot can trade TODAY)", SETTLE_VERIFIED_NOW)
    al = section("B.  ALL-VERIFIED  (the full strategy - POST-RECON PREVIEW)", SETTLE_VERIFIED_ALL)

    P(""); P(SEP); P("C.  BOTTOM LINE"); P(SEP)
    P(f"  TODAY (weather-only, tradeable now) : {wx['ret_pct']:+.2f}% realized on ${total0:.0f} "
      f"({wx['entered']} positions; only ~{100*max(wx['peak_k'],wx['peak_p'])/(total0/2):.0f}% of a venue "
      f"pool deployable - too few real arbs to fill $250/$250)")
    P(f"  POST-RECON preview (all-verified)   : {al['ret_pct']:+.2f}% realized on ${total0:.0f} "
      f"({al['entered']} positions, but the largest is ~{100*al['big']/total0:.0f}% of capital - "
      f"concentration, not a portfolio)")
    P(f"  over ~{span_d:.1f} d, effective-n ~ 1-2, PAPER/GROSS, sports settlement UNVERIFIED.")
    P("  THREE caveats dominate the number: (1) most apparent edges were FLAT-LADDER book-init PHANTOMS")
    P("  (cohort collapsed 16->5 weather / 57->24 sports once [L20] flat-drop is applied); (2) what remains")
    P("  is 1-2 CONCENTRATED bets, not a diversified book (the bot's real per-pair/cluster caps - lifted")
    P("  here - would forbid that concentration and shrink the number further); (3) GROSS of latency /")
    P("  ~55%-naked-leg-@1s / slippage / void. NOT annualizable, NOT validated. The weather +1.39% (settled,")
    P("  verified, less concentrated) is the only semi-trustworthy figure; +4.55% leans on unverified sports.")
    P("  NEXT: re-run on the multi-week post-recon data the 0014 protocol requires.")
    P(SEP)
    return "\n".join(L)


# ---- selftest ------------------------------------------------------------------------------------
def _selftest():
    print("venue-split-backtest self-test")

    def ep(mkt, cat, t, net, c2=200, dir_="PK"):
        return {"market": mkt, "cat": cat, "open_t": t, "open_net": net, "open_c2": c2, "peak_c2": c2,
                "duration": 100, "censored": "none", "dir": dir_, "close_t": t + 100, "open_age": 0,
                "open_flat": False}

    # --- leg_split: BINARY dirs are single letters "P"/"K" (audit CRITICAL was that "P" mis-branched). ---
    # dir "P" = cheap pmus: pmus pays YES ask 0.07, kalshi pays NO ask 1-0.86=0.14 -> kalshi share 2/3.
    e = ep("tc-temp-x-2026-06-10-gte70f", "weather", 100, 0.05, dir_="P")
    px = {"p_yb": 0.05, "p_ya": 0.07, "k_yb": 0.86, "k_ya": 0.88}
    f = leg_split(e, px)
    assert abs(f - (0.14 / 0.21)) < 1e-9, f                          # kalshi 0.14 of 0.21 total ~ 0.667
    assert leg_split(e, None) == 0.5                                 # missing px -> 50/50
    # dir "K" = cheap kalshi: kalshi pays YES ask 0.07, pmus pays NO ask 1-0.86=0.14 -> kalshi share 1/3.
    # (The first `e` assertion alone catches the old bug: under the mis-branch dir "P" would give ~0.48, not 0.667.)
    e_k = ep("tc-temp-x-2026-06-10-gte70f", "weather", 100, 0.05, dir_="K")
    fk = leg_split(e_k, {"p_yb": 0.86, "p_ya": 0.88, "k_yb": 0.05, "k_ya": 0.07})
    assert abs(fk - (0.07 / 0.21)) < 1e-9, fk                        # kalshi pays the cheap 0.07 leg
    # sports shape: PK pmus pays pm_a, kalshi pays kb
    es = ep("aec-mlb-lad-pit-2026-06-10", "sports", 100, 0.04)
    fs = leg_split(es, {"pm_a": 0.55, "pm_b": 0.53, "ka": 0.46, "kb": 0.44})
    assert abs(fs - (0.44 / (0.44 + 0.55))) < 1e-9, fs
    print("  OK - leg_split: binary PK/KP + sports, asymmetric venue shares, 50/50 fallback")

    # --- per-venue BINDING: a lopsided book (95c kalshi leg) exhausts the Kalshi pool while pmus sits idle ---
    # 3 arbs, each cost_per ~0.97 split 95% kalshi / 5% pmus; Kalshi $2 funds ~2 contracts, pmus $200 idle.
    lop = [ep(f"tc-temp-c{i}high-2026-06-10-gte70f", "weather", 100 + i, 0.03, c2=100) for i in range(3)]
    px_lop = {(e["market"], e["open_t"]): {"p_yb": 0.02, "p_ya": 0.03, "k_yb": 0.06, "k_ya": 0.08} for e in lop}
    # PK: pmus pays p_ya=0.03, kalshi pays 1-0.06=0.94 -> kalshi ~0.97 share
    ent, pk, pp = two_pool_walk(lop, 2.0, 200.0, px_lop, 1.0, 10**12)
    deployed_k = sum(p["kcost"] for p in ent)
    assert deployed_k <= 2.0 + 1e-9, deployed_k                      # Kalshi pool is the hard ceiling
    assert pp < 0.5 * 200.0, pp                                      # pmus pool barely touched (idle) - the asymmetry
    assert pk <= 2.0 + 1e-9
    print(f"  OK - per-venue binding: Kalshi $2 caps deployment (${deployed_k:.2f}) while pmus $200 sits idle "
          f"(peak ${pp:.2f}) - the $250/$250 asymmetry the model exists to show")

    # --- settlement frees BOTH pools -> a later arb can reuse the freed capital ---
    import calendar
    mid = lambda y, mo, d: float(calendar.timegm((y, mo, d, 0, 0, 0, 0, 0, 0)))
    # A opens 06-09 (settles 06-10 +28h); B opens 06-12 -- AFTER A's settlement -> reuses the freed pool.
    seq = [ep("tc-temp-ahigh-2026-06-10-gte70f", "weather", mid(2026, 6, 9), 0.03, c2=1),
           ep("tc-temp-bhigh-2026-06-13-gte70f", "weather", mid(2026, 6, 12), 0.03, c2=1)]
    px_seq = {(e["market"], e["open_t"]): {"p_yb": 0.45, "p_ya": 0.49, "k_yb": 0.45, "k_ya": 0.49} for e in seq}
    ent2, _, _ = two_pool_walk(seq, 0.6, 0.6, px_seq, 1.0, mid(2026, 7, 1))  # ~0.5/ctr/side; pool funds 1 at a time
    assert len(ent2) == 2, [p["market"] for p in ent2]              # 2nd funded only because the 1st settled+freed
    print("  OK - settlement recycles both pools (sequential arbs reuse freed capital)")

    print("self-test passed.")


# ---- main ----------------------------------------------------------------------------------------
if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="honest total-return backtest under a per-venue $/$ split")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--data-dir", default=os.path.join(os.path.dirname(__file__), "..", "..", "data", "cross-arb"))
    ap.add_argument("--kalshi", type=float, default=250.0, help="Kalshi-side bankroll $ (default 250)")
    ap.add_argument("--pmus", type=float, default=250.0, help="polymarket.us-side bankroll $ (default 250)")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    if a.selftest:
        _selftest(); sys.exit(0)
    dd = os.path.abspath(a.data_dir)
    if not glob.glob(os.path.join(dd, "transitions-*.jsonl*")):
        print(f"no data at {dd} - run `pwsh deploy/pull-data.ps1` first, or `--selftest`."); sys.exit(0)
    recs, sessions = load(dd)
    eps = build_episodes(recs, sessions)
    report = build_report(eps, recs, sessions, BotConfig, a.kalshi, a.pmus, dd)
    print(report)
    if a.out:
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(report + "\n")
        print(f"\n[written to {a.out}]")
