"""scripts/clip_threshold_test.py — TEST the proposed clip-stage allocation design.

alloc_policy_experiment.py showed FIFO-by-arrival loses to a GLOBAL EDGE THRESHOLD (reservation price
tau). This script TESTS the recommended design — **edge threshold tau + CLIP CAP** — and, crucially,
whether it GENERALIZES (an in-sample tau sweep is an ORACLE; a deployable rule must work on data it
was not tuned on). Three tests, all reusing alloc_policy_experiment.run_policy => account_sim economics
(apples-to-apples; the only thing that changes is consideration order + the tau gate + the clip cap):

  A. 2D SURFACE   tau x clip_cap at a fixed bankroll -> PnL surface; isolate clip-only vs tau-only vs both.
  B. OUT-OF-SAMPLE  causal temporal split on ARRIVAL time: fit (tau*,clip*) on the EARLY half, apply it
     FIXED to the LATE half with a fresh bankroll; compare vs FIFO and vs NON-oracle rules (clip-cap-only,
     fixed 2c tau). This is the overfitting test.
  C. FRICTION     re-run under a latency/leg-fill haircut; a threshold should HELP under friction because
     thin arbs (the ones a haircut zeroes out) get skipped instead of eating capital.

READ-ONLY. PnL is paper/gross (same caveats as account_sim). `--selftest` included.

  python scripts/clip_threshold_test.py [--capital 500]
  python scripts/clip_threshold_test.py --selftest
"""
import os, sys, argparse, glob
sys.path.insert(0, os.path.dirname(__file__))
from analyze_persistence import load, build_episodes
from capital_sim import capturable, one_per_market
from alloc_policy_experiment import run_policy, _profit_per

# economics knobs held at account_sim defaults; only tau/clip/haircut/bankroll vary in the tests.
EDGE_MIN, WINDOW_MIN, OFFSET_H, VOID_MULT, LIQ_FLOOR, MAX_AGE = 0.0, 0.0, 28.0, 1.0, 1, 0.0


def P(eps, bankroll, clip, tau, haircut=0.0):
    """Reservation policy (tau gate) at a clip cap. tau=0 -> FIFO-order + clip cap (the clip-only lever)."""
    return run_policy(eps, "reservation", bankroll, clip, EDGE_MIN, WINDOW_MIN, OFFSET_H,
                      haircut, VOID_MULT, LIQ_FLOOR, MAX_AGE, tau=tau)


def n_candidates(eps):
    return len(one_per_market(capturable(eps, EDGE_MIN, WINDOW_MIN, liq_floor=LIQ_FLOOR, max_age=MAX_AGE)))


def split_by_open_t(eps, frac=0.5):
    """CAUSAL temporal split on arrival time: earliest `frac` of arrivals = train, the rest = test."""
    ts = sorted(e["open_t"] for e in eps)
    if not ts:
        return [], [], None
    cut = ts[min(len(ts) - 1, int(len(ts) * frac))]
    early = [e for e in eps if e["open_t"] < cut]
    late = [e for e in eps if e["open_t"] >= cut]
    return early, late, cut


def best_tau_clip(eps, bankroll, taus, clips, haircut=0.0):
    """Grid-search (tau, clip) maximizing total_pnl on `eps` (used on the TRAIN half only)."""
    best = None
    for clip in clips:
        for tau in taus:
            r = P(eps, bankroll, clip, tau, haircut)
            if best is None or r["total_pnl"] > best[0]:
                best = (r["total_pnl"], tau, clip, len(r["entered"]))
    return best  # (pnl, tau, clip, n_funded)


# grids
TAUS = [0.0, 0.005, 0.01, 0.02, 0.03, 0.05, 0.08, 0.10]
CLIPS = [25, 50, 100, 250, 500, 1000]


def test_A_surface(eps, bankroll):
    O = []; w = O.append
    w(f"TEST A — tau x clip-cap PnL SURFACE  (bankroll ${bankroll:,.0f}; total deployed PnL, paper/gross)")
    w(f"  FIFO baseline (no cap, tau=0): ${P(eps, bankroll, 1000, 0.0)['total_pnl']:,.4f}   "
      f"funds {len(P(eps, bankroll, 1000, 0.0)['entered'])}")
    hdr = "  clip\\tau " + "".join(f"{t*100:>8.1f}c" for t in TAUS)
    w(hdr)
    surf = {}
    for clip in CLIPS:
        row = []
        for tau in TAUS:
            r = P(eps, bankroll, clip, tau)
            surf[(clip, tau)] = r["total_pnl"]
            row.append(f"{r['total_pnl']:>9.2f}")
        w(f"  {clip:>6}  " + "".join(row))
    # isolate the levers
    fifo = surf[(1000, 0.0)]
    clip_only = max(surf[(c, 0.0)] for c in CLIPS)              # clip cap, NO threshold
    tau_only = max(surf[(1000, t)] for t in TAUS)               # threshold, NO cap (depth-limited)
    both = max(surf.values())
    bc = max((c for c in CLIPS), key=lambda c: surf[(c, 0.0)])
    bt = max((t for t in TAUS), key=lambda t: surf[(1000, t)])
    bcb = max(surf, key=lambda k: surf[k])
    w("")
    w("  LEVER ISOLATION (best of each):")
    w(f"    FIFO (no cap, no thresh)   : ${fifo:>9.2f}")
    w(f"    clip-cap ONLY (tau=0)      : ${clip_only:>9.2f}  (best clip={bc})   "
      f"{_gain(clip_only, fifo)}")
    w(f"    threshold ONLY (no cap)    : ${tau_only:>9.2f}  (best tau={bt*100:.1f}c)  "
      f"{_gain(tau_only, fifo)}")
    w(f"    BOTH (tau + clip)          : ${both:>9.2f}  (tau={bcb[1]*100:.1f}c clip={bcb[0]})  "
      f"{_gain(both, fifo)}")
    return "\n".join(O), surf


def _gain(x, base):
    if base == 0:
        return f"(+${x-base:.2f} vs fifo)"
    return f"({100*(x-base)/base:+.0f}% vs fifo)"


def test_B_oos(eps, bankroll):
    O = []; w = O.append
    early, late, cut = split_by_open_t(eps, 0.5)
    w(f"TEST B — OUT-OF-SAMPLE  (causal split on arrival time; fresh ${bankroll:,.0f} per half)")
    w(f"  train (early arrivals): {len(early)} eps / {n_candidates(early)} candidate arbs")
    w(f"  test  (late  arrivals): {len(late)} eps / {n_candidates(late)} candidate arbs")
    if n_candidates(early) == 0 or n_candidates(late) == 0:
        w("  one half has no candidates — OOS split not meaningful on this little data."); return "\n".join(O)
    # FIT on train
    bp, btau, bclip, bn = best_tau_clip(early, bankroll, TAUS, CLIPS)
    w(f"  FIT on train -> tau*={btau*100:.1f}c  clip*={bclip}   (train PnL ${bp:,.2f}, funds {bn})")
    w("")
    # APPLY to the unseen test half
    clip_rule = max(1, int(round(0.05 * bankroll / 0.98)))      # non-oracle: <=5% of bankroll per pair
    rows = [
        ("FIFO (no cap, tau=0)         ", P(late, bankroll, 1000, 0.0)),
        ("FITTED (tau*,clip* from train)", P(late, bankroll, bclip, btau)),
        (f"clip-cap ONLY (clip={clip_rule}, 5% rule) ", P(late, bankroll, clip_rule, 0.0)),
        (f"FIXED RULE (tau=2c, clip={clip_rule})    ", P(late, bankroll, clip_rule, 0.02)),
    ]
    base = rows[0][1]["total_pnl"]
    w("  APPLIED TO TEST HALF (the real question — does it beat FIFO on data it never saw?):")
    w(f"    {'policy':<34} {'test PnL':>10}  {'funds':>5}  {'skip_cap':>8}  {'vs fifo':>9}")
    for name, r in rows:
        w(f"    {name:<34} ${r['total_pnl']:>8.2f}  {len(r['entered']):>5}  "
          f"{r['skipped_capital']:>8}  {_gain(r['total_pnl'], base):>9}")
    return "\n".join(O)


def test_C_friction(eps, bankroll):
    O = []; w = O.append
    w(f"TEST C — FRICTION SENSITIVITY  (bankroll ${bankroll:,.0f}; latency/leg-fill haircut on net edge)")
    w("  Does the threshold's advantage GROW as friction zeroes out thin arbs?")
    clip_rule = max(1, int(round(0.05 * bankroll / 0.98)))
    w(f"    {'haircut':>8}  {'FIFO':>10}  {'design(2c+cap)':>15}  {'design vs fifo':>15}")
    for hc in (0.0, 0.005, 0.01, 0.02):
        f = P(eps, bankroll, 1000, 0.0, haircut=hc)["total_pnl"]
        d = P(eps, bankroll, clip_rule, 0.02, haircut=hc)["total_pnl"]
        w(f"    {hc*100:>6.1f}c  ${f:>9.2f}  ${d:>14.2f}  {_gain(d, f):>15}")
    w("  (design = fixed 2c threshold + 5%-of-bankroll clip cap; a non-oracle deployable rule.)")
    return "\n".join(O)


def run_report(eps, bankroll):
    O = []; w = O.append
    t0 = min(e["open_t"] for e in eps); t1 = max(e["open_t"] for e in eps)
    span = max((t1 - t0) / 86400.0, 1e-9)
    w("=" * 92)
    w(f"CLIP-STAGE DESIGN TEST — edge threshold + clip cap   (span {span:.2f} d; "
      f"{n_candidates(eps)} candidate arbs)")
    if span < 1:
        w("  *** < 1 day of data — PRELIMINARY; OOS halves are ~0.4 d each, SMALL n. Directional only. ***")
    w("=" * 92); w("")
    a, _ = test_A_surface(eps, bankroll); w(a); w("")
    w(test_B_oos(eps, bankroll)); w("")
    w(test_C_friction(eps, bankroll)); w("")
    w("CAVEATS: paper/gross (fees+spread netted; latency/leg-fill/slippage NOT — except where C adds a")
    w("  haircut); one position/market (C8); <1 day data so OOS is a METHOD demo, not validation — real")
    w("  OOS needs the weeks of ms+px data now accumulating. tau in A is in-sample (oracle ceiling);")
    w("  B's FIXED RULE (2c + 5% cap) is the deployable, non-oracle claim to judge.")
    w("=" * 92)
    return "\n".join(O)


def _selftest():
    print("clip-threshold self-test")
    base = {"cat": "weather", "duration": 100, "peak_c2": 1000, "open_age": 0, "close_t": 100000}

    # --- SCENARIO 1: MONOPOLIZATION (capital NOT scarce after capping) -> the CLIP CAP is the lever. ---
    # one DEEP thin arb arrives first; under FIFO-no-cap it eats the whole bankroll; capping it lets the
    # shallow FAT arbs in too. (Threshold need NOT beat clip-cap here — capital isn't binding.)
    deep_thin = {**base, "market": "tc-temp-deepthin-2026-06-10-gte70", "open_t": 1000,
                 "open_net": 0.01, "open_c2": 100000}                      # huge depth, tiny edge
    fat = [{**base, "market": f"tc-temp-fat{i}-2026-06-10-gte70", "open_t": 1000 + i,
            "open_net": 0.20, "open_c2": 50} for i in range(1, 6)]          # small depth, big edge
    eps1 = [deep_thin] + fat
    BK = 500.0
    fifo1 = P(eps1, BK, 1000000, 0.0)
    capped1 = P(eps1, BK, 50, 0.0)
    thresh1 = P(eps1, BK, 50, 0.02)
    assert fifo1["entered"][0]["market"] == deep_thin["market"]            # FIFO funds the thin deep arb first
    assert len(fifo1["entered"]) == 1 and fifo1["skipped_capital"] == 5    # ...and it monopolizes the bankroll
    assert capped1["total_pnl"] > fifo1["total_pnl"], (capped1["total_pnl"], fifo1["total_pnl"])  # cap >> FIFO
    assert all(p["market"] != deep_thin["market"] for p in thresh1["entered"]), "2c tau must skip the 1c arb"

    # --- SCENARIO 2: BINDING (more good arbs than bankroll, even after capping) -> the THRESHOLD adds. ---
    # 10 thin arbs arrive BEFORE 10 fat arbs; bankroll funds ~9 capped positions. capped-FIFO spends them
    # all on thin (arrived first); the threshold skips thin and funds the fat -> threshold >> clip-cap-alone.
    thin = [{**base, "market": f"tc-temp-thin{i}-2026-06-10-gte70", "open_t": 1000 + i,
             "open_net": 0.01, "open_c2": 50} for i in range(10)]
    fat2 = [{**base, "market": f"tc-temp-fat2-{i}-2026-06-10-gte70", "open_t": 2000 + i,
             "open_net": 0.20, "open_c2": 50} for i in range(10)]
    eps2 = thin + fat2
    BKb = 450.0
    capped2 = P(eps2, BKb, 50, 0.0)        # FIFO-order + cap: funds thin-first, crowds out fat
    thresh2 = P(eps2, BKb, 50, 0.02)       # threshold: skips thin, funds fat
    assert thresh2["total_pnl"] > capped2["total_pnl"], (thresh2["total_pnl"], capped2["total_pnl"])
    assert all(p["cat"] == "weather" and "fat2" in p["market"] for p in thresh2["entered"]), "tau funds only fat"
    # friction: zero out the thin arbs with a 1c haircut -> the design still beats FIFO (FIFO wastes capital
    # on now-zero-edge thin arbs; the threshold funds only still-profitable fat). RELATIVE edge widens.
    ffric = P(eps2, BKb, 1000000, 0.0, haircut=0.01)["total_pnl"]
    dfric = P(eps2, BKb, 50, 0.02, haircut=0.01)["total_pnl"]
    assert dfric > ffric, (dfric, ffric)

    # OOS split returns disjoint, time-ordered halves
    early, late, cut = split_by_open_t(eps2, 0.5)
    assert early and late and max(e["open_t"] for e in early) < min(e["open_t"] for e in late)

    print("  OK — clip cap beats FIFO under monopolization; threshold beats clip-cap-alone when the "
          "bankroll binds; threshold skips thin arbs; design > FIFO under friction; OOS split is causal.")
    print("self-test passed.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="test the clip-stage design: edge threshold + clip cap")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--data-dir", default=os.path.join(os.path.dirname(__file__), "..", "..", "data", "cross-arb"))
    ap.add_argument("--capital", type=float, default=500.0)
    a = ap.parse_args()
    if a.selftest:
        _selftest(); sys.exit(0)
    data_dir = os.path.abspath(a.data_dir)
    if not glob.glob(os.path.join(data_dir, "transitions-*.jsonl*")):
        print(f"no data at {data_dir} — run `pwsh deploy/pull-data.ps1` first, or `--selftest`."); sys.exit(0)
    recs, sessions = load(data_dir)
    print(f"loaded {len(recs)} transitions + {len(sessions)} restarts from {data_dir}\n")
    eps = build_episodes(recs, sessions)
    print(run_report(eps, a.capital))
