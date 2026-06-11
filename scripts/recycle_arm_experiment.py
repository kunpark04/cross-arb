"""scripts/recycle_arm_experiment.py — EXPLORATORY probes #8 + #9 (recycle arm / cluster exposure).

*** EXPLORATORY — outside the 0014 frozen protocol (decisions/0014 + research/allocation-prereg-
*** 2026-06-10.md). Method demo on days of data; NOT a result until re-run on multi-week data.
*** This script does NOT touch the frozen confirmatory pipeline; it IMPORTS the shared economics
*** (account_sim/capital_sim/alloc_policy_experiment) and adds two opt-in exploratory levers that
*** prereg section 5 explicitly leaves outside H1's semantics:

  #8 RECYCLE-TIME RECONSIDERATION ARM
     Baseline = the frozen arrival-or-never semantics (a candidate unaffordable at its arrival is
     PERMANENTLY skipped — account_sim / run_policy "reservation"). Arm = when an in-window
     settlement frees capital, re-score every previously-skipped candidate whose edge episode is
     STILL OPEN at that moment (settle time within [open_t, close_t) of the one_per_market
     representative episode) and fund best-booked-edge-first by the same rule (tau gate + caps +
     affordability). Entries at a recycle moment book the episode's OPEN economics by default
     (--recycle-edge twa books the time-weighted-average net instead — the decay-aware sensitivity).

  #9 EVENT-CLUSTER (city-date / game / release) CORRELATED-EXPOSURE
     Measurement: replay the frozen H1 rule (tau=2c + category caps 20/10/5%, $500 — constants
     REFERENCED from the prereg, not re-tuned) and record peak concurrent locked capital per event
     cluster. Cluster key = the market slug truncated right after its event date:
       weather tc-temp-laxhigh-2026-06-10-gte73lt74f -> tc-temp-laxhigh-2026-06-10  == (city, date)
       sports  aec-mlb-lad-pit-2026-06-10            -> itself                      == the game
       econ    urc-us-seasonadj-gte-june-2026-07-02-atl4pt6 -> ...-2026-07-02       == the release
     Lever: --cluster-cap X (OPT-IN, default OFF) limits concurrent locked capital per cluster to
     X*bankroll; the report also prints a {10,20,30}% sweep AS A CURVE (L19: no single swept row is
     "the result"; expect ~0 or negative PnL — it is risk-control unless the curve says otherwise).

The simulator reuses the EXACT per-contract economics by import (L20 — one shared chokepoint):
capturable()/one_per_market()/settle_t()/void_haircut() from capital_sim, _cost_per()/_profit_per()
from alloc_policy_experiment (themselves lifted verbatim from account_sim.run_account). With
tau=0 / no caps / no recycle it reproduces alloc_policy_experiment.run_policy("fifo") exactly
(asserted in --selftest). READ-ONLY; no orders, no edits to any frozen script.

  python scripts/recycle_arm_experiment.py [--capital 500] [--cluster-cap 0.2] [--recycle-edge open|twa]
  python scripts/recycle_arm_experiment.py --selftest
"""
import os, sys, re, json, glob, heapq, time, argparse
sys.path.insert(0, os.path.dirname(__file__))
from analyze_persistence import load, build_episodes, ECON_REMAP_DEPLOY_TS, _pct
from capital_sim import capturable, one_per_market, settle_t, void_haircut, DEPTH_BOUNDARY_NET
from alloc_policy_experiment import _cost_per, _profit_per, run_policy

# ---- frozen H1 constants, REFERENCED from research/allocation-prereg-2026-06-10.md section 1.
#      Replicated here only so this EXPLORATORY replay can diagnose around them; the confirmatory
#      pipeline (prereg section 1b code freeze) is separate and unaffected by this script. ----
H1_TAU       = 0.02                                              # booked per-contract edge floor
H1_CAT_CAPS  = {"weather": 0.20, "sports": 0.10, "econ": 0.05}   # per-pair cap, fraction of bankroll
CAT_CAP_OTHER = 0.05                                             # unmapped category -> most conservative
# frozen economics knobs (prereg section 1 row "Economics"): account_sim defaults
MAX_CLIP, OFFSET_H, VOID_MULT, HAIRCUT = 1000, 28.0, 1.0, 0.0
CAPTURABLE_GATES = dict(edge_min=0.0, window_min=0.0, liq_floor=1, max_age=0)  # drop_restart=True default

_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

EXPL_BANNER = ("EXPLORATORY — outside the 0014 frozen protocol; method demo on ~{span:.2f} d of data; "
               "not a result until re-run on multi-week data.")


def cluster_key(market):
    """Event-cluster key = slug truncated right after its (first) event date. Weather -> (city,date);
    sports -> the game (a doubleheader suffix after the date collapses into the same cluster, which is
    the point: same teams same day = correlated postpone risk); econ -> the release/meeting. A slug
    with no date is its own cluster."""
    s = str(market)
    m = _DATE_RE.search(s)
    return s[:m.end()] if m else s


# =============================================================================================
# CORE SIMULATOR — account_sim economics (imported), event-driven so settlements are first-class
# =============================================================================================
def run_arm(episodes, capital0, tau=0.0, cat_caps=None, cluster_cap=None, recycle=False,
            recycle_edge="open", max_clip=MAX_CLIP, offset_h=OFFSET_H, haircut=HAIRCUT,
            void_mult=VOID_MULT, drop_flat=False):
    """Fixed-bankroll walk over capturable one-per-market candidates in arrival order, with
    settlements processed as timeline events. tau=0/cat_caps=None/cluster_cap=None/recycle=False
    reproduces alloc_policy_experiment.run_policy('fifo') == account_sim (selftest-asserted).

    cat_caps    : {cat: frac} per-PAIR cap (H1_CAT_CAPS replays the frozen H1 rule).
    cluster_cap : frac of bankroll max CONCURRENT locked capital per event cluster (opt-in, #9).
    recycle     : at each in-window settlement, re-score pending (capital/cluster-skipped)
                  candidates whose episode is still open and fund best-booked-edge-first (#8).
    recycle_edge: 'open' books recycle entries at the episode's open economics (optimistic:
                  assumes the open-time edge/depth is still there); 'twa' books the episode's
                  time-weighted-average net (decay-aware sensitivity).
    drop_flat   : opt-in [L20] flat-ladder lens (default OFF == the frozen H1 gate args; the prereg
                  keeps it a reported-alongside diagnostic, never a silent substitute — L15).
    Tie rules: settlement fires before an arrival at the same timestamp (matches account_sim's
    settle_due(open_t)); candidate order is (open_t, market) for determinism."""
    cap = capturable(episodes, CAPTURABLE_GATES["edge_min"], CAPTURABLE_GATES["window_min"],
                     liq_floor=CAPTURABLE_GATES["liq_floor"], max_age=CAPTURABLE_GATES["max_age"],
                     drop_flat=drop_flat)
    cands = sorted(one_per_market(cap), key=lambda e: (e["open_t"], e["market"]))
    if not episodes or not cands:
        return None
    window_end = max(e["close_t"] if e.get("close_t") else e["open_t"] for e in episodes)

    cash = float(capital0)
    held = []                                  # heap: (settle_t, seq, pos)
    realized, entered = [], []
    skips = {"tau": 0, "capital": 0, "cluster0": 0, "partial": 0}
    cluster_locked, cluster_npos, cluster_peak = {}, {}, {}
    cluster_trims = [0]
    pending = []                               # capital/cluster-skipped, recycle-eligible
    recycle_events = []
    seq = [0]

    def _view(e, basis):
        if basis == "twa" and e.get("twa_net") is not None:
            return {**e, "open_net": e["twa_net"]}
        return e

    def _bump_cluster(ck, t, delta, dn):
        cluster_locked[ck] = cluster_locked.get(ck, 0.0) + delta
        cluster_npos[ck] = cluster_npos.get(ck, 0) + dn
        if delta > 0:
            cur = cluster_locked[ck]
            pk = cluster_peak.get(ck)
            if pk is None or cur > pk["peak"]:
                cluster_peak[ck] = {"peak": cur, "t": t, "npos": cluster_npos[ck]}

    def try_fund(e, now, basis, via):
        """Apply the rule: tau gate -> per-pair cap -> cluster cap -> affordability. Returns a
        status string; 'funded' books the position (account_sim economics, imported)."""
        nonlocal cash
        v = _view(e, basis)
        per = _profit_per(v, haircut, void_mult)        # booked per-contract edge (== account_sim)
        if per < tau:
            return "tau"
        cost = _cost_per(v)                             # $ per 1-contract payout pair (== account_sim)
        want = min(e["open_c2"], max_clip)
        if cat_caps is not None:
            frac = cat_caps.get(e["cat"], CAT_CAP_OTHER)
            want = min(want, int((frac * capital0) // cost))
        if want < 1:
            return "cap0"                               # cap below one contract (unreachable at sane params)
        ck = cluster_key(e["market"])
        trimmed = False
        if cluster_cap is not None:
            room = cluster_cap * capital0 - cluster_locked.get(ck, 0.0)
            cwant = int(room // cost) if room > 0 else 0
            if cwant < want:
                trimmed = True
            want = min(want, cwant)
            if want < 1:
                return "cluster0"
        afford = int(cash // cost)
        size = min(want, afford)
        if size < 1:
            return "capital"
        if size < min(e["open_c2"], max_clip):
            skips["partial"] += 1
        if trimmed:
            cluster_trims[0] += 1
        capital = size * cost
        pos = {"market": e["market"], "cat": e["cat"], "cluster": ck, "size": size,
               "capital": capital, "profit": size * per, "entry_t": now, "via": via, "basis": basis,
               "settle_t": settle_t(e["market"], now, offset_h)}
        cash -= capital
        seq[0] += 1
        heapq.heappush(held, (pos["settle_t"], seq[0], pos))
        entered.append(pos)
        _bump_cluster(ck, now, capital, 1)
        return "funded"

    i, n_c = 0, len(cands)
    while True:
        next_arr = cands[i]["open_t"] if i < n_c else None
        next_set = held[0][0] if held else None
        if next_set is not None and next_set <= window_end and (next_arr is None or next_set <= next_arr):
            # ---- SETTLEMENT EVENT (all positions settling at this timestamp = one recycle event)
            s = next_set
            freed_cap = freed_pnl = 0.0
            n_freed = 0
            while held and held[0][0] <= s + 1e-9:
                _, _, p = heapq.heappop(held)
                cash += p["capital"] + p["profit"]
                realized.append(p)
                _bump_cluster(p["cluster"], s, -p["capital"], -1)
                freed_cap += p["capital"]; freed_pnl += p["profit"]; n_freed += 1
            pending = [pe for pe in pending if pe["close_t"] > s]      # still-open: s in [open_t, close_t)
            live = sorted(pending, key=lambda pe: (-_profit_per(_view(pe, recycle_edge), haircut, void_mult),
                                                   pe["market"]))
            ev = {"t": s, "n_settled": n_freed, "freed_capital": freed_cap, "freed_pnl": freed_pnl,
                  "n_pending_live": len(live),
                  "pending_edges": [_profit_per(_view(pe, recycle_edge), haircut, void_mult) for pe in live],
                  "pending_markets": [pe["market"] for pe in live],
                  "n_funded": 0, "funded_capital": 0.0, "funded_profit": 0.0, "funded": []}
            if recycle and live:
                still = []
                for pe in live:
                    res = try_fund(pe, s, recycle_edge, "recycle")
                    if res == "funded":
                        p2 = entered[-1]
                        ev["n_funded"] += 1
                        ev["funded_capital"] += p2["capital"]; ev["funded_profit"] += p2["profit"]
                        ev["funded"].append(pe["market"])
                    elif res in ("capital", "cluster0"):
                        still.append(pe)                 # may free up at a later settlement
                    # tau/cap0 under the recycle basis -> permanently out of pending
                pending = still
            recycle_events.append(ev)
            continue
        if next_arr is None:
            break
        # ---- ARRIVAL EVENT
        e = cands[i]; i += 1
        res = try_fund(e, e["open_t"], "open", "arrival")
        if res == "tau":
            skips["tau"] += 1
        elif res == "capital":
            skips["capital"] += 1
            pending.append(e)
        elif res == "cluster0":
            skips["cluster0"] += 1
            pending.append(e)
        elif res == "cap0":
            skips["capital"] += 1

    unrealized = [p for _, _, p in held]
    realized_pnl = sum(p["profit"] for p in realized)
    unrealized_pnl = sum(p["profit"] for p in unrealized)
    return {
        "tau": tau, "cat_caps": dict(cat_caps) if cat_caps else None, "cluster_cap": cluster_cap,
        "recycle": recycle, "recycle_edge": recycle_edge, "capital0": capital0,
        "window_end": window_end, "candidates": n_c,
        "entered": entered, "realized": realized, "unrealized": unrealized,
        "realized_pnl": realized_pnl, "unrealized_pnl": unrealized_pnl,
        "total_pnl": realized_pnl + unrealized_pnl,
        "cash": cash, "locked_capital": sum(p["capital"] for p in unrealized),
        "skipped_tau": skips["tau"], "skipped_capital": skips["capital"],
        "skipped_cluster": skips["cluster0"], "skipped_partial": skips["partial"],
        "cluster_trims": cluster_trims[0],
        "cluster_peak": cluster_peak,
        "recycle_events": recycle_events,
        "recycle_funded": [p for p in entered if p["via"] == "recycle"],
    }


# =============================================================================================
# REPORT
# =============================================================================================
def _cents(x):
    return 100.0 * x


def _max_cluster_share(r):
    if not r or not r["cluster_peak"]:
        return 0.0, None
    ck, pk = max(r["cluster_peak"].items(), key=lambda kv: kv[1]["peak"])
    return pk["peak"] / r["capital0"], ck


def recycle_section(eps, capital0, recycle_edge, drop_flat=False):
    """#8: {FIFO, H1} x {baseline, +recycle} matrix + recycle-moment detail."""
    O = []; P = O.append
    rules = [("FIFO  (tau=0, no caps)  ", dict(tau=0.0, cat_caps=None)),
             ("H1    (tau=2c, cat caps)", dict(tau=H1_TAU, cat_caps=H1_CAT_CAPS))]
    out = {}
    P("[1] RECYCLE-TIME RECONSIDERATION ARM (#8)   — arm vs frozen arrival-or-never baseline")
    P(f"    {'rule':<26} {'arm':<10} {'n_fund':>6} {'n_real':>6} {'n_lock':>6} "
      f"{'total_pnl':>10} {'realized':>9} {'locked':>9} {'rec_ev':>6} {'rec_fund':>8} {'rec_$cap':>9} {'rec_$pnl':>9}")
    for name, kw in rules:
        base = run_arm(eps, capital0, recycle=False, recycle_edge=recycle_edge, drop_flat=drop_flat, **kw)
        arm = run_arm(eps, capital0, recycle=True, recycle_edge=recycle_edge, drop_flat=drop_flat, **kw)
        out[name.strip().split()[0]] = {"base": base, "arm": arm}
        for tag, r in (("baseline", base), ("+recycle", arm)):
            if r is None:
                P(f"    {name:<26} {tag:<10} (no candidates)"); continue
            P(f"    {name:<26} {tag:<10} {len(r['entered']):>6} {len(r['realized']):>6} "
              f"{len(r['unrealized']):>6} {r['total_pnl']:>10.2f} {r['realized_pnl']:>9.2f} "
              f"{r['unrealized_pnl']:>9.2f} {len(r['recycle_events']):>6} "
              f"{len(r['recycle_funded']):>8} "
              f"{sum(p['capital'] for p in r['recycle_funded']):>9.2f} "
              f"{sum(p['profit'] for p in r['recycle_funded']):>9.2f}")
        if base and arm:
            d = arm["total_pnl"] - base["total_pnl"]
            pct = (100.0 * d / base["total_pnl"]) if base["total_pnl"] else float("nan")
            P(f"    {'':<26} -> arm-vs-baseline dPnL = ${d:+.2f}  ({pct:+.1f}%)   "
              f"[recycle entries booked at {recycle_edge.upper()} economics]")
    # recycle-moment observational detail (H1 baseline: what the frozen rule leaves on the table)
    h1b = out["H1"]["base"]
    if h1b:
        evs = h1b["recycle_events"]
        with_live = [ev for ev in evs if ev["n_pending_live"] > 0]
        all_edges = [x for ev in with_live for x in ev["pending_edges"]]
        P("")
        P("    RECYCLE-MOMENT DETAIL (H1 baseline, observational — n gates everything):")
        P(f"      in-window settlement timestamps (recycle events) : {len(evs)}   "
          f"(positions settled in-window: {len(h1b['realized'])})")
        P(f"      events with >=1 previously-skipped candidate still open : {len(with_live)}")
        if all_edges:
            n_distinct = len({m for ev in with_live for m in ev["pending_markets"]})
            P(f"      still-open skipped candidates at those moments         : {len(all_edges)} "
              f"candidate-moments, {n_distinct} distinct candidates")
            P(f"      their booked per-contract edge (c): median {_cents(_pct(all_edges, 50)):.2f}  "
              f"p75 {_cents(_pct(all_edges, 75)):.2f}  max {_cents(max(all_edges)):.2f}")
        else:
            P("      still-open skipped candidates across those moments     : 0")
    return "\n".join(O), out


def cluster_section(eps, capital0, drop_flat=False):
    """#9 measurement: event-cluster concentration under the frozen H1 rule (no cluster cap)."""
    O = []; P = O.append
    r = run_arm(eps, capital0, tau=H1_TAU, cat_caps=H1_CAT_CAPS, drop_flat=drop_flat)
    P("[2] EVENT-CLUSTER CONCENTRATION under the frozen H1 rule (#9 measurement; NO cluster cap)")
    if r is None:
        P("    (no candidates)"); return "\n".join(O), None
    peaks = sorted(r["cluster_peak"].items(), key=lambda kv: -kv[1]["peak"])
    shares = [pk["peak"] / capital0 for _, pk in peaks]
    mx, mck = _max_cluster_share(r)
    P(f"    funded positions {len(r['entered'])} across {len(peaks)} clusters "
      f"(bankroll ${capital0:,.0f}; share = peak concurrent locked capital / bankroll)")
    P(f"    MAX cluster share : {100*mx:.1f}%   ({mck})")
    if shares:
        P(f"    share distribution: median {100*_pct(shares,50):.1f}%  p75 {100*_pct(shares,75):.1f}%  "
          f"max {100*max(shares):.1f}%   | clusters >=10%: {sum(1 for s in shares if s >= 0.10)}  "
          f">=20%: {sum(1 for s in shares if s >= 0.20)}  >=30%: {sum(1 for s in shares if s >= 0.30)}")
    P(f"    {'cluster (top 8 by peak $)':<44} {'cat':<8} {'peak$':>8} {'share':>7} {'npos':>5}")
    for ck, pk in peaks[:8]:
        cat = next((p["cat"] for p in r["entered"] if p["cluster"] == ck), "?")
        P(f"    {ck:<44} {cat:<8} {pk['peak']:>8.2f} {100*pk['peak']/capital0:>6.1f}% {pk['npos']:>5}")
    by_cat = {}
    for ck, pk in peaks:
        cat = next((p["cat"] for p in r["entered"] if p["cluster"] == ck), "?")
        cur = by_cat.get(cat)
        if cur is None or pk["peak"] > cur[0]:
            by_cat[cat] = (pk["peak"], ck)
    P("    max share by category: " + "   ".join(
        f"{c}={100*v[0]/capital0:.1f}% ({v[1]})" for c, v in sorted(by_cat.items())))
    return "\n".join(O), r


def cluster_cap_sweep(eps, capital0, h1_nocap, caps=(0.10, 0.20, 0.30), drop_flat=False):
    """#9 lever: H1 + cluster cap curve. L19: the CURVE is the output; no single row is the result."""
    O = []; P = O.append
    P("[3] CLUSTER-CAP SWEEP on H1 (OPT-IN lever, default OFF; curve only — no swept row is 'the result')")
    if h1_nocap is None:
        P("    (no candidates)"); return "\n".join(O), {}
    base_pnl = h1_nocap["total_pnl"]
    P(f"    {'cap':>6} {'n_fund':>6} {'trims':>6} {'rejects':>8} {'binds/fund':>10} "
      f"{'max_clu_share':>13} {'total_pnl':>10} {'dPnL vs no-cap':>15}")
    P(f"    {'(off)':>6} {len(h1_nocap['entered']):>6} {'-':>6} {'-':>8} {'-':>10} "
      f"{100*_max_cluster_share(h1_nocap)[0]:>12.1f}% {base_pnl:>10.2f} {'-':>15}")
    grid = {}
    for cc in caps:
        r = run_arm(eps, capital0, tau=H1_TAU, cat_caps=H1_CAT_CAPS, cluster_cap=cc,
                    drop_flat=drop_flat)
        binds = r["cluster_trims"] + r["skipped_cluster"]
        denom = len(r["entered"]) + r["skipped_cluster"]
        grid[cc] = r
        P(f"    {100*cc:>5.0f}% {len(r['entered']):>6} {r['cluster_trims']:>6} {r['skipped_cluster']:>8} "
          f"{binds:>4}/{denom:<5} {100*_max_cluster_share(r)[0]:>12.1f}% {r['total_pnl']:>10.2f} "
          f"{r['total_pnl']-base_pnl:>+15.2f}")
    P("    (binds/fund = cap-trimmed fills + cap-rejected candidates, over funded+rejected; a reject")
    P("     is a POSITIVE-edge trade refused — per L15 this stays an opt-in risk lens, never a default.)")
    return "\n".join(O), grid


def full_report(eps, capital0, recycle_edge, label, drop_flat=False):
    O = []; P = O.append
    if not eps:
        return f"({label}: no episodes)", {}
    t0 = min(e["open_t"] for e in eps); t1 = max(e["open_t"] for e in eps)
    span_d = max((t1 - t0) / 86400.0, 1e-9)
    n_cand = len(one_per_market(capturable(eps, 0.0, 0.0, liq_floor=1, max_age=0,
                                           drop_flat=drop_flat)))
    P("=" * 96)
    P(EXPL_BANNER.format(span=span_d))
    P(f"DATASET [{label}]  span {span_d:.2f} d  ({time.strftime('%Y-%m-%d %H:%M', time.gmtime(t0))} .. "
      f"{time.strftime('%Y-%m-%d %H:%M', time.gmtime(t1))} UTC)   capturable one-per-market candidates: {n_cand}"
      + ("   [drop_flat LENS ON — diagnostic, not the frozen gates]" if drop_flat else ""))
    P(f"frozen-H1 reference constants (prereg, NOT re-tuned): tau={_cents(H1_TAU):.1f}c  "
      f"caps w/s/e={int(100*H1_CAT_CAPS['weather'])}/{int(100*H1_CAT_CAPS['sports'])}/"
      f"{int(100*H1_CAT_CAPS['econ'])}%  bankroll=${capital0:,.0f}  clip={MAX_CLIP}  offset={OFFSET_H:.0f}h  "
      f"void_mult={VOID_MULT}  haircut={HAIRCUT}")
    P("=" * 96)
    sec1, runs = recycle_section(eps, capital0, recycle_edge, drop_flat=drop_flat)
    P(sec1); P("")
    sec2, h1_nocap = cluster_section(eps, capital0, drop_flat=drop_flat)
    P(sec2); P("")
    sec3, grid = cluster_cap_sweep(eps, capital0, h1_nocap, drop_flat=drop_flat)
    P(sec3)
    P("=" * 96)
    return "\n".join(O), {"runs": runs, "h1_nocap": h1_nocap, "cap_grid": grid,
                          "span_d": span_d, "n_cand": n_cand}


def slim(r):
    """JSON-friendly summary of a run (drop per-position lists, keep the decision numbers)."""
    if r is None:
        return None
    mx, mck = _max_cluster_share(r)
    return {
        "tau": r["tau"], "cat_caps": r["cat_caps"], "cluster_cap": r["cluster_cap"],
        "recycle": r["recycle"], "recycle_edge": r["recycle_edge"],
        "candidates": r["candidates"], "n_entered": len(r["entered"]),
        "n_realized": len(r["realized"]), "n_unrealized": len(r["unrealized"]),
        "realized_pnl": round(r["realized_pnl"], 4), "unrealized_pnl": round(r["unrealized_pnl"], 4),
        "total_pnl": round(r["total_pnl"], 4),
        "skipped_tau": r["skipped_tau"], "skipped_capital": r["skipped_capital"],
        "skipped_cluster": r["skipped_cluster"], "cluster_trims": r["cluster_trims"],
        "n_recycle_events": len(r["recycle_events"]),
        "n_recycle_events_with_live_pending": sum(1 for ev in r["recycle_events"] if ev["n_pending_live"] > 0),
        "n_recycle_funded": len(r["recycle_funded"]),
        "recycle_funded_capital": round(sum(p["capital"] for p in r["recycle_funded"]), 4),
        "recycle_funded_profit": round(sum(p["profit"] for p in r["recycle_funded"]), 4),
        "recycle_funded_markets": [p["market"] for p in r["recycle_funded"]],
        "max_cluster_share": round(mx, 4), "max_cluster": mck,
        "cluster_peaks": {k: {"peak": round(v["peak"], 2), "npos": v["npos"]}
                          for k, v in sorted(r["cluster_peak"].items(), key=lambda kv: -kv[1]["peak"])},
    }


# =============================================================================================
# SELF-TEST — synthetic sets where the answers are known
# =============================================================================================
def _selftest():
    print("recycle-arm / cluster-cap self-test (EXPLORATORY script)")
    # --- cluster keys per live slug conventions
    assert cluster_key("tc-temp-laxhigh-2026-06-10-gte73lt74f") == "tc-temp-laxhigh-2026-06-10"
    assert cluster_key("tc-temp-sfohigh-2026-06-10-lt64f") == "tc-temp-sfohigh-2026-06-10"
    assert cluster_key("aec-mlb-lad-pit-2026-06-09") == "aec-mlb-lad-pit-2026-06-09"
    assert cluster_key("urc-us-seasonadj-gte-june-2026-07-02-atl4pt4") == "urc-us-seasonadj-gte-june-2026-07-02"
    assert cluster_key("rdc-usfed-fomc-2026-06-17-hike25bps") == "rdc-usfed-fomc-2026-06-17"
    assert cluster_key("nodate-market") == "nodate-market"
    print("  OK - cluster keys: weather (city,date) / sports game / econ release / no-date own cluster")

    base = {"cat": "weather", "duration": 100, "peak_c2": 1000, "open_age": 0}

    # --- 1) ECONOMICS EQUIVALENCE: tau=0/no-caps/no-recycle == run_policy('fifo');
    #        tau=2c/no-caps == run_policy('reservation', tau=0.02). Same entries, sizes, PnL, splits.
    #        e1 is a no-date slug so it SETTLES IN-WINDOW (open+1h): both implementations must free
    #        and re-deploy that cash identically (the recycling clock), not just match statically.
    eqs = [
        {**base, "market": "nodate-weather-eq1", "open_t": 1000, "open_net": 0.02,
         "open_c2": 400, "close_t": 1100, "twa_net": 0.02},        # settles 4600, in-window
        {**base, "market": "tc-temp-bbbhigh-2026-06-10-gte70lt71f", "open_t": 2000, "open_net": 0.05,
         "open_c2": 300, "close_t": 2100, "twa_net": 0.05},
        {**base, "market": "aec-mlb-xxx-yyy-2026-06-10", "cat": "sports", "open_t": 3000,
         "open_net": 0.03, "open_c2": 200, "close_t": 3100, "twa_net": 0.03},
        {**base, "market": "tc-temp-dddhigh-2026-06-10-gte70lt71f", "open_t": 5000, "open_net": 0.04,
         "open_c2": 50, "close_t": 99000, "twa_net": 0.04},        # funded from e1's recycled cash
    ]
    for pol, tau in (("fifo", 0.0), ("reservation", 0.02)):
        ref = run_policy(eqs, pol, 500.0, 1000, 0.0, 0.0, 28.0, 0.0, 1.0, 1, 0,
                         tau=(tau if pol == "reservation" else None))
        mine = run_arm(eqs, 500.0, tau=tau)
        assert abs(ref["total_pnl"] - mine["total_pnl"]) < 1e-9, (pol, ref["total_pnl"], mine["total_pnl"])
        assert abs(ref["realized_pnl"] - mine["realized_pnl"]) < 1e-9
        assert [(p["market"], p["size"]) for p in ref["entered"]] == \
               [(p["market"], p["size"]) for p in mine["entered"]], pol
        assert ref["skipped_capital"] == mine["skipped_capital"]
        assert len(ref["realized"]) == len(mine["realized"]) and \
               len(ref["unrealized"]) == len(mine["unrealized"])
    f_ref = run_policy(eqs, "fifo", 500.0, 1000, 0.0, 0.0, 28.0, 0.0, 1.0, 1, 0)
    assert f_ref["realized_pnl"] > 0, "equivalence scenario must exercise an in-window settlement"
    print("  OK - tau=0 == run_policy fifo; tau=2c == run_policy reservation (entries, sizes, PnL, splits,")
    print("       including the in-window settle->recycle-cash clock)")

    # --- 2) RECYCLE ARM: A eats the bankroll, settles in-window; B,D skipped at arrival.
    #        D's episode closes BEFORE the settlement -> must NOT be recycle-funded. B is still open
    #        -> funded by the arm at the settle moment; the baseline never funds it.
    A = {**base, "market": "nodate-weather-a", "open_t": 1000, "open_net": 0.02, "open_c2": 600,
         "close_t": 1500, "twa_net": 0.015}                       # settles open+1h = 4600
    B = {**base, "market": "nodate-weather-b", "open_t": 2000, "open_net": 0.04, "open_c2": 600,
         "close_t": 9000, "twa_net": 0.03}                        # still open at 4600
    D = {**base, "market": "nodate-weather-d", "open_t": 2100, "open_net": 0.04, "open_c2": 600,
         "close_t": 4000, "twa_net": 0.04}                        # closed by 4600 -> not fundable
    C = {**base, "market": "nodate-weather-c", "open_t": 5000, "open_net": 0.03, "open_c2": 10,
         "close_t": 100000, "twa_net": 0.03}                      # window_end = 100000
    eps = [A, B, D, C]
    b = run_arm(eps, 500.0, recycle=False)
    a = run_arm(eps, 500.0, recycle=True)
    assert [p["market"] for p in b["entered"]] == [A["market"], C["market"]], b["entered"]
    assert b["skipped_capital"] == 2                               # B and D
    assert len(b["recycle_events"]) >= 1 and b["recycle_funded"] == []
    arm_mkts = [p["market"] for p in a["entered"]]
    assert B["market"] in arm_mkts and D["market"] not in arm_mkts, arm_mkts
    rf = a["recycle_funded"]
    assert [p["market"] for p in rf][0] == B["market"] and rf[0]["via"] == "recycle"
    ev0 = a["recycle_events"][0]
    assert ev0["t"] == 4600 and ev0["n_settled"] == 1 and ev0["n_pending_live"] == 1 and ev0["n_funded"] == 1, ev0
    assert a["total_pnl"] > b["total_pnl"], (a["total_pnl"], b["total_pnl"])
    # equity conservation on a run with realized positions: cash + locked == capital0 + realized_pnl
    assert abs((a["cash"] + a["locked_capital"]) - (500.0 + a["realized_pnl"])) < 1e-6
    print("  OK - recycle arm funds the still-open skip at the settle moment; closed episode excluded;")
    print("       baseline (arrival-or-never) never reconsiders; equity conserved")

    # --- 3) RECYCLE-EDGE BASIS: open_net clears tau at the recycle moment but twa_net does not ->
    #        'open' books it, 'twa' drops it (tau gate re-applied with the decay-aware edge).
    A2 = {**base, "market": "nodate-weather-a2", "open_t": 1000, "open_net": 0.05, "open_c2": 600,
          "close_t": 1500, "twa_net": 0.05}
    B2 = {**base, "market": "nodate-weather-b2", "open_t": 2000, "open_net": 0.04, "open_c2": 600,
          "close_t": 9000, "twa_net": 0.02}    # per(open)=2.25c >= tau ; per(twa)=1.25c < tau
    C2 = {**base, "market": "nodate-weather-c2", "open_t": 5000, "open_net": 0.05, "open_c2": 1,
          "close_t": 100000, "twa_net": 0.05}
    eps2 = [A2, B2, C2]
    r_open = run_arm(eps2, 500.0, tau=H1_TAU, recycle=True, recycle_edge="open")
    r_twa = run_arm(eps2, 500.0, tau=H1_TAU, recycle=True, recycle_edge="twa")
    assert any(p["market"] == B2["market"] for p in r_open["recycle_funded"])
    assert not any(p["market"] == B2["market"] for p in r_twa["entered"])
    print("  OK - recycle-edge basis: OPEN books the recycle entry; TWA re-applies tau and drops it")

    # --- 4) H1 CATEGORY CAPS: per-pair size capped at 20/10/5% of bankroll by category.
    deep = {"open_c2": 100000, "close_t": 100000, "duration": 100, "peak_c2": 100000, "open_age": 0}
    W = {**deep, "cat": "weather", "market": "tc-temp-zzzhigh-2026-06-10-gte70lt71f", "open_t": 1000,
         "open_net": 0.05, "twa_net": 0.05}
    S = {**deep, "cat": "sports", "market": "aec-mlb-qqq-rrr-2026-06-10", "open_t": 2000,
         "open_net": 0.05, "twa_net": 0.05}
    E = {**deep, "cat": "econ", "market": "urc-us-seasonadj-gte-june-2026-07-02-atl4pt4", "open_t": 3000,
         "open_net": 0.05, "twa_net": 0.05}
    r = run_arm([W, S, E], 500.0, tau=H1_TAU, cat_caps=H1_CAT_CAPS)
    sz = {p["market"]: p for p in r["entered"]}
    cost = 1 - 0.05
    assert sz[W["market"]]["size"] == int(0.20 * 500 // cost), sz[W["market"]]
    assert sz[S["market"]]["size"] == int(0.10 * 500 // cost), sz[S["market"]]
    assert sz[E["market"]]["size"] == int(0.05 * 500 // cost), sz[E["market"]]
    print("  OK - H1 per-pair category caps: weather 20% / sports 10% / econ 5% of bankroll")

    # --- 5) CLUSTER CAP (#9): two buckets of the SAME (city,date) cluster. No cap -> both at the
    #        20% pair cap (cluster stacks to ~40%). cap=20% -> second REJECTED; 30% -> TRIMMED;
    #        40% -> both fit. Different-city third market never affected.
    W1 = {**deep, "cat": "weather", "market": "tc-temp-zzzhigh-2026-06-10-gte70lt71f", "open_t": 1000,
          "open_net": 0.05, "twa_net": 0.05}
    W2 = {**deep, "cat": "weather", "market": "tc-temp-zzzhigh-2026-06-10-gte72lt73f", "open_t": 1100,
          "open_net": 0.05, "twa_net": 0.05}
    W3 = {**deep, "cat": "weather", "market": "tc-temp-qqqhigh-2026-06-10-gte70lt71f", "open_t": 1200,
          "open_net": 0.05, "twa_net": 0.05}
    epsW = [W1, W2, W3]
    r0 = run_arm(epsW, 500.0, tau=H1_TAU, cat_caps=H1_CAT_CAPS)
    ckz = cluster_key(W1["market"])
    assert cluster_key(W2["market"]) == ckz and cluster_key(W3["market"]) != ckz
    assert len(r0["entered"]) == 3 and r0["cluster_peak"][ckz]["npos"] == 2
    share0 = r0["cluster_peak"][ckz]["peak"] / 500.0
    assert 0.38 < share0 <= 0.40, share0                          # two 20% pair-caps stacked
    r20 = run_arm(epsW, 500.0, tau=H1_TAU, cat_caps=H1_CAT_CAPS, cluster_cap=0.20)
    assert r20["skipped_cluster"] == 1 and len(r20["entered"]) == 2
    assert all(p["market"] != W2["market"] for p in r20["entered"])
    assert r20["cluster_peak"][ckz]["peak"] / 500.0 <= 0.20 + 1e-9
    r30 = run_arm(epsW, 500.0, tau=H1_TAU, cat_caps=H1_CAT_CAPS, cluster_cap=0.30)
    assert r30["cluster_trims"] == 1 and len(r30["entered"]) == 3
    assert r30["cluster_peak"][ckz]["peak"] / 500.0 <= 0.30 + 1e-9
    w2 = [p for p in r30["entered"] if p["market"] == W2["market"]][0]
    assert w2["size"] < sz[W["market"]]["size"]                   # trimmed below its pair cap
    r40 = run_arm(epsW, 500.0, tau=H1_TAU, cat_caps=H1_CAT_CAPS, cluster_cap=0.40)
    assert r40["cluster_trims"] == 0 and r40["skipped_cluster"] == 0 and len(r40["entered"]) == 3
    # PnL ordering: the cap only ever REMOVES positive-edge size -> PnL(cap) <= PnL(no cap)
    assert r20["total_pnl"] <= r30["total_pnl"] <= r0["total_pnl"] + 1e-9
    print("  OK - cluster cap: stacks measured (~40%), 20% rejects, 30% trims, 40% clears; PnL monotone")
    print("self-test passed.")


# =============================================================================================
if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="EXPLORATORY (outside 0014): recycle-time reconsideration arm (#8) + "
                    "event-cluster exposure measurement / opt-in cluster cap (#9)")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--data-dir", default=os.path.join(os.path.dirname(__file__), "..", "..", "data", "cross-arb"))
    ap.add_argument("--capital", type=float, default=500.0, help="bankroll $ (H1 verdict arm = 500)")
    ap.add_argument("--recycle-edge", choices=("open", "twa"), default="open",
                    help="economics basis for recycle-moment entries (open = episode-open, twa = decay-aware)")
    ap.add_argument("--cluster-cap", type=float, default=None,
                    help="OPT-IN (#9 lever, default OFF): max concurrent locked capital per event "
                         "cluster as a fraction of bankroll (e.g. 0.2)")
    ap.add_argument("--drop-flat", action="store_true",
                    help="OPT-IN [L20] flat-ladder lens (diagnostic; frozen H1 gates keep it OFF)")
    ap.add_argument("--json-out", default=None,
                    help="write the numeric summary JSON here (default scripts/_data/recycle_cluster_probe-<utcdate>.json)")
    a = ap.parse_args()
    if a.selftest:
        _selftest(); sys.exit(0)

    data_dir = os.path.abspath(a.data_dir)
    if not glob.glob(os.path.join(data_dir, "transitions-*.jsonl*")):
        print(f"no data at {data_dir} — run `pwsh deploy/pull-data.ps1` first, or `--selftest`."); sys.exit(0)
    recs, sessions = load(data_dir)
    print(f"loaded {len(recs)} transitions + {len(sessions)} censoring events from {data_dir}")
    eps = build_episodes(recs, sessions)
    eps_pe = [e for e in eps if e["open_t"] >= ECON_REMAP_DEPLOY_TS]
    print(f"episodes: {len(eps)} total; post-epoch (open_t >= {ECON_REMAP_DEPLOY_TS}): {len(eps_pe)}\n")

    txt_full, res_full = full_report(eps, a.capital, a.recycle_edge,
                                     "FULL loader-corrected dataset (PRIMARY read)", drop_flat=a.drop_flat)
    print(txt_full)
    print()
    txt_pe, res_pe = full_report(eps_pe, a.capital, a.recycle_edge,
                                 "POST-0013-EPOCH-ONLY slice", drop_flat=a.drop_flat)
    print(txt_pe)

    extra = None
    if a.cluster_cap is not None:
        extra = run_arm(eps, a.capital, tau=H1_TAU, cat_caps=H1_CAT_CAPS, cluster_cap=a.cluster_cap,
                        drop_flat=a.drop_flat)
        mx, mck = _max_cluster_share(extra)
        print(f"\n--cluster-cap {a.cluster_cap:.2f} single run (H1 + cluster cap, full dataset): "
              f"funded {len(extra['entered'])}  trims {extra['cluster_trims']}  rejects {extra['skipped_cluster']}  "
              f"total_pnl ${extra['total_pnl']:.2f}  max cluster share {100*mx:.1f}% ({mck})")

    print("\nCAVEATS")
    print("  - EXPLORATORY — outside the 0014 frozen protocol; nothing here amends H1/H2 or their gates.")
    print("  - paper/gross (account_sim caveats apply: fees+spread netted; latency/leg-fill/slippage NOT).")
    print("  - recycle entries assume the episode-OPEN edge+depth are still takeable at the recycle moment")
    print("    (optimistic); --recycle-edge twa is the decay-aware sensitivity. Later re-openings of an")
    print("    already-closed market are NOT counted as fundable (conservative; one_per_market).")
    print("  - settlement is the slug-date+offset proxy (account_sim); recycle-event COUNT inherits it.")
    print("  - cluster cap rejects/trims POSITIVE-edge trades: risk-control lens, opt-in, default OFF (L15).")

    # numeric outputs -> scripts/_data/
    out_path = a.json_out or os.path.join(os.path.dirname(__file__), "_data",
                                          f"recycle_cluster_probe-{time.strftime('%Y%m%d', time.gmtime())}"
                                          + ("-dropflat" if a.drop_flat else "") + ".json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    payload = {
        "generated_utc": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
        "label": "EXPLORATORY — outside the 0014 frozen protocol; method demo; "
                 "not a result until re-run on multi-week data",
        "capital": a.capital, "recycle_edge": a.recycle_edge, "drop_flat_lens": a.drop_flat,
        "h1_constants": {"tau": H1_TAU, "cat_caps": H1_CAT_CAPS, "max_clip": MAX_CLIP,
                         "offset_h": OFFSET_H, "void_mult": VOID_MULT, "haircut": HAIRCUT},
        "full": {
            "span_d": res_full.get("span_d"), "n_candidates": res_full.get("n_cand"),
            "runs": {k: {"base": slim(v["base"]), "arm": slim(v["arm"])}
                     for k, v in (res_full.get("runs") or {}).items()},
            "h1_nocap": slim(res_full.get("h1_nocap")),
            "cluster_cap_grid": {str(k): slim(v) for k, v in (res_full.get("cap_grid") or {}).items()},
        },
        "post_epoch": {
            "span_d": res_pe.get("span_d"), "n_candidates": res_pe.get("n_cand"),
            "runs": {k: {"base": slim(v["base"]), "arm": slim(v["arm"])}
                     for k, v in (res_pe.get("runs") or {}).items()},
            "h1_nocap": slim(res_pe.get("h1_nocap")),
            "cluster_cap_grid": {str(k): slim(v) for k, v in (res_pe.get("cap_grid") or {}).items()},
        },
        "cluster_cap_single": slim(extra),
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=1)
    print(f"\nnumeric summary -> {out_path}")
