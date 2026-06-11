"""scripts/ev_at_latency.py - EV consequences of the MEASURED leg-fill risk at realistic latency.

shadow_fill.py measured WHO dies (naked-leg rate vs entry latency L) on the post-0013 capturable
cohort. This script prices it on the SAME episodes:

  1. per-attempt per-contract EV(L,u) for L in the measured RTT bracket {86,150,261}ms + the old 1s
     regime, u = assumed naked-unwind cost {1,2,3}c (UNMEASURED - a labelled assumption);
  2. dollarized per-episode EV x logged fillable depth (c1 = gross marginal >=1c, matching the >=1c
     cohort; c2 = gross >=2c) -> window-$ and $/day, capped + uncapped per episode ([L18] top-1 share);
  3. the frozen H1 $500 account (0014: reservation tau=2c booked + category caps 20/10/5%) with the
     per-episode L=150ms outcome applied to FUNDED positions only;
  4. weather vs sports split (econ n=0 in this window).

Fill semantics are shadow_fill's EXACTLY (build_trajectories/edge_at + its loop-body classification,
factored per-episode here and CROSS-CHECKED against shadow_fill()'s aggregate rows at runtime):
  both-legs : episode still open at open_t+L, no FLIP at/before, net>0  -> +net@L per contract
  one-leg   : episode CLOSEd or FLIPped at/before open_t+L (0013: FLIP-before-fill = leg-fail) -> -u
  neither   : net<=0 while open (defensive branch; structurally absent at transition grain -
              a non-positive net IS a CLOSE in the monitor) -> 0
ALL leg-fails are priced as ONE-LEG (-u): transition-grain data cannot distinguish the
both-quotes-vanished (neither-leg, $0) case, so EV is conservative on that axis - and OPTIMISTIC on
every other (see CAVEATS in the report).

READ-ONLY pure analysis of the pulled archive; post-0013 epoch only (the only records whose
sub-second timestamps are real). `--selftest` runs the offline synthetic check.

  python scripts/ev_at_latency.py [--data-dir PATH] [--json-out PATH]
  python scripts/ev_at_latency.py --selftest
"""
import os, sys, argparse, glob, json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from analyze_persistence import load, build_episodes, ECON_REMAP_DEPLOY_TS   # shared loader (quarantine inside)
from shadow_fill import (post_epoch, build_trajectories, edge_at, capturable,  # the fill machinery - REUSED
                         shadow_fill, _median)
from capital_sim import one_per_market, settle_t                              # frozen account economics
from alloc_policy_experiment import run_policy, _profit_per, _cost_per        # 0012-tested reservation walk

EV_LATS  = (0.086, 0.15, 0.261, 1.0)   # measured order-RTT bracket (86..261ms) + the old 1s regime
UNWINDS  = (0.01, 0.02, 0.03)          # assumed naked-unwind cost u per contract (UNMEASURED)
DOL_LATS = (0.0, 0.15, 1.0)            # paper ceiling / realistic / old regime
DOL_U    = 0.02                        # headline u for the dollarized + H1 views
CLIP     = 1000                        # sane per-episode contract cap (account_sim default max_clip)

# Frozen H1 rule (decision 0014 / research/allocation-prereg-2026-06-10.md S1) - NOT tunable here.
H1_TAU, H1_BANKROLL = 0.02, 500.0
H1_CAPS = {"weather": 0.20, "sports": 0.10, "econ": 0.05}    # per-pair cap as fraction of bankroll
OFFSET_H, VOID_MULT = 28.0, 1.0                              # frozen account_sim economics


# ============================================================================================
# PER-EPISODE OUTCOME - the EXACT shadow_fill.shadow_fill loop-body conditions, factored so a
# single episode's outcome can be priced/dollarized. crosscheck() proves no drift at runtime.
# ============================================================================================
def outcome(e, L):
    """-> ("both", net_at_fill) | ("one", None) | ("neither", None) for entry latency L."""
    x = e["open_t"] + L
    if x >= e["close_t"]:                                  # edge died before both legs landed
        return "one", None
    if any(ft <= x for ft in e.get("flip_ts", ())):        # FLIP-before-fill = leg-fail (0013)
        return "one", None
    net = edge_at(e, x)
    if net is not None and net > 0:
        return "both", net
    return "neither", None                                 # defensive; never fires on real data


def crosscheck(eps, edge_min=0.01):
    """Aggregate outcome() over the cohort and assert equality with shadow_fill()'s own rows
    (n_survived / fill_fail / realized_mean) on the canonical grid -> the per-episode factoring
    is provably the same machinery, not a re-implementation."""
    cohort = capturable(eps, edge_min)
    if not cohort:
        return 0
    rows, n = shadow_fill(eps, edge_min)
    for r in rows:
        outs = [outcome(e, r["L"]) for e in cohort]
        nb = sum(1 for k, _ in outs if k == "both")
        no = sum(1 for k, _ in outs if k == "one")
        nets = [v for k, v in outs if k == "both"]
        mean = sum(nets) / len(nets) if nets else 0.0
        assert nb == r["n_survived"], (r["L"], nb, r["n_survived"])
        assert abs(no / n - r["fill_fail"]) < 1e-12, (r["L"], no / n, r["fill_fail"])
        assert abs(mean - r["realized_mean"]) < 1e-12, (r["L"], mean, r["realized_mean"])
    return len(rows)


def attach_c1(records, episodes):
    """Join depth.c1 (contracts at gross marginal >=1c) from each episode's OPEN record onto the
    episode (build_episodes keeps only c2). Post-epoch OPEN stamps are raw detection-time and
    unmodified by build_episodes, so (market, open_t) is an exact key. Missing -> fall back to
    open_c2 (c1 >= c2 by construction, so the fallback is conservative); count reported."""
    dmap = {}
    for r in records:
        if r.get("transition") == "OPEN" and r.get("depth"):
            dmap.setdefault((r["market"], r["t"]), r["depth"])
    n_miss = 0
    for e in episodes:
        c1 = (dmap.get((e["market"], e["open_t"])) or {}).get("c1")
        if c1 is None:
            n_miss += 1
            c1 = e["open_c2"]
        e["open_c1"] = int(c1)
    return n_miss


# ============================================================================================
# VIEW 1 - per-attempt per-contract EV(L, u)
# ============================================================================================
def ev_rows(cohort, lats=EV_LATS, unwinds=UNWINDS):
    """Per L: outcome probabilities, survivor mean/median, EV per u, breakeven u. EV is the mean
    over episodes of [both -> +net@L ; one -> -u ; neither -> 0] - per-episode first, THEN the
    mean (never aggregate x aggregate)."""
    n = len(cohort)
    rows = []
    for L in lats:
        outs = [outcome(e, L) for e in cohort]
        nets = [v for k, v in outs if k == "both"]
        n_one = sum(1 for k, _ in outs if k == "one")
        n_nei = sum(1 for k, _ in outs if k == "neither")
        s = sum(nets)
        rows.append({
            "L": L, "n": n, "n_both": len(nets), "n_one": n_one, "n_neither": n_nei,
            "p_both": len(nets) / n if n else 0.0, "p_one": n_one / n if n else 0.0,
            "p_neither": n_nei / n if n else 0.0,
            "surv_mean": s / len(nets) if nets else 0.0,
            "surv_median": _median(nets),
            "ev": {f"{u:g}": (s - u * n_one) / n if n else 0.0 for u in unwinds},
            "breakeven_u_mean": (s / n_one) if n_one else float("inf"),
            "breakeven_u_median": (len(nets) * _median(nets) / n_one) if n_one else float("inf"),
        })
    return rows


def dominators(cohort, L, k=3):
    """Top-k survivor episodes by realized net@L (the fat tail that drives the MEAN) + their share
    of the total survivor edge."""
    surv = []
    for e in cohort:
        kind, net = outcome(e, L)
        if kind == "both":
            surv.append((net, e))
    surv.sort(key=lambda x: -x[0])
    tot = sum(v for v, _ in surv)
    top = [{"market": e["market"], "net": v, "open_c1": e.get("open_c1"), "open_c2": e["open_c2"],
            "duration": e["duration"]} for v, e in surv[:k]]
    return top, (sum(t["net"] for t in top) / tot if tot else 0.0)


# ============================================================================================
# VIEW 2 - dollarized at logged depth (per-episode EV x its fillable depth, then summed)
# ============================================================================================
def dollarize(cohort, L, u, depth_key, clip=None):
    """Sum over episodes of [both -> net@L ; one -> -u ; neither -> 0] x min(depth, clip).
    depth_key in {"open_c1","open_c2"}. Returns window-$ + the top-1 positive episode ([L18])."""
    tot = gross_pos = 0.0
    top_v, top_m = 0.0, None
    for e in cohort:
        kind, net = outcome(e, L)
        size = e[depth_key]
        if clip:
            size = min(size, clip)
        v = net * size if kind == "both" else (-u * size if kind == "one" else 0.0)
        tot += v
        if v > 0:
            gross_pos += v
            if v > top_v:
                top_v, top_m = v, e["market"]
    return {"window_usd": tot, "top1_usd": top_v, "top1_market": top_m,
            "top1_share": (top_v / gross_pos) if gross_pos else 0.0}


# ============================================================================================
# VIEW 3 - the frozen H1 $500 account at L=150ms
# ============================================================================================
def h1_replay(episodes, bankroll=H1_BANKROLL, caps=H1_CAPS, tau=H1_TAU, offset_h=OFFSET_H,
              void_mult=VOID_MULT, haircut=0.0, max_clip=10 ** 9):
    """The frozen H1 reservation walk (0014 / prereg S1): candidates in arrival order; fund iff
    booked per-contract edge (_profit_per - identical account_sim economics) >= tau AND the
    category cap permits; size = min(depth c2, cap-contracts, affordable); unaffordable ->
    permanent skip; settle-recycling exactly as run_policy. caps are $-of-initial-bankroll per
    pair. With caps=None this IS alloc_policy_experiment.run_policy('reservation', tau=tau)
    (asserted in --selftest), so the only new code is the category-cap term in `want`."""
    cap = capturable(episodes, 0.0)        # frozen gates: edge0/window0/liq1/age-off/drop_restart
    cands = sorted(one_per_market(cap), key=lambda e: e["open_t"])
    cash = float(bankroll)
    held, entered = [], []
    skipped_tau = skipped_capital = 0

    def settle_due(now):
        nonlocal cash, held
        still = []
        for p in held:
            if p["settle_t"] <= now:
                cash += p["capital"] + p["profit"]
            else:
                still.append(p)
        held = still

    for e in cands:
        settle_due(e["open_t"])
        per = _profit_per(e, haircut, void_mult)           # booked edge: book-avg - void (account_sim)
        if per < tau:
            skipped_tau += 1
            continue
        cost = _cost_per(e)
        cap_n = int((caps.get(e["cat"], min(caps.values())) * bankroll) // cost) if caps else max_clip
        want = min(e["open_c2"], cap_n, max_clip)
        afford = int(cash // cost)
        size = min(want, afford)
        if size < 1:
            skipped_capital += 1
            continue
        capital = size * cost
        cash -= capital
        pos = {"market": e["market"], "cat": e["cat"], "size": size, "capital": capital,
               "profit": size * per, "settle_t": settle_t(e["market"], e["open_t"], offset_h),
               "ep": e}
        held.append(pos)
        entered.append(pos)
    return {"entered": entered, "cash": cash, "skipped_tau": skipped_tau,
            "skipped_capital": skipped_capital,
            "deployed": sum(p["capital"] for p in entered),
            "booked": sum(p["profit"] for p in entered)}


def h1_at_latency(h1, L, unwinds=UNWINDS):
    """Apply each funded position's own episode outcome at L: both -> size x net@L realized;
    one -> naked-tax size x u. (Booked H1 profit is the paper-gross reference next to it.)"""
    realized, n_both, n_one = 0.0, 0, 0
    tax = {f"{u:g}": 0.0 for u in unwinds}
    fails = []
    for p in h1["entered"]:
        kind, net = outcome(p["ep"], L)
        if kind == "both":
            realized += p["size"] * net
            n_both += 1
        elif kind == "one":
            n_one += 1
            fails.append(p["market"])
            for u in unwinds:
                tax[f"{u:g}"] += p["size"] * u
    return {"L": L, "n_funded": len(h1["entered"]), "n_both": n_both, "n_one": n_one,
            "realized_usd": realized, "naked_tax_usd": tax,
            "net_usd": {k: realized - v for k, v in tax.items()}, "failed_markets": fails}


# ============================================================================================
# REPORT
# ============================================================================================
def _ms(L):
    return f"{int(round(L * 1000))}ms" if L < 1 else f"{L:g}s"


def _c(x):
    return 100.0 * x


def render(cohort, span_s, eps, json_sink):
    out = []
    P = out.append
    day = 86400.0 / span_s if span_s else 0.0
    span_h = span_s / 3600.0
    by_cat = {"weather": [e for e in cohort if e["cat"] == "weather"],
              "sports": [e for e in cohort if e["cat"] == "sports"],
              "econ": [e for e in cohort if e["cat"] == "econ"]}

    P("=" * 96)
    P(f"EV AT LATENCY  (post-0013 epoch; capturable >=1c cohort n={len(cohort)}; span {span_h:.2f} h; "
      f"window->/day scale = x{day:.3f})")
    P(f"  cohort: weather={len(by_cat['weather'])}  sports={len(by_cat['sports'])}  "
      f"econ={len(by_cat['econ'])}" + ("  (econ n=0 in this window)" if not by_cat["econ"] else ""))
    P("")

    # ---- view 1: EV table ----
    rows = ev_rows(cohort)
    json_sink["ev_table"] = rows
    P(f"[1] PER-ATTEMPT PER-CONTRACT EV (cents)   n={len(cohort)} episodes per cell; u = naked-unwind cost")
    P("      L   | P(both) P(one) P(nei) | surv mean / med (c) | EV u=1c  u=2c  u=3c | breakeven-u mean/med")
    for r in rows:
        P(f"   {_ms(r['L']):>6} | {r['p_both']:6.1%} {r['p_one']:6.1%} {r['p_neither']:5.1%} |"
          f"   {_c(r['surv_mean']):5.2f} / {_c(r['surv_median']):4.2f}      |"
          f"  {_c(r['ev']['0.01']):+5.2f}  {_c(r['ev']['0.02']):+5.2f}  {_c(r['ev']['0.03']):+5.2f} |"
          f"  {_c(r['breakeven_u_mean']):5.2f} / {_c(r['breakeven_u_median']):4.2f}c")
    top, share = dominators(cohort, 0.15)
    json_sink["dominators_150ms"] = {"top": top, "share_of_survivor_edge": share}
    P(f"   mean > median at every L: the mean (which drives EV) leans on a fat-edge tail - top-3 survivors")
    P(f"   @150ms = {share:.1%} of all survivor edge: " +
      "; ".join(f"{t['market']} ({_c(t['net']):.1f}c, c1={t['open_c1']}, c2={t['open_c2']})" for t in top))
    P("")

    # ---- view 2: dollarized ----
    P(f"[2] DOLLARIZED AT LOGGED DEPTH  (u={_c(DOL_U):.0f}c; per-episode EV x depth, summed; "
      f"$/day = window x {day:.3f}; clip={CLIP}/episode)")
    P("      L   | lens |   capped $/window   $/day | uncapped $/window   $/day |  top-1 share (capped)")
    json_sink["dollarized"] = []
    for L in DOL_LATS:
        for lens, key in (("c1", "open_c1"), ("c2", "open_c2")):
            d_cap = dollarize(cohort, L, DOL_U, key, clip=CLIP)
            d_unc = dollarize(cohort, L, DOL_U, key, clip=None)
            json_sink["dollarized"].append({"L": L, "lens": lens, "u": DOL_U, "clip": CLIP,
                                            "capped": d_cap, "uncapped": d_unc})
            P(f"   {_ms(L):>6} |  {lens}  |   {d_cap['window_usd']:8.2f}  {d_cap['window_usd'] * day:8.2f}"
              f" |     {d_unc['window_usd']:8.2f}  {d_unc['window_usd'] * day:8.2f} |"
              f"  {d_cap['top1_share']:5.1%}  {d_cap['top1_market']}")
    base = {k: dollarize(cohort, 0.0, DOL_U, k, CLIP)["window_usd"] * day
            for k in ("open_c1", "open_c2")}
    json_sink["latency_tax_per_day"] = {}
    for L in (0.15, 1.0):
        taxes = {k: base[k] - dollarize(cohort, L, DOL_U, k, CLIP)["window_usd"] * day
                 for k in ("open_c1", "open_c2")}
        json_sink["latency_tax_per_day"][_ms(L)] = {"c1": taxes["open_c1"], "c2": taxes["open_c2"]}
        P(f"   LATENCY TAX vs L=0 at {_ms(L):>5} (capped): c1 ${taxes['open_c1']:,.0f}/day   "
          f"c2 ${taxes['open_c2']:,.0f}/day")
    P("")

    # ---- view 3: H1 $500 account ----
    h1 = h1_replay(eps)
    h1l = h1_at_latency(h1, 0.15)
    json_sink["h1"] = {"n_funded": len(h1["entered"]), "deployed": h1["deployed"],
                       "booked": h1["booked"], "skipped_tau": h1["skipped_tau"],
                       "skipped_capital": h1["skipped_capital"],
                       "funded": [{k: p[k] for k in ("market", "cat", "size", "capital", "profit")}
                                  for p in h1["entered"]],
                       "at_150ms": {k: v for k, v in h1l.items() if k != "failed_markets"},
                       "failed_markets_150ms": h1l["failed_markets"]}
    cats = {}
    for p in h1["entered"]:
        cats[p["cat"]] = cats.get(p["cat"], 0) + 1
    P(f"[3] $500 H1 ACCOUNT (frozen 0014 rule: tau=2c booked + caps w20/s10/e5%) AT L=150ms")
    P(f"   funded {len(h1['entered'])} positions ({'  '.join(f'{c}={n}' for c, n in sorted(cats.items()))})"
      f"   deployed ${h1['deployed']:.2f}   skipped: tau={h1['skipped_tau']} capital={h1['skipped_capital']}")
    P(f"   booked paper-gross PnL (instant-fill assumption)   : ${h1['booked']:+7.2f}")
    P(f"   at 150ms: {h1l['n_both']}/{h1l['n_funded']} fill both legs -> realized ${h1l['realized_usd']:+7.2f}"
      f"  (touch net@fill x size; gross of sports-void EV)")
    P(f"   naked legs {h1l['n_one']}/{h1l['n_funded']}: tax u=1c ${h1l['naked_tax_usd']['0.01']:.2f}"
      f"  u=2c ${h1l['naked_tax_usd']['0.02']:.2f}  u=3c ${h1l['naked_tax_usd']['0.03']:.2f}")
    P(f"   NET at 150ms: u=1c ${h1l['net_usd']['0.01']:+7.2f}   u=2c ${h1l['net_usd']['0.02']:+7.2f}"
      f"   u=3c ${h1l['net_usd']['0.03']:+7.2f}   (window = {span_h:.1f} h)")
    if h1l["failed_markets"]:
        P(f"   failed: {', '.join(h1l['failed_markets'])}")
    P("")

    # ---- view 4: category split ----
    P(f"[4] CATEGORY SPLIT  (EV at u=2c, cents/contract; $/day capped at u=2c)")
    P("      cat      n |   EV@86ms  150ms  261ms     1s |  c1 $/day @150ms |  c2 $/day @150ms")
    json_sink["by_category"] = {}
    for cat in ("weather", "sports", "econ"):
        sub = by_cat[cat]
        if not sub:
            P(f"   {cat:>8}   0 |  (no capturable episodes in window)")
            json_sink["by_category"][cat] = {"n": 0}
            continue
        r = ev_rows(sub)
        d1 = dollarize(sub, 0.15, DOL_U, "open_c1", CLIP)
        d2 = dollarize(sub, 0.15, DOL_U, "open_c2", CLIP)
        json_sink["by_category"][cat] = {"n": len(sub), "ev_table": r,
                                         "usd_day_150ms_c1": d1["window_usd"] * day,
                                         "usd_day_150ms_c2": d2["window_usd"] * day,
                                         "top1_c1": {"market": d1["top1_market"], "share": d1["top1_share"]}}
        e = {row["L"]: row["ev"]["0.02"] for row in r}
        P(f"   {cat:>8} {len(sub):3d} |   {_c(e[0.086]):+5.2f}  {_c(e[0.15]):+5.2f}  {_c(e[0.261]):+5.2f}"
          f"  {_c(e[1.0]):+5.2f} |     ${d1['window_usd'] * day:8.2f} |     ${d2['window_usd'] * day:8.2f}")
    P("")
    P("CAVEATS (label every number above with these)")
    P("  - ONE sports-heavy ~16h window (476/575 sports); /day figures are a x"
      f"{day:.2f} extrapolation of it.")
    P("  - Shadow proxy is OPTIMISTIC: transition-grain trajectories (books move between logged")
    P("    transitions), full fill at touch net assumed, no queue/partial fills, no adverse selection.")
    P("  - Depth = LOGGED crossable depth at open (c1/c2), not a walked-book fill - edge decays down the")
    P("    ladder, so $ x touch-net x depth overstates; conversely all leg-fails priced as full -u x size.")
    P("  - [2] is an ATTEMPT-grain capacity lens: every capturable OPEN counts as a fresh full-depth")
    P("    attempt (same-market re-detections NOT collapsed, unlike the C8 account views) and assumes")
    P("    instant capital recycling - a gross ceiling, not a bankroll account ([3] is the account).")
    P("  - u (naked-unwind cost) is UNMEASURED: 1-3c plausible (re-cross spread + fees); breakeven col")
    P("    shows where EV flips. Paper-gross throughout; settlement-identity recon still open.")
    P("=" * 96)
    return "\n".join(out)


# ============================================================================================
# SELF-TEST  (synthetic; no I/O)
# ============================================================================================
def _selftest():
    print("ev-at-latency self-test")

    def tr(t, m, lab, net, d="PK", depth=None):
        r = {"t": t, "market": m, "transition": lab, "dir": d, "net_edge": net}
        if depth:
            r["depth"] = depth
        return r

    E1 = "tc-temp-aaa-2026-06-10-gte70"      # weather: opens 3c @0, widens 5c @1, closes @1.5
    E2 = "aec-mlb-cc-dd-2026-06-10"          # sports : opens 4c @10, closes @20
    recs = [
        tr(0, E1, "OPEN", 0.03, depth={"c2": 100, "c1": 150, "c0": 200}),
        tr(1, E1, "WIDEN", 0.05, depth={"c2": 100, "c1": 150, "c0": 200}),
        tr(1.5, E1, "CLOSE", -0.01),
        tr(10, E2, "OPEN", 0.04, depth={"c2": 200, "c1": 300, "c0": 300}),
        tr(20, E2, "CLOSE", -0.01),
    ]
    eps = build_episodes(recs, sessions=[], close_lag=0)
    build_trajectories(recs, [], eps)
    n_miss = attach_c1(recs, eps)
    by = {e["market"]: e for e in eps}
    assert n_miss == 0 and by[E1]["open_c1"] == 150 and by[E2]["open_c1"] == 300, (n_miss, by)

    # outcome semantics == shadow_fill loop body
    assert outcome(by[E1], 1.0) == ("both", 0.05)          # widened, still open
    assert outcome(by[E1], 2.0) == ("one", None)           # closed @1.5 -> naked leg
    assert outcome(by[E2], 2.0) == ("both", 0.04)
    crosscheck(eps)                                        # aggregates == shadow_fill() rows
    print("  OK - outcome() matches shadow_fill loop-body semantics + aggregate cross-check")

    # FLIP-before-fill = leg-fail (0013)
    E3 = "aec-mlb-ee-ff-2026-06-10"
    frecs = [tr(100, E3, "OPEN", 0.04, depth={"c2": 50, "c1": 50, "c0": 50}),
             {**tr(102, E3, "FLIP", 0.03), "dir": "KP"}, tr(110, E3, "CLOSE", -0.01)]
    feps = build_episodes(frecs, [], close_lag=0)
    build_trajectories(frecs, [], feps)
    assert outcome(feps[0], 1.0) == ("both", 0.04) and outcome(feps[0], 2.0) == ("one", None)
    print("  OK - FLIP at/before fill counts as one-leg")

    # EV by hand: at L=2 cohort {E1 one, E2 both@4c}: EV(u=2c) = (0.04 - 0.02)/2 = +1.00c
    cohort = capturable(eps, 0.01)
    r2 = [r for r in ev_rows(cohort, lats=(2.0,)) if r["L"] == 2.0][0]
    assert r2["n_both"] == 1 and r2["n_one"] == 1 and r2["n_neither"] == 0
    assert abs(r2["ev"]["0.02"] - 0.01) < 1e-12, r2["ev"]
    assert abs(r2["breakeven_u_mean"] - 0.04) < 1e-12          # EV=0 at u = 0.04/1
    # dollarized by hand at L=2 u=2c: c2: -0.02*100 + 0.04*200 = $6 ; c1: -3 + 12 = $9
    assert abs(dollarize(cohort, 2.0, 0.02, "open_c2")["window_usd"] - 6.0) < 1e-9
    assert abs(dollarize(cohort, 2.0, 0.02, "open_c1")["window_usd"] - 9.0) < 1e-9
    d = dollarize(cohort, 2.0, 0.02, "open_c2", clip=150)      # E2 capped 200->150: 6-2 = $4
    assert abs(d["window_usd"] - 4.0) < 1e-9 and d["top1_market"] == E2 and abs(d["top1_share"] - 1.0) < 1e-12
    print("  OK - EV table + dollarized window math by hand (capped + uncapped + top-1)")

    # H1 walk: caps bind by category; tau skips thin; caps=None == run_policy('reservation')
    W1 = {"market": "tc-temp-w1-2026-06-10-gte70", "cat": "weather", "open_t": 100, "close_t": 5000,
          "duration": 4900, "open_net": 0.05, "open_c2": 1000, "peak_c2": 1000, "censored": "none",
          "traj": [(100, 0.05)], "flip_ts": []}
    W2 = {**W1, "market": "tc-temp-w2-2026-06-10-gte70", "open_t": 200, "open_net": 0.02,
          "traj": [(200, 0.02)]}                                                            # per=1.25c < tau
    S1 = {**W1, "market": "aec-mlb-s1-x-2026-06-10", "cat": "sports", "open_t": 300,
          "open_net": 0.06, "close_t": 300.1, "duration": 0.1, "traj": [(300, 0.06)]}
    h1 = h1_replay([W1, W2, S1])
    assert h1["skipped_tau"] == 1 and len(h1["entered"]) == 2, h1
    w1p, s1p = h1["entered"]
    assert w1p["size"] == int((0.20 * 500) // 0.95), w1p       # weather cap $100 -> 105 contracts
    assert s1p["size"] == int((0.10 * 500) // 0.94), s1p       # sports  cap  $50 ->  53 contracts
    ref = run_policy([W1, W2, S1], "reservation", 500.0, 10 ** 9, 0.0, 0.0, OFFSET_H,
                     0.0, VOID_MULT, 1, 0, tau=H1_TAU)
    unc = h1_replay([W1, W2, S1], caps=None)
    assert [(p["market"], p["size"]) for p in unc["entered"]] == \
           [(p["market"], p["size"]) for p in ref["entered"]]
    assert abs(unc["booked"] - ref["total_pnl"]) < 1e-9
    # at L=0.15: W1 survives at open 5c -> +105*0.05 ; S1 closed @+0.1s -> naked, tax 53*u
    hl = h1_at_latency(h1, 0.15)
    assert hl["n_both"] == 1 and hl["n_one"] == 1
    assert abs(hl["realized_usd"] - 105 * 0.05) < 1e-9
    assert abs(hl["naked_tax_usd"]["0.02"] - 53 * 0.02) < 1e-9
    assert abs(hl["net_usd"]["0.02"] - (5.25 - 1.06)) < 1e-9
    print("  OK - frozen H1 walk (category caps + tau skip), caps-off == run_policy, latency outcomes applied")
    print("self-test passed.")


# ============================================================================================
if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="EV consequences of measured leg-fill risk at latency")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--data-dir", default=os.path.join(os.path.dirname(__file__), "..", "..", "data", "cross-arb"))
    ap.add_argument("--edge-min", type=float, default=0.01, help="capturable cohort floor (default 1c, = the probe)")
    ap.add_argument("--json-out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                       "_data", "ev_at_latency_20260611.json"))
    args = ap.parse_args()
    if args.selftest:
        _selftest()
        sys.exit(0)

    data_dir = os.path.abspath(args.data_dir)
    if not glob.glob(os.path.join(data_dir, "transitions-*.jsonl*")):
        print(f"no data at {data_dir} - run `pwsh deploy/pull-data.ps1` first, or `--selftest`.")
        sys.exit(0)
    recs, sessions = load(data_dir)
    n0 = len(recs)
    recs = post_epoch(recs)
    print(f"POST-EPOCH: {len(recs)}/{n0} records at t >= {ECON_REMAP_DEPLOY_TS} (0013 detection-time stamps)")
    eps = build_episodes(recs, sessions)
    build_trajectories(recs, sessions, eps)
    n_miss = attach_c1(recs, eps)
    cohort = capturable(eps, args.edge_min)
    span_s = (recs[-1]["t"] - recs[0]["t"]) if recs else 0.0
    n_rows = crosscheck(eps, args.edge_min)
    print(f"loaded {len(recs)} post-epoch transitions; cohort n={len(cohort)}; c1 joined "
          f"({n_miss} missing -> c2 fallback); cross-check vs shadow_fill OK on {n_rows} grid rows\n")

    sink = {"data_dir": data_dir, "epoch": ECON_REMAP_DEPLOY_TS, "span_s": span_s,
            "n_records": len(recs), "n_cohort": len(cohort), "edge_min": args.edge_min,
            "c1_missing": n_miss, "lats": EV_LATS, "unwinds": UNWINDS, "clip": CLIP,
            "h1_rule": {"tau": H1_TAU, "caps": H1_CAPS, "bankroll": H1_BANKROLL}}
    print(render(cohort, span_s, eps, sink))
    os.makedirs(os.path.dirname(args.json_out), exist_ok=True)
    with open(args.json_out, "w", encoding="utf-8") as f:
        json.dump(sink, f, indent=1)
    print(f"json -> {args.json_out}")
