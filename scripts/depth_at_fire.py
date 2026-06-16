#!/usr/bin/env python3
"""
depth_at_fire.py — does the DISPLAYED cross-venue depth at fire predict whether a fire LOCKS?

The council's adjudicating experiment (2026-06-16, tasks/council-transcript-2026-06-16-pmus-fill-wall.md).
The pmus hedge leg fails to fill on ~85% of fires. Two worlds hide behind that one number:

  World 1 (real-but-thin book): WITHIN a category, locks have MORE displayed paired depth at fire than
    misses -> a depth gate ("fire only when depth_c2 >= X") would raise conversion. THAT is the scale
    unlock (one conditional), and breadth then becomes the multiplier.
  World 2 (phantom / adversarial book): locks and misses have the SAME displayed depth -> the displayed
    book is a mirage, the surviving ~12% are adversely selected, and breadth/cap-raise scale a
    negatively-selected book. Research, not a business.

This script reads which world we're in off data already in hand.

DATA: bot-rs/executions.jsonl. Each fire emits, in file (=time) order:
  - `book`  phase="entry": pm_bid/pm_ask, k_bid/k_ask (YES touch on each venue), and
      depth_c2 = cross-venue fillable PAIRS while the marginal gross pair-edge stays >= 2c
      (book.rs::depth_curve, a two-pointer merge CAPPED BY THE THINNER LEG -> it already reflects
      pmus-side fillability). Logged SYNCHRONOUSLY at fire (live.rs:477) BEFORE the submit, so it is
      the PRE-fire displayed book -> NO look-ahead (contrast the p_hedge clock-skew trap, L44).
  - `approved`: edge_net_c (the bot's net-edge metric, the 1.5c-floor units), dir (PK|KP).
  - `submit`(s): the legs.
  - `fire_outcome`: lock | abort_clean | recover | abort_ambiguous | naked_halt.

OUTCOME CODING (did the pmus HEDGE fill?):
  lock = 1 (both legs filled, hedge filled).
  everything else = 0 (hedge missed). NB `recover` = the LIVE leg filled but the pmus hedge
  FOK-rejected -> still a hedge MISS (and a real-money event the bot auto-flattened).

CRITICAL CONFOUND (why this script stratifies): depth_c2 is correlated with CATEGORY. [0025] found
sports shows HIGH displayed depth but aborts MORE, while weather shows LOW displayed depth (median ~2)
yet fills. A POOLED locks-vs-misses comparison is therefore Simpson's-paradox-prone. We report the
pooled number ONLY to expose the confound, then the per-category split that actually answers the fork.

SEPARATION METRIC: AUC = P(depth of a random LOCK > depth of a random MISS), tie-corrected (the
Mann-Whitney U statistic / (nL*nM)). 0.50 = depth is useless (World 2). >0.50 = locks are DEEPER than
misses (World 1: a high-depth gate helps). <0.50 = aborts are deeper ([0025]'s inversion: a high-depth
gate would select TOWARD misses). A stratified label-shuffle permutation p-value guards the small cells.

Usage:
  python scripts/depth_at_fire.py [path/to/executions.jsonl]   # default: ../bot-rs/executions.jsonl
  python scripts/depth_at_fire.py --selftest
"""
from __future__ import annotations
import argparse
import json
import random
import statistics
import sys
from collections import defaultdict
from pathlib import Path

PERM_SEED = 20260616  # reproducible permutation p-values
N_PERM = 20000

# pmus hedge filled iff the fire LOCKED. Everything else is a hedge miss.
LOCK = "lock"
MISS_RESULTS = {"abort_clean", "recover", "abort_ambiguous", "naked_halt"}


def pmus_filled(f: dict) -> bool:
    """Robustness coding (WARN-1, stats review 2026-06-16): the LITERAL 'did the pmus leg fill?' label,
    distinct from 'did the arb LOCK?'. In recover/naked_halt the `detail` field is the FILLED leg's
    venue (bookkeeping.rs:247) — so a `recover/Pmus` is a pmus FILL that just didn't lock. lock => both
    legs filled => pmus filled. This re-coding does NOT flip the World-2 verdict (verified)."""
    if f["result"] == LOCK:
        return True
    if f["result"] in ("recover", "naked_halt"):
        return "pmus" in (f.get("detail") or "").lower()
    return False


# ---------------------------------------------------------------------------------------------------
# categorization
# ---------------------------------------------------------------------------------------------------
def categorize(slug: str) -> tuple[str, str]:
    """(category, league) from the pair slug. category is the STRATUM; league is a finer sports tag.
    `aec-{league}-...` is the pmus sports-event prefix — take the league token from the slug itself
    (data-driven; an enumerated list silently mis-bucketed aec-ufc-/aec-valorant- into `other`,
    manufacturing a spurious n=1 World-1-looking cell — stats review 2026-06-16, [WARN]→fixed)."""
    s = slug.lower()
    if s.startswith("tc-temp-") or "-temp-" in s:
        return "weather", "weather"
    if s.startswith("atc-fwc-") or "-fwc-" in s:
        return "worldcup", "fwc"
    if s.startswith("aec-"):
        parts = s.split("-")
        return "sports", (parts[1] if len(parts) > 1 else "unknown")
    return "other", "other"


# ---------------------------------------------------------------------------------------------------
# fire grouping — a chronological state machine over the append-only (=time-ordered) log
# ---------------------------------------------------------------------------------------------------
def group_fires(records: list[dict]) -> list[dict]:
    """Walk the log; an entry `book` opens a fire for its market, `approved` enriches it, and the next
    `fire_outcome` closes it. Repeated fires of the same market are handled (each outcome closes one).
    A fire_outcome with no preceding entry-book is emitted with depth_c2=None (pre-instrumentation /
    censored) so it is COUNTED but excluded from the depth analysis."""
    pending: dict[str, dict] = {}
    fires: list[dict] = []
    for r in records:
        ev = r.get("event")
        mkt = r.get("market")
        if ev == "book" and r.get("phase") == "entry":
            pending[mkt] = {
                "depth_c2": r.get("depth_c2"),
                "pm_depth0": r.get("pm_depth0"), "pm_depth2": r.get("pm_depth2"),  # pmus-only ([0027], 2026-06-16+)
                "pm_bid": r.get("pm_bid"), "pm_ask": r.get("pm_ask"),
                "k_bid": r.get("k_bid"), "k_ask": r.get("k_ask"),
                "ts_ms": r.get("ts_ms"), "approved": None,
            }
        elif ev == "approved":
            ctx = pending.get(mkt)
            if ctx is None:
                pending[mkt] = {"depth_c2": None, "pm_bid": None, "pm_ask": None,
                                "k_bid": None, "k_ask": None, "ts_ms": r.get("ts_ms"), "approved": {}}
                ctx = pending[mkt]
            ctx["approved"] = {"edge_net_c": r.get("edge_net_c"), "dir": r.get("dir"),
                               "cost_per": r.get("cost_per")}
        elif ev == "fire_outcome":
            ctx = pending.pop(mkt, None)
            cat, league = categorize(mkt or "")
            appr = (ctx or {}).get("approved") or {}
            pm_bid = (ctx or {}).get("pm_bid")
            pm_ask = (ctx or {}).get("pm_ask")
            pm_spread = (pm_ask - pm_bid) if (pm_bid is not None and pm_ask is not None) else None
            fires.append({
                "market": mkt, "result": r.get("result"), "detail": r.get("detail") or "",
                "category": cat, "league": league,
                "depth_c2": (ctx or {}).get("depth_c2"),
                "pm_depth0": (ctx or {}).get("pm_depth0"), "pm_depth2": (ctx or {}).get("pm_depth2"),
                "edge_net_c": appr.get("edge_net_c"), "dir": appr.get("dir"),
                "pm_bid": pm_bid, "pm_ask": pm_ask, "pm_spread": pm_spread,
                "ts_ms": r.get("ts_ms"),
            })
    return fires


# ---------------------------------------------------------------------------------------------------
# statistics
# ---------------------------------------------------------------------------------------------------
def auc(locks: list[float], misses: list[float]) -> float | None:
    """Tie-corrected AUC = Mann-Whitney U / (nL*nM) = P(lock > miss) + 0.5*P(tie). None if a side empty."""
    nL, nM = len(locks), len(misses)
    if nL == 0 or nM == 0:
        return None
    pooled = sorted([(v, "L") for v in locks] + [(v, "M") for v in misses], key=lambda x: x[0])
    # average ranks (1-based) with tie handling
    ranks: list[float] = [0.0] * len(pooled)
    i = 0
    while i < len(pooled):
        j = i
        while j + 1 < len(pooled) and pooled[j + 1][0] == pooled[i][0]:
            j += 1
        avg = (i + j) / 2.0 + 1.0  # mean of 1-based ranks i+1..j+1
        for k in range(i, j + 1):
            ranks[k] = avg
        i = j + 1
    rank_sum_L = sum(ranks[k] for k in range(len(pooled)) if pooled[k][1] == "L")
    U = rank_sum_L - nL * (nL + 1) / 2.0
    return U / (nL * nM)


def perm_pvalue_stratified(fires: list[dict], key: str) -> tuple[float | None, float | None]:
    """Two-sided permutation p-value for 'locks differ from misses on `key`', SHUFFLING LABELS WITHIN
    each category (so the category confound can't manufacture significance). Statistic = nL-weighted
    mean of per-category (AUC-0.5). Returns (observed_stat, p). None if no usable stratum."""
    strata: dict[str, tuple[list[float], list[float]]] = {}
    for cat in sorted({f["category"] for f in fires}):
        L = [f[key] for f in fires if f["category"] == cat and f["result"] == LOCK and f[key] is not None]
        M = [f[key] for f in fires if f["category"] == cat and f["result"] != LOCK and f[key] is not None]
        if L and M:
            strata[cat] = (L, M)
    if not strata:
        return None, None

    def stat(assign: dict[str, list[int]]) -> float:
        num = den = 0.0
        for cat, (L, M) in strata.items():
            vals = L + M
            labels = assign[cat]
            ls = [vals[i] for i in range(len(vals)) if labels[i] == 1]
            ms = [vals[i] for i in range(len(vals)) if labels[i] == 0]
            a = auc(ls, ms)
            if a is not None:
                num += len(ls) * (a - 0.5)
                den += len(ls)
        return (num / den) if den else 0.0

    base_assign = {cat: [1] * len(L) + [0] * len(M) for cat, (L, M) in strata.items()}
    observed = stat(base_assign)
    rng = random.Random(PERM_SEED)
    ge = 0
    for _ in range(N_PERM):
        assign = {}
        for cat, (L, M) in strata.items():
            lab = [1] * len(L) + [0] * len(M)
            rng.shuffle(lab)
            assign[cat] = lab
        if abs(stat(assign)) >= abs(observed) - 1e-12:
            ge += 1
    return observed, ge / N_PERM


def fmt_dist(vals: list[float]) -> str:
    if not vals:
        return "n=0"
    vs = sorted(vals)
    med = statistics.median(vs)
    q1 = vs[len(vs) // 4] if len(vs) >= 4 else vs[0]
    q3 = vs[(3 * len(vs)) // 4] if len(vs) >= 4 else vs[-1]
    return f"n={len(vs)} med={med:.0f} IQR[{q1:.0f},{q3:.0f}] min={vs[0]:.0f} max={vs[-1]:.0f}"


def best_threshold(fires: list[dict]) -> str:
    """If a depth_c2 gate 'fire only when depth_c2 >= X' were applied, what conversion would survive,
    and how many fires? Scans candidate X. Reports the gate that maximizes conversion subject to
    retaining >= 25% of fires (a gate that keeps 2 fires is not a business)."""
    usable = [f for f in fires if f["depth_c2"] is not None]
    if not usable:
        return "  (no depth_c2 coverage)"
    total = len(usable)
    base_conv = sum(1 for f in usable if f["result"] == LOCK) / total
    cands = sorted({f["depth_c2"] for f in usable})
    rows = [f"  baseline (no gate): {sum(1 for f in usable if f['result']==LOCK)}/{total} = {base_conv*100:.1f}% conversion"]
    best = None
    for x in cands:
        kept = [f for f in usable if f["depth_c2"] >= x]
        if len(kept) < max(4, 0.25 * total):
            continue
        conv = sum(1 for f in kept if f["result"] == LOCK) / len(kept)
        if best is None or conv > best[1]:
            best = (x, conv, len(kept))
    if best:
        rows.append(f"  best gate depth_c2>={best[0]}: {sum(1 for f in usable if f['depth_c2']>=best[0] and f['result']==LOCK)}/{best[2]} "
                    f"= {best[1]*100:.1f}% conversion  (keeps {best[2]}/{total} fires)")
        lift = best[1] - base_conv
        rows.append(f"  -> gate lift: {lift*100:+.1f} pts conversion "
                    f"({'MATERIAL — World 1 signal' if lift > 0.10 else 'negligible — World 2 signal'})")
    else:
        rows.append("  no gate retains >=25% of fires with a higher conversion -> depth_c2 does not separate (World 2)")
    return "\n".join(rows)


# ---------------------------------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------------------------------
def report(fires: list[dict]) -> None:
    n = len(fires)
    with_depth = [f for f in fires if f["depth_c2"] is not None]
    locks = [f for f in fires if f["result"] == LOCK]
    print(f"=== depth-at-fire replay — {n} fires "
          f"({len(with_depth)} with a depth_c2 snapshot; {n - len(with_depth)} pre-instrumentation/censored) ===")
    # outcome tally
    tally = defaultdict(int)
    for f in fires:
        tally[f["result"]] += 1
    print("  outcomes: " + "  ".join(f"{k}={v}" for k, v in sorted(tally.items(), key=lambda x: -x[1])))
    conv = len(locks) / n if n else 0
    print(f"  overall conversion (lock / all fires): {len(locks)}/{n} = {conv*100:.1f}%")

    # ---- THE CONFOUND, exposed: pooled comparison (DO NOT trust this one) ----
    print("\n--- [confound check] POOLED depth_c2, locks vs misses (Simpson-prone — for exposure only) ---")
    pL = [f["depth_c2"] for f in with_depth if f["result"] == LOCK]
    pM = [f["depth_c2"] for f in with_depth if f["result"] != LOCK]
    a = auc(pL, pM)
    print(f"  locks:  {fmt_dist(pL)}")
    print(f"  misses: {fmt_dist(pM)}")
    print(f"  pooled AUC(lock depth > miss depth) = {a:.3f}" if a is not None else "  pooled AUC: n/a")
    if a is not None:
        print(f"    {'(<0.5 => aborts run DEEPER — the [0025] inversion, a category artifact)' if a < 0.5 else '(>0.5 => locks run deeper)'}")

    # ---- THE ANSWER: per-category (stratified) ----
    print("\n--- [the answer] PER-CATEGORY depth_c2, locks vs misses (confound removed) ---")
    for cat in sorted({f["category"] for f in with_depth}):
        cf = [f for f in with_depth if f["category"] == cat]
        cL = [f["depth_c2"] for f in cf if f["result"] == LOCK]
        cM = [f["depth_c2"] for f in cf if f["result"] != LOCK]
        ca = auc(cL, cM)
        nlock = len(cL)
        conv_c = nlock / len(cf) if cf else 0
        print(f"\n  [{cat}]  {len(cf)} fires, {nlock} locks ({conv_c*100:.0f}% conversion)")
        print(f"    lock depth_c2:  {fmt_dist(cL)}")
        print(f"    miss depth_c2:  {fmt_dist(cM)}")
        if ca is not None:
            verdict = "locks DEEPER (World 1 — depth gate could help)" if ca > 0.60 else \
                      "aborts DEEPER (gate selects misses)" if ca < 0.40 else \
                      "NO separation (World 2 — displayed depth is a mirage here)"
            print(f"    AUC = {ca:.3f}  -> {verdict}")
        else:
            print("    AUC: n/a (a side has 0 fires)")
        # pmus touch-spread as a second predictor
        sL = [f["pm_spread"] for f in cf if f["result"] == LOCK and f["pm_spread"] is not None]
        sM = [f["pm_spread"] for f in cf if f["result"] != LOCK and f["pm_spread"] is not None]
        sa = auc(sL, sM)
        if sa is not None:
            print(f"    pmus touch-spread AUC(lock>miss) = {sa:.3f}  "
                  f"(lock spread med={statistics.median(sL):.3f}, miss med={statistics.median(sM):.3f})")

    # ---- the [0027] gap: pmus-SIDE-ONLY depth (not the paired depth_c2) — instrumented 2026-06-16 ----
    print("\n--- [pmus-only depth, [0027] gap] HEDGE-leg resting qty within 2¢, locks vs misses per category ---")
    pm_inst = [f for f in with_depth if f.get("pm_depth2") is not None]
    if not pm_inst:
        print("  pmus-side ladder NOT in this log (pre-2026-06-16 run). book_snapshot now logs pm_depth0 (touch)")
        print("  + pm_depth2 (within 2¢) on the dir-specific HEDGE leg — re-run this after the next live session")
        print("  to test the pmus-ONLY fill signal the paired depth_c2 masked.")
    else:
        print(f"  {len(pm_inst)}/{len(with_depth)} fires carry the pmus-side ladder")
        for cat in sorted({f["category"] for f in pm_inst}):
            cf = [f for f in pm_inst if f["category"] == cat]
            cL = [f["pm_depth2"] for f in cf if f["result"] == LOCK]
            cM = [f["pm_depth2"] for f in cf if f["result"] != LOCK]
            ca = auc(cL, cM)
            if ca is not None:
                verdict = "pmus depth SEPARATES (World 1 at the pmus level!)" if ca > 0.60 else \
                          "no pmus-only separation -> World 2 confirmed at the pmus level"
                print(f"  [{cat}] {len(cf)} fires, {len(cL)} locks — pmus within-2¢ AUC(lock>miss) = {ca:.3f}  ({verdict})")
            else:
                print(f"  [{cat}] {len(cf)} fires, {len(cL)} locks — AUC n/a")

    # ---- robustness: literal 'did pmus fill?' coding (recover/naked_halt detail = the FILLED leg) ----
    print("\n--- [robustness] re-code outcome as the LITERAL 'did the pmus leg fill?' (not 'did it lock?') ---")
    fL = [f["depth_c2"] for f in with_depth if pmus_filled(f)]
    fM = [f["depth_c2"] for f in with_depth if not pmus_filled(f)]
    fa = auc(fL, fM)
    moved = sum(1 for f in with_depth if f["result"] != LOCK and pmus_filled(f))
    print(f"  {moved} non-lock fires actually had the pmus leg fill (recover/naked_halt to Pmus)")
    print(f"  pmus-filled: {fmt_dist(fL)}")
    print(f"  pmus-missed: {fmt_dist(fM)}")
    print(f"  pooled AUC(filled depth > missed depth) = {fa:.3f}  "
          f"({'still no/inverse separation -> World-2 holds under the literal coding' if fa is not None and fa <= 0.55 else 'check'})"
          if fa is not None else "  AUC: n/a")

    # ---- stratified permutation tests ----
    print("\n--- [significance] stratified label-shuffle permutation (labels shuffled WITHIN category) ---")
    for key, label in (("depth_c2", "depth_c2"), ("pm_spread", "pmus touch-spread")):
        obs, p = perm_pvalue_stratified(with_depth, key)
        if obs is None:
            print(f"  {label}: no stratum with both locks and misses")
        else:
            print(f"  {label}: stratified stat (nL-weighted AUC-0.5) = {obs:+.3f}, perm p = {p:.4f}  "
                  f"({'separates' if p < 0.05 else 'NOT significant — consistent with World 2'})")

    # ---- the gate the council asked about ----
    print("\n--- [the gate] would 'fire only when depth_c2 >= X' raise conversion? (pooled & per-category) ---")
    print(best_threshold(with_depth))
    for cat in sorted({f["category"] for f in with_depth}):
        cf = [f for f in with_depth if f["category"] == cat]
        if sum(1 for f in cf if f["result"] == LOCK) >= 2:
            print(f"  [{cat}]")
            print("  " + best_threshold(cf).replace("\n", "\n  "))

    # ---- net edge captured on the locks (the reviewers' fee catch) ----
    print("\n--- [realized] net edge on the locks (edge_net_c is the bot's net-of-fees metric, exec_log.rs:159) ---")
    le = [f["edge_net_c"] for f in locks if f["edge_net_c"] is not None]
    if le:
        print(f"  {len(le)} locks with edge: sum={sum(le):.1f}c  mean={statistics.mean(le):.2f}c  "
              f"min={min(le):.1f}c  max={max(le):.1f}c")
        print(f"  ({len(locks)-len(le)} locks pre-instrumentation, no edge_net_c)")
        by_cat = defaultdict(list)
        for f in locks:
            if f["edge_net_c"] is not None:
                by_cat[f["category"]].append(f["edge_net_c"])
        for cat, vs in sorted(by_cat.items()):
            print(f"    [{cat}] {len(vs)} locks, sum={sum(vs):.1f}c mean={statistics.mean(vs):.2f}c")
    else:
        print("  no locks carry an edge_net_c (all pre-instrumentation)")

    # ---- bottom line ----
    print("\n=== read ===")
    print("  World 1 (depth gate is the scale unlock): per-category AUC > ~0.60 AND a depth_c2>=X gate")
    print("    lifts conversion materially AND the permutation p < 0.05.")
    print("  World 2 (mirage book -> research, not a business): per-category AUC ~0.50, no gate lift,")
    print("    p not significant. The displayed depth does not predict the fill.")


# ---------------------------------------------------------------------------------------------------
def load(path: Path) -> list[dict]:
    out = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def selftest() -> int:
    # synthetic log: 2 markets, each fires twice. Grouping must split repeated fires; AUC must compute.
    recs = [
        {"event": "book", "phase": "entry", "market": "tc-temp-x", "depth_c2": 5,
         "pm_depth0": 3, "pm_depth2": 7, "pm_bid": 0.10, "pm_ask": 0.12, "ts_ms": 1},
        {"event": "approved", "market": "tc-temp-x", "edge_net_c": 2.0, "dir": "KP", "ts_ms": 2},
        {"event": "submit", "market": "tc-temp-x", "venue": "Pmus", "filled": True, "ts_ms": 3},
        {"event": "fire_outcome", "market": "tc-temp-x", "result": "lock", "ts_ms": 4},
        # second fire of the SAME market — a miss with shallower depth
        {"event": "book", "phase": "entry", "market": "tc-temp-x", "depth_c2": 2,
         "pm_bid": 0.10, "pm_ask": 0.20, "ts_ms": 5},
        {"event": "approved", "market": "tc-temp-x", "edge_net_c": 3.0, "dir": "KP", "ts_ms": 6},
        {"event": "fire_outcome", "market": "tc-temp-x", "result": "abort_clean", "ts_ms": 7},
        # a sports fire with NO book (pre-instrumentation) -> censored but counted
        {"event": "fire_outcome", "market": "aec-mlb-a-b-2026", "result": "recover", "ts_ms": 8},
    ]
    fires = group_fires(recs)
    assert len(fires) == 3, f"expected 3 fires, got {len(fires)}"
    by_mkt = defaultdict(list)
    for f in fires:
        by_mkt[f["market"]].append(f)
    assert len(by_mkt["tc-temp-x"]) == 2, "repeated-fire grouping failed"
    lock = next(f for f in fires if f["result"] == "lock")
    miss = next(f for f in fires if f["result"] == "abort_clean")
    assert lock["depth_c2"] == 5 and miss["depth_c2"] == 2, "depth association wrong"
    assert lock["pm_depth2"] == 7 and lock["pm_depth0"] == 3, "pmus-side depth association wrong"
    assert miss["pm_depth2"] is None, "absent pmus-side depth must stay None (graceful pre-instrumentation)"
    assert lock["category"] == "weather" and lock["league"] == "weather"
    censored = next(f for f in fires if f["market"].startswith("aec-mlb"))
    assert censored["depth_c2"] is None and censored["category"] == "sports", "censored fire mishandled"
    # AUC: lock depth 5 > miss depth 2 -> AUC = 1.0
    a = auc([5.0], [2.0])
    assert a == 1.0, f"AUC expected 1.0, got {a}"
    # tie -> 0.5
    assert auc([3.0], [3.0]) == 0.5, "tie AUC should be 0.5"
    # empty side -> None
    assert auc([], [1.0]) is None
    # pm_spread computed
    assert abs(miss["pm_spread"] - 0.10) < 1e-9, "pm_spread wrong"
    print("selftest OK (grouping splits repeated fires, censors pre-instrumentation, AUC + spread correct)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", nargs="?", default=None, help="executions.jsonl (default ../bot-rs/executions.jsonl)")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    path = Path(args.path) if args.path else Path(__file__).resolve().parent.parent / "bot-rs" / "executions.jsonl"
    if not path.exists():
        print(f"FATAL: no exec log at {path}", file=sys.stderr)
        return 1
    fires = group_fires(load(path))
    if not fires:
        print("no fires found in the log", file=sys.stderr)
        return 1
    report(fires)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
