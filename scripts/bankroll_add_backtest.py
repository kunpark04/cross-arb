"""scripts/bankroll_add_backtest.py - the TRUE bankroll-netted marginal value of SCALE-IN / RE-ENTRY adds.

THE QUESTION (owner, 2026-06-14): flip_add_backtest.add_events sums each add's profit with NO bankroll
constraint (~$98.69 all / $22.69 true scale-in, paper-gross UPPER BOUND - every qualifying widen funded at
its full per-pair cap, as if capital were free). That is NOT the decision-useful number. Under the project's
SEPARATED-WALLET model ($250 Kalshi / $250 polymarket.us, non-shareable - memory `venue-wallets-separated`),
an add deploys a YES leg + a NO leg ACROSS the two venues, exactly like a base arb, so it COMPETES with the
base strategy for BOTH pools and often can't be funded. The headline is the DELTA vs base: the REAL marginal
$ from adding, netted against real capital.

THE MODEL (one CAUSAL two-pool walk; base entries + add attempts share the SAME two pools):
  * BASE  : one position per market, deployed on BOTH pools (YES one venue / NO the other), held to
            settlement (capital_sim.settle_t), capital freed back to each pool at settlement. This is
            venue_split_backtest's existing reference walk, item-for-item.
  * ADD   : when flip_add_backtest.add_events (drop_flat_widen=True, the [L20] phantom lens) yields a
            qualifying bigger SAME-DIRECTION widen on a still-held bucket, ATTEMPT a SECOND pair at the
            widen time - deploy ONLY if BOTH pools have residual room for >=1 contract of its two legs;
            else SKIP (capital-constrained, COUNTED). The add's capital frees at ITS settlement.
  * Events are processed in chronological (open-time) order so pool state is causal - a base pair opens
            before its own add's widen (add_events requires widen_t >= open_t), and an add only ever
            consumes the pool room the base entries left behind.

FOUR scenarios x TWO cohorts (mirrors venue_split's weather-only / all-verified universes):
  A. BASE only (the reference)         B. BASE + TRUE scale-in (is_true_add=True)
  C. BASE + RE-ENTRY (is_true_add=False)   D. BASE + BOTH
The DELTA (B/C/D realized minus A realized) IS the bankroll-netted marginal contribution of the adds.

REUSES the proven harness ([L20]/[L28] - never re-derive a "clean" cohort or re-implement economics):
  venue_split_backtest : _live_cohort (capturable + flat-drop + velocity gates), leg_split (the [L28]
                         single-letter P/K vs two-letter PK/KP trap), SETTLE_OFFSET_H, the per-leg->pool split.
  flip_add_backtest    : add_events(drop_flat_widen=True) - the same-direction-widen detection, the
                         is_true_add vs re-entry classification, the [L20] flat-ladder lens on the widen.
  capital_sim          : settle_t, void_haircut, DEPTH_BOUNDARY_NET (book-avg trapezoid economics).
  backtest_current_strategy : open_px_index (the at-open touches for the base leg_split).

HONEST (PAPER/GROSS - read before any number): fees+spread are in net_edge; latency, the ~17-55% naked-leg
risk, slippage, adverse-selection, the sports settlement-void TAIL are NOT netted -> realized would be LOWER.
Effective-n is TINY (~5 event-dates carry it), 5-day window, World Cup nearly absent (deployed only today).
A method demo, NOT a validated return. The add DELTA is the deliverable; do not size off the level.

Does NOT modify the bot or any frozen 0014 script. READ-ONLY. `--selftest` is offline.

  python scripts/bankroll_add_backtest.py --selftest
  python scripts/bankroll_add_backtest.py [--data-dir PATH] [--kalshi 250] [--pmus 250] [--add-cap-frac 0.20]
"""
import os, sys, argparse, glob, datetime as dt

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from analyze_persistence import load, build_episodes
from capital_sim import settle_t, void_haircut, DEPTH_BOUNDARY_NET
from backtest_current_strategy import (
    BotConfig, SETTLE_VERIFIED_NOW, SETTLE_VERIFIED_ALL, open_direction_index, open_px_index,
    _fmt_catmix, _catmix, _eff_n_days,
)
from venue_split_backtest import leg_split, _live_cohort, SETTLE_OFFSET_H
from flip_add_backtest import add_events

MAX_CLIP = 1000
TAU_GAIN = 0.01              # min net-edge GAIN for a widen to qualify as an add (flip_add_backtest default)
ADD_CAP_FRAC = 0.20         # per-pair add size cap as a fraction of the base clip (flip_add default)
SEP = "=" * 92


def _item_economics(dir_, net, px, market):
    """The per-CONTRACT pool draws + per-contract realized edge for one leg-pair (base OR add), priced the
    SAME way venue_split's two_pool_walk prices a base arb. Returns (k_unit, p_unit, edge_per):
      k_unit / p_unit = $ tied up on Kalshi / pmus per contract (cost_per split by leg_split's venue share);
      edge_per        = book-avg realized edge per contract = max(0, avg_edge - void), avg_edge the trapezoid
                        (touch..2c-boundary) decay capital_sim/account use. dir_/px feed leg_split (the [L28]
                        single-letter binary vs two-letter sports trap); px=None -> 50/50 fallback (handled)."""
    cost_per = max(0.1, 1.0 - net)
    kfrac = leg_split({"dir": dir_}, px)                  # leg_split reads only e["dir"] + the px touches
    k_unit = cost_per * kfrac
    p_unit = cost_per * (1.0 - kfrac)
    avg_edge = max(DEPTH_BOUNDARY_NET, (net + DEPTH_BOUNDARY_NET) / 2.0)
    edge_per = max(0.0, avg_edge - void_haircut(market))
    return k_unit, p_unit, edge_per


def two_pool_walk_with_adds(base_cohort, adds, px_idx, kalshi0, pmus0):
    """ONE causal two-pool walk over base entries + add attempts sharing the SAME two pools.

    base_cohort : the venue_split _live_cohort (one episode/market). Each becomes a BASE entry at open_t,
                  sized greedily min(open_c2, kcash//k_unit, pcash//p_unit) - venue_split's exact rule.
    adds        : add_events dicts (already filtered to base markets + the requested true/re-entry class),
                  each an ADD attempt at widen_t, sized min(add_cap, kcash//k_unit, pcash//p_unit) where
                  add_cap = min(c2_add, MAX_CLIP, round(ADD_CAP_FRAC*size0)) is the add policy's per-pair
                  cap - deployed iff BOTH pools fund >=1 contract, else SKIPPED (counted).

    Both kinds deduct their Kalshi leg from kcash + their pmus leg from pcash; BOTH freed at their own
    settle_t. Returns a dict with realized/unrealized $, peak deploy per pool, entered/add counts, and the
    add fund/skip tallies. window_end = the realized/unrealized cut (latest base open's settlement >= now)."""
    kcash, pcash = float(kalshi0), float(pmus0)
    held, base_entered, add_entered = [], [], []
    add_funded = add_skipped = 0
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

    # merged causal stream. kind rank 0 (base) before 1 (add) at an EXACT timestamp tie: the committed
    # base position claims pool room before an opportunistic add (and a market's own add always has
    # widen_t >= its open_t, so a position is never added-to before it opens).
    stream = ([(e["open_t"], 0, e) for e in base_cohort]
              + [(a["widen_t"], 1, a) for a in adds])
    stream.sort(key=lambda x: (x[0], x[1]))

    for now, kind, it in stream:
        settle_due(now)
        if kind == 0:                                     # ---- BASE entry (venue_split's exact sizing) ----
            net = it["open_net"]
            k_unit, p_unit, edge_per = _item_economics(
                it.get("dir"), net, px_idx.get((it["market"], it["open_t"])), it["market"])
            size = it["open_c2"]
            if k_unit > 0:
                size = min(size, int(kcash // k_unit))
            if p_unit > 0:
                size = min(size, int(pcash // p_unit))
            if size < 1:
                continue                                  # a venue pool can't fund even 1 contract
            # fat-edge haircut (risk.rs step 6) - identical to venue_split's base walk.
            if net * 100.0 >= BotConfig.fat_edge_knee_cents and BotConfig.fat_edge_size_factor < 1.0:
                size = max(1, int(size * BotConfig.fat_edge_size_factor))
            kcost, pcost = size * k_unit, size * p_unit
            kcash -= kcost; pcash -= pcost
            peak_k = max(peak_k, kalshi0 - kcash); peak_p = max(peak_p, pmus0 - pcash)
            pos = {"settle_t": settle_t(it["market"], it["open_t"], SETTLE_OFFSET_H), "kcost": kcost,
                   "pcost": pcost, "profit": size * edge_per, "market": it["market"], "cat": it["cat"],
                   "size": size, "kind": "base", "capital": kcost + pcost}
            held.append(pos); base_entered.append(pos)
        else:                                             # ---- ADD attempt (residual pool room only) ----
            net = it["net_add"]
            k_unit, p_unit, edge_per = _item_economics(it.get("dir"), net, it.get("widen_px"), it["market"])
            add_cap = min(it["c2_add"], MAX_CLIP, int(round(ADD_CAP_FRAC * it["size0"])))
            size = add_cap
            if k_unit > 0:
                size = min(size, int(kcash // k_unit))
            if p_unit > 0:
                size = min(size, int(pcash // p_unit))
            if size < 1:                                  # EITHER pool full (or no add cap) -> can't fund
                add_skipped += 1
                continue
            kcost, pcost = size * k_unit, size * p_unit
            kcash -= kcost; pcash -= pcost
            peak_k = max(peak_k, kalshi0 - kcash); peak_p = max(peak_p, pmus0 - pcash)
            pos = {"settle_t": settle_t(it["market"], it["widen_t"], SETTLE_OFFSET_H), "kcost": kcost,
                   "pcost": pcost, "profit": size * edge_per, "market": it["market"], "cat": it["cat"],
                   "size": size, "kind": "add", "capital": kcost + pcost}
            held.append(pos); add_entered.append(pos)
            add_funded += 1

    return {"base": base_entered, "adds": add_entered, "peak_k": peak_k, "peak_p": peak_p,
            "add_funded": add_funded, "add_skipped": add_skipped}


def _scenario(base_cohort, adds, px_idx, kalshi0, pmus0, window_end):
    """Run one scenario and reduce to the reportable scalars. `total` = realized + unrealized (mark-to-edge);
    it is the WINDOW-INVARIANT economic value and the basis for the honest add DELTA - the realized-only
    split moves capital across the 5-day window boundary (a high-edge add that settles in-window can crowd
    out base pairs that settle out-of-window, inflating dRealized while dTotal stays flat-to-negative; [L18])."""
    r = two_pool_walk_with_adds(base_cohort, adds, px_idx, kalshi0, pmus0)
    entered = r["base"] + r["adds"]
    realized = sum(p["profit"] for p in entered if p["settle_t"] <= window_end)
    unreal = sum(p["profit"] for p in entered if p["settle_t"] > window_end)
    locked = sum(p["capital"] for p in entered if p["settle_t"] > window_end)
    total0 = kalshi0 + pmus0
    return {"realized": realized, "unreal": unreal, "total": realized + unreal, "locked": locked,
            "ret_pct": 100.0 * realized / total0, "tot_pct": 100.0 * (realized + unreal) / total0,
            "n_base": len(r["base"]), "n_add": len(r["adds"]),
            "peak_k": r["peak_k"], "peak_p": r["peak_p"],
            "add_funded": r["add_funded"], "add_skipped": r["add_skipped"]}


# ---- report --------------------------------------------------------------------------------------
def build_report(eps, recs, sessions, cfg, kalshi0, pmus0, add_cap_frac, data_dir):
    L = []; P = L.append
    total0 = kalshi0 + pmus0
    window_end = recs[-1]["t"]
    span_d = (recs[-1]["t"] - recs[0]["t"]) / 86400.0
    span_d_hint = f"{span_d:.1f}d"
    dir_idx, _ = open_direction_index(recs, sessions)
    px_idx = open_px_index(recs)
    # ALL adds (phantom-lensed) on the FULL episode set, once; we partition by class + filter to each cohort.
    all_adds, _ = add_events(recs, eps, 0.0, 0, MAX_CLIP, add_cap_frac, TAU_GAIN, drop_flat_widen=True)

    P(SEP)
    P("CROSS-ARB - BANKROLL-NETTED ADD BACKTEST  ($%.0f Kalshi / $%.0f polymarket.us, SEPARATE wallets)" % (kalshi0, pmus0))
    P(SEP)
    P(f"generated : {dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')}")
    P(f"data dir  : {data_dir}")
    days = sorted({dt.datetime.utcfromtimestamp(r["t"]).strftime("%m-%d") for r in recs})
    P(f"span      : {span_d:.2f} d   transitions {len(recs)}   data days {', '.join(days)}")
    P(f"add policy: drop_flat_widen=ON ([L20] phantom lens), tau_gain {TAU_GAIN*100:.1f}c, per-pair cap "
      f"{add_cap_frac*100:.0f}% of base clip; settlement = capital_sim.settle_t (+{SETTLE_OFFSET_H:.0f}h).")
    P("")
    P("*** HONEST READ FIRST - the LEVEL is a method-demo; the decision-useful figure is the add DELTA: ***")
    P(f"  * SEPARATED WALLETS: an add deploys a YES leg + a NO leg ACROSS both venues, so it needs room in")
    P(f"    BOTH $250 pools - exactly like a base arb. flip_add_backtest's ~$98.69(all)/$22.69(true) is the")
    P(f"    UNCONSTRAINED upper bound (every widen funded at full cap); here adds compete for RESIDUAL pool room.")
    P(f"  * EFFECTIVE-N TINY (~5 event-dates carry it; {span_d:.1f} d window). PAPER/GROSS: fees+spread in")
    P(f"    net_edge; latency / ~17-55%-naked-leg / slippage / adverse-selection / sports-void NOT netted -> LOWER.")
    P(f"  * Base cohort = venue_split's _live_cohort (capturable + [L20] flat-drop + the now-live velocity")
    P(f"    gates); the size caps are LIFTED (else the $20 cap binds and $250/$250 is moot). World Cup nearly")
    P(f"    absent (deployed only today). The add DELTA is the deliverable; do not size off the LEVEL.")

    def section(title, settle_verified):
        cohort = _live_cohort(eps, cfg, settle_verified, dir_idx, px_idx, total0)
        markets = {e["market"] for e in cohort}
        # adds restricted to THIS cohort's markets (an add on a bucket the base strategy never entered is
        # not a scale-in of THIS book). Partition by the is_true_add class add_events already computed.
        c_adds = [a for a in all_adds if a["market"] in markets]
        true_adds = [a for a in c_adds if a["is_true_add"]]
        reentry = [a for a in c_adds if not a["is_true_add"]]

        A = _scenario(cohort, [], px_idx, kalshi0, pmus0, window_end)                      # BASE only
        B = _scenario(cohort, true_adds, px_idx, kalshi0, pmus0, window_end)               # + TRUE scale-in
        C = _scenario(cohort, reentry, px_idx, kalshi0, pmus0, window_end)                 # + RE-ENTRY
        D = _scenario(cohort, c_adds, px_idx, kalshi0, pmus0, window_end)                  # + BOTH

        P(""); P(SEP); P(title); P(SEP)
        P(f"  base cohort (one/market, full stack + velocity gates): {len(cohort)}  ({_fmt_catmix(_catmix(cohort))})")
        P(f"  effective event-days: {_eff_n_days(cohort)}")
        P(f"  add opportunities on these markets (phantom-lensed): {len(c_adds)}  "
          f"(true scale-in {len(true_adds)}  |  re-entry {len(reentry)})")
        P("")
        P(f"  {'scenario':<26}{'realized $':>12}{'unreal $':>10}{'TOTAL $':>10}"
          f"{'dTOTAL':>9}{'dReal':>9}{'adds':>6}{'fund/skip':>11}")
        P(f"  {'-'*26}{'-'*12}{'-'*10}{'-'*10}{'-'*9}{'-'*9}{'-'*6}{'-'*11}")

        def row(tag, S, ref):
            is_ref = S is ref
            dtot = "ref" if is_ref else f"{S['total']-ref['total']:+.2f}"
            dreal = "ref" if is_ref else f"{S['realized']-ref['realized']:+.2f}"
            fs = "-" if is_ref else f"{S['add_funded']}/{S['add_skipped']}"
            adds_in = "-" if is_ref else str(S["n_add"])
            P(f"  {tag:<26}{S['realized']:>12.2f}{S['unreal']:>10.2f}{S['total']:>10.2f}"
              f"{dtot:>9}{dreal:>9}{adds_in:>6}{fs:>11}")

        row("A. BASE only", A, A)
        row("B. BASE + TRUE scale-in", B, A)
        row("C. BASE + RE-ENTRY", C, A)
        row("D. BASE + BOTH", D, A)
        P("")
        P(f"  >>> dTOTAL (realized+unrealized mark-to-edge) is the HONEST marginal $ from adds - WINDOW-INVARIANT.")
        P(f"      dReal can DIVERGE (e.g. an add that settles inside the {span_d_hint} window crowds out base pairs that")
        P(f"      settle OUTSIDE it -> dReal positive while dTOTAL is flat/negative; [L18] - do NOT quote dReal alone).")
        P(f"  base TOTAL ${A['total']:.2f} ({A['tot_pct']:+.2f}% on ${int(total0)}); +both TOTAL ${D['total']:.2f} "
          f"({D['tot_pct']:+.2f}%); capital still locked +both ${D['locked']:.2f}")
        P(f"  peak deployed (+both)  : Kalshi ${D['peak_k']:.2f}/{kalshi0:.0f} ({100*D['peak_k']/kalshi0:.0f}%)  |  "
          f"pmus ${D['peak_p']:.2f}/{pmus0:.0f} ({100*D['peak_p']/pmus0:.0f}%)")
        P(f"  base entered {A['n_base']} pairs; +both adds FUNDED {D['add_funded']} / SKIPPED-for-capital {D['add_skipped']} "
          f"of {len(c_adds)} attempts")
        # the unconstrained upper bound add_events would report on THIS cohort, for the gap.
        unc_true = sum(a["inc_pnl"] for a in true_adds)
        unc_both = sum(a["inc_pnl"] for a in c_adds)
        P(f"  UNCONSTRAINED add UB (flip_add_backtest, no bankroll): true ${unc_true:.2f} | both ${unc_both:.2f}  "
          f"-> the decision-useful figure is dTOTAL above (bankroll-netted), FAR below this per-market UB.")
        return {"A": A, "B": B, "C": C, "D": D, "n_add": len(c_adds), "funded": D["add_funded"],
                "skipped": D["add_skipped"], "unc_both": unc_both}

    wx = section("WEATHER-ONLY  (what the bot can trade TODAY)", SETTLE_VERIFIED_NOW)
    al = section("ALL-VERIFIED  (the full strategy - POST-RECON PREVIEW)", SETTLE_VERIFIED_ALL)

    P(""); P(SEP); P("BOTTOM LINE  (the marginal $ from adds is dTOTAL, bankroll-netted + window-invariant)"); P(SEP)
    for name, s in (("WEATHER-ONLY (tradeable now)", wx), ("ALL-VERIFIED (post-recon preview)", al)):
        dB, dC, dD = (s[k]["total"] - s["A"]["total"] for k in ("B", "C", "D"))          # TOTAL deltas
        dDr = s["D"]["realized"] - s["A"]["realized"]                                    # realized delta (for the gap note)
        P(f"  {name}:")
        P(f"    base TOTAL ${s['A']['total']:.2f} ({s['A']['tot_pct']:+.2f}% on $500, realized+unrealized); MARGINAL from")
        P(f"    adds (bankroll-netted dTOTAL) - true {dB:+.2f} | re-entry {dC:+.2f} | both {dD:+.2f}")
        P(f"    adds: {s['funded']} FUNDED / {s['skipped']} SKIPPED-for-capital of {s['n_add']} attempts. "
          f"Unconstrained per-market UB ${s['unc_both']:.2f}")
        if abs(dDr - dD) > 0.10:
            P(f"    (NB: +both dReal {dDr:+.2f} >> dTOTAL {dD:+.2f} - the funded add settles in-window + crowds out base")
            P(f"     pairs that settle out-of-window; the realized-only gain is a window artifact, NOT new edge [L18].)")
    P("")
    P(f"  over ~{span_d:.1f} d, effective-n TINY, PAPER/GROSS, sports/econ settlement UNVERIFIED (weather-only is")
    P("  the honest 'today' universe). The add DELTA - NOT the level - is the decision: with two $250 wallets the")
    P("  base strategy already competes for both pools, so most adds can't be funded; the bankroll-netted marginal")
    P("  $ is what 'add scale-in' is really worth, far below the unconstrained per-market upper bound. NOT a go,")
    P("  NOT a magnitude to size off. NEXT: re-run on the multi-week post-recon data the 0014 protocol requires.")
    P(SEP)
    return "\n".join(L)


# ---- selftest ------------------------------------------------------------------------------------
def _selftest():
    print("bankroll-add-backtest self-test")
    import calendar
    mid = lambda y, mo, d: float(calendar.timegm((y, mo, d, 0, 0, 0, 0, 0, 0)))

    def base(mkt, t, net, c2, dir_="P"):
        return {"market": mkt, "cat": "weather", "open_t": t, "open_net": net, "open_c2": c2, "dir": dir_}

    def add(mkt, wt, net_add, c2_add, size0, is_true, dir_="P", px=None):
        return {"market": mkt, "cat": "weather", "dir": dir_, "net0": 0.03, "c2_0": size0,
                "net_add": net_add, "c2_add": c2_add, "size0": size0, "size_add": min(c2_add, int(round(0.20*size0))),
                "inc_pnl": 1.0, "void": 0.0, "flat_widen": False, "is_true_add": is_true,
                "widen_t": wt, "widen_px": px}

    # px touches making leg_split ~50/50 so per-contract pool draws are ~equal (cost_per/2 each side).
    px50 = {"p_yb": 0.45, "p_ya": 0.49, "k_yb": 0.51, "k_ya": 0.55}   # dir P: p pays p_ya .49, k pays 1-.51=.49

    # (i) an add is FUNDED when both pools have room, SKIPPED when EITHER pool is full -----------------
    # ONE base arb, ~$0.485/ctr/side, on date 06-10, depth 5. The add cap = round(.20*size0); size0=10 ->
    # cap 2, so the add wants 2 contracts (~$0.97/side). A pool of $1.45/side funds the 1 base contract
    # (~$0.485) leaving ~$0.97 - exactly enough for 2 add contracts; tighten to $1.10/side and the residual
    # ~$0.61 funds only 1 add contract; tighten to $0.60/side and the base eats it all -> add SKIPPED.
    c = [base("tc-temp-ahigh-2026-06-10-gte70f", 100, 0.03, c2=1, dir_="P")]
    pxi = {("tc-temp-ahigh-2026-06-10-gte70f", 100): px50}
    a10 = [add("tc-temp-ahigh-2026-06-10-gte70f", 150, 0.05, c2_add=10, size0=10, is_true=True, px=px50)]
    r = two_pool_walk_with_adds(c, a10, pxi, 0.60, 0.60)         # base eats the pool -> add can't fund
    assert len(r["base"]) == 1 and r["add_funded"] == 0 and r["add_skipped"] == 1, r
    r2 = two_pool_walk_with_adds(c, a10, pxi, 5.0, 5.0)          # both pools have room -> FUNDED
    assert r2["add_funded"] == 1 and r2["add_skipped"] == 0 and len(r2["adds"]) == 1, r2
    # asymmetric: plenty on pmus, Kalshi full after the base -> add SKIPPED (BOTH-pools rule, not either).
    r3 = two_pool_walk_with_adds(c, a10, pxi, 0.60, 200.0)
    assert r3["add_funded"] == 0 and r3["add_skipped"] == 1, ("Kalshi pool full blocks the add", r3)
    print("  OK - add FUNDED iff BOTH pools have residual room; SKIPPED (counted) when EITHER is full")

    # (ii) the delta-vs-base is computed correctly (a funded add adds exactly its funded profit) --------
    wend = mid(2026, 7, 1)
    A = _scenario(c, [], pxi, 50.0, 50.0, wend)                    # BASE only
    D2 = _scenario(c, a10, pxi, 50.0, 50.0, wend)                  # BASE + the add (cap 2, pools ample)
    # edge_per for net_add 0.05 = (0.05+0.005)/2 = 0.0275; funded size = round(.20*10)=2 -> +$0.055 realized.
    expect_delta = 2 * ((0.05 + DEPTH_BOUNDARY_NET) / 2.0)
    assert abs((D2["realized"] - A["realized"]) - expect_delta) < 1e-9, (D2["realized"], A["realized"], expect_delta)
    # an add whose cap rounds to 0 (size0 too small) contributes ZERO delta - the cap, not the pool, gates it.
    a_cap0 = [add("tc-temp-ahigh-2026-06-10-gte70f", 150, 0.05, c2_add=10, size0=1, is_true=True, px=px50)]
    Dz = _scenario(c, a_cap0, pxi, 50.0, 50.0, wend)
    assert Dz["realized"] == A["realized"], ("size0=1 -> add cap round(.2)=0 -> no add -> zero delta", Dz, A)
    print(f"  OK - delta-vs-base = the funded add's realized profit ({expect_delta:+.4f} at size 2); cap-0 add -> 0 delta")

    # (iii) true-add vs re-entry routed correctly (each class scenario sees only its own adds) ----------
    cohort2 = [base("tc-temp-bhigh-2026-06-10-gte70f", 100, 0.03, c2=5, dir_="P")]
    pxi2 = {("tc-temp-bhigh-2026-06-10-gte70f", 100): px50}
    t_add = [add("tc-temp-bhigh-2026-06-10-gte70f", 150, 0.06, c2_add=20, size0=10, is_true=True, px=px50)]
    r_add = [add("tc-temp-bhigh-2026-06-10-gte70f", 160, 0.06, c2_add=20, size0=10, is_true=False, px=px50)]
    A2 = _scenario(cohort2, [], pxi2, 50.0, 50.0, wend)            # cohort2's OWN base reference
    B = _scenario(cohort2, t_add, pxi2, 50.0, 50.0, wend)          # TRUE only
    Cc = _scenario(cohort2, r_add, pxi2, 50.0, 50.0, wend)         # RE-ENTRY only
    Dd = _scenario(cohort2, t_add + r_add, pxi2, 50.0, 50.0, wend) # BOTH
    assert B["n_add"] == 1 and Cc["n_add"] == 1 and Dd["n_add"] == 2, (B["n_add"], Cc["n_add"], Dd["n_add"])
    # both deltas positive + BOTH ~ sum of the two (pools large enough that neither starves the other)
    assert (B["realized"] - A2["realized"]) > 0 and (Cc["realized"] - A2["realized"]) > 0
    assert abs((Dd["realized"] - A2["realized"])
               - ((B["realized"] - A2["realized"]) + (Cc["realized"] - A2["realized"]))) < 1e-9
    print("  OK - true-add / re-entry routed to their own scenarios; BOTH = true + re-entry when unconstrained")

    # (iv) capital frees at settlement -> a later add reuses the freed pool ----------------------------
    # base opens 06-09 (settles 06-10 +28h); a tiny pool funds ONLY the base. An add whose widen is AFTER
    # the base's settlement reuses the freed capital -> FUNDED despite the pool being too small to hold both.
    cohort3 = [base("tc-temp-chigh-2026-06-10-gte70f", mid(2026, 6, 9), 0.03, c2=1, dir_="P")]
    pxi3 = {("tc-temp-chigh-2026-06-10-gte70f", mid(2026, 6, 9)): px50}
    late_add = [add("tc-temp-chigh-2026-06-10-gte70f", mid(2026, 6, 12), 0.05, c2_add=1, size0=5, is_true=False, px=px50)]
    r_seq = two_pool_walk_with_adds(cohort3, late_add, pxi3, 0.60, 0.60)   # ~0.49/side: holds 1 at a time
    assert len(r_seq["base"]) == 1 and r_seq["add_funded"] == 1, ("add funded only after base settled+freed", r_seq)
    # and if the add fired DURING the hold (before settlement), the same tiny pool would skip it:
    early_add = [add("tc-temp-chigh-2026-06-10-gte70f", mid(2026, 6, 9) + 100, 0.05, c2_add=1, size0=5, is_true=True, px=px50)]
    r_seq2 = two_pool_walk_with_adds(cohort3, early_add, pxi3, 0.60, 0.60)
    assert r_seq2["add_funded"] == 0 and r_seq2["add_skipped"] == 1, ("concurrent add can't fit the tiny pool", r_seq2)
    print("  OK - capital frees at settlement: a post-settlement add reuses the pool; a concurrent one is skipped")

    # (v) leg_split [L28] trap: a single-letter binary dir routes through the binary branch (not 50/50 by accident).
    # lopsided book (Kalshi ~0.95 leg) drains the Kalshi pool first - the per-venue asymmetry, on a real binary dir.
    lop_px = {"p_yb": 0.02, "p_ya": 0.03, "k_yb": 0.06, "k_ya": 0.08}      # dir P: pmus .03 / kalshi 1-.06=.94
    ku, pu, _ = _item_economics("P", 0.03, lop_px, "tc-temp-lop-2026-06-10-gte70f")
    assert ku > 5 * pu, ("binary dir P must put ~0.94 on Kalshi vs ~0.03 on pmus", ku, pu)
    print("  OK - leg_split [L28]: single-letter binary dir 'P' splits ~0.94 Kalshi / ~0.03 pmus (not 50/50)")

    # (vi) CROWD-OUT: a high-edge add that SETTLES IN-WINDOW can displace a base pair that settles
    # OUT-of-window -> dReal inflates above the honest dTOTAL. This is the [L18] window artifact the report
    # leads with dTOTAL (not dReal) to avoid. Three markets, settle_t keys on the SLUG date (+28h):
    #   pin      : 06-14 slug -> settles 06-15 (OUT-of-window); opens 06-09 and STAYS HELD the whole window,
    #              holding ~1/3 of the tight pool.
    #   addbase  : 06-12 slug -> its base pair settles 06-13 04:00 (IN-window vs we=06-13 12:00); the add on
    #              this bucket (widen 06-10, while addbase is held) is a 2nd pair settling IN-window, high edge.
    #   base-late: 06-14 slug -> settles 06-15 (OUT-of-window); opens 06-11, AFTER the add's widen.
    # Pool (~3 x $0.485) holds pin + addbase + ONE of {add, base-late}. With NO add, all 3 base pairs fit.
    # WITH the add, it claims the 3rd slot first (06-10 widen < base-late's 06-11 open) -> base-late crowded
    # out. The add's profit is realized in-window (dReal up); the lost base-late was unrealized -> dTOTAL
    # moves far less (the add's thin edge nets only marginally above the forfeited base edge).
    we = 1781400000.0                                              # 06-13 ~12:00Z: after addbase/add 06-13 settle, before the 06-15 ones
    pin = base("tc-temp-pinhigh-2026-06-14-lt72f", mid(2026, 6, 9), 0.03, c2=1, dir_="P")
    addbase = base("tc-temp-addhigh-2026-06-12-gte70f", mid(2026, 6, 9) + 50, 0.03, c2=1, dir_="P")
    b_late = base("tc-temp-lathigh-2026-06-14-gte70f", mid(2026, 6, 11), 0.03, c2=1, dir_="P")
    base6 = [pin, addbase, b_late]
    pxi6 = {(e["market"], e["open_t"]): px50 for e in base6}
    crowd_add = [add("tc-temp-addhigh-2026-06-12-gte70f", mid(2026, 6, 10), 0.05, c2_add=5, size0=5,
                     is_true=True, px=px50)]                        # cap round(.2*5)=1 -> wants ~1 contract ($0.485/side)
    Abig = _scenario(base6, [], pxi6, 30.0, 30.0, we)              # ample pool: all 3 base pairs enter
    assert Abig["n_base"] == 3 and Abig["unreal"] > 0, ("the 06-14 pairs must be unrealized", Abig)
    pool = 1.50                                                    # ~pin + addbase + 1 more: holds exactly one of {add, base-late}
    Asmall = _scenario(base6, [], pxi6, pool, pool, we)           # base-only: pin + addbase + base-late all fit
    Dsmall = _scenario(base6, crowd_add, pxi6, pool, pool, we)    # + the in-window crowding add
    dReal = Dsmall["realized"] - Asmall["realized"]
    dTot = Dsmall["total"] - Asmall["total"]
    assert Asmall["n_base"] == 3, ("base-only must seat all 3 at this pool", Asmall)
    assert Dsmall["add_funded"] == 1 and Dsmall["n_base"] == 2, \
        ("the add must fund AND crowd out exactly one base pair", Dsmall)
    assert dReal > dTot + 1e-6, ("the in-window add must inflate dReal above the honest dTOTAL when it crowds "
                                 "out an out-of-window base pair [L18]", dReal, dTot)
    print(f"  OK - crowd-out [L18]: an in-window add inflates dReal ({dReal:+.4f}) above the honest dTOTAL ({dTot:+.4f})")

    print("self-test passed.")


# ---- main ----------------------------------------------------------------------------------------
if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="bankroll-netted marginal value of scale-in/re-entry adds (read-only)")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--data-dir", default=os.path.join(os.path.dirname(__file__), "..", "..", "data", "cross-arb"))
    ap.add_argument("--kalshi", type=float, default=250.0, help="Kalshi-side bankroll $ (default 250)")
    ap.add_argument("--pmus", type=float, default=250.0, help="polymarket.us-side bankroll $ (default 250)")
    ap.add_argument("--add-cap-frac", type=float, default=ADD_CAP_FRAC, help="per-pair add size cap as a fraction of the base clip")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    if a.selftest:
        _selftest(); sys.exit(0)
    dd = os.path.abspath(a.data_dir)
    if not glob.glob(os.path.join(dd, "transitions-*.jsonl*")):
        print(f"no data at {dd} - run `pwsh deploy/pull-data.ps1` first, or `--selftest`."); sys.exit(0)
    recs, sessions = load(dd)
    eps = build_episodes(recs, sessions)
    report = build_report(eps, recs, sessions, BotConfig, a.kalshi, a.pmus, a.add_cap_frac, dd)
    print(report)
    if a.out:
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(report + "\n")
        print(f"\n[written to {a.out}]")
