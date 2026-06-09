"""scripts/capital_velocity.py - capital-VELOCITY / early-exit lens.

Raw per-arb edge is only half the return; the other half is how fast the capital RECYCLES. The official pmus
finalization (endDate) is far in the future (sports/econ ~15d), but the OUTCOME is determined much earlier -- a
weather high is locked by late afternoon, a game at the final out -- so you can SELL the winning leg at ~$1 the
same day (the losing leg is ~$0 and just expires) and redeploy. So the binding lockup is time-to-OUTCOME-KNOWN +
an exit haircut, NOT the endDate -- EXCEPT for econ, whose "outcome known" moment IS the far-future release, so it
has no early-out.

This computes, per category: per-arb return on capital, capital turns/month under EARLY-EXIT vs PASSIVE-HOLD, and
the resulting monthly return on capital. Edge is taken from the live archive (weather/sports); econ has no data
yet (just mapped) so it uses --econ-edge. READ-ONLY. `--selftest` for the offline check.

  python scripts/capital_velocity.py [--exit-haircut 0.02] [--econ-edge 0.03]

CAVEATS baked into the output: lockup-days are ESTIMATES; the early-exit assumes post-resolution EXIT LIQUIDITY at
the converged price (unmeasured -- thin pmus books may force a bigger discount); and real turns/month are capped by
min(capital-recycle rate, OPPORTUNITY-arrival rate) -- for the fast categories opportunity (breadth) is the binding
constraint, not capital. Treat the monthly-RoC as an UPPER bound that isolates the velocity axis.
"""
import os, sys, argparse
sys.path.insert(0, os.path.dirname(__file__))
from analyze_persistence import load, build_episodes

# effective lockup in DAYS. early = time-to-outcome-known + same-day exit ; passive = hold to pmus endDate.
LOCKUP_EARLY   = {"weather": 0.3, "sports": 0.4, "econ": 21.0}   # econ has NO early-out (known only at release)
LOCKUP_PASSIVE = {"weather": 1.2, "sports": 15.0, "econ": 21.0}  # pmus endDate horizon (probed 2026-06-09)
DAYS_PER_MONTH = 30.0


def roc_row(cat, edge, exit_haircut):
    cap = max(0.05, 1.0 - edge)                              # capital tied per $1 payout ~ (1 - edge)
    early_edge = max(0.0, edge - exit_haircut)               # early exit crosses the exit spread on the winning leg
    le, lp = LOCKUP_EARLY[cat], LOCKUP_PASSIVE[cat]
    early_turns, pass_turns = DAYS_PER_MONTH / le, DAYS_PER_MONTH / lp
    return {
        "cat": cat, "edge_c": edge * 100, "cap": cap,
        "roc_per_turn_pct": edge / cap * 100,
        "early_lockup_d": le, "early_turns_mo": early_turns,
        "early_roc_mo_pct": (early_edge / cap) * early_turns * 100,
        "passive_lockup_d": lp, "passive_turns_mo": pass_turns,
        "passive_roc_mo_pct": (edge / cap) * pass_turns * 100,
    }


def _med(xs):
    return sorted(xs)[len(xs) // 2] if xs else None


def edges_by_cat(data_dir, edge_min=0.01):
    """Median capturable open-edge per category from the live archive (weather/sports). econ: no data yet."""
    recs, sess = load(data_dir)
    eps = [e for e in build_episodes(recs, sess) if e["censored"] in ("none", "eod")
           and e["open_net"] >= edge_min and e["open_c2"] >= 1]
    out = {}
    for cat in ("weather", "sports", "econ"):
        es = [e["open_net"] for e in eps if e["cat"] == cat]
        out[cat] = _med(es)
    return out


def report(edges, exit_haircut):
    L = ["=" * 92, "CAPITAL-VELOCITY / EARLY-EXIT LENS   (return = per-arb edge x how fast capital recycles)", "=" * 92,
         f"  exit haircut (early-exit only): {exit_haircut*100:.1f}c  |  lockup: early = outcome-known+exit, passive = pmus endDate",
         ""]
    L.append(f"  {'cat':8}{'edge':>7}{'RoC/turn':>10}  |  {'EARLY-EXIT lockup':>18}{'turns/mo':>10}{'RoC/mo':>9}  |  {'PASSIVE lockup':>16}{'turns/mo':>10}{'RoC/mo':>9}")
    for cat in ("weather", "sports", "econ"):
        e = edges.get(cat)
        if e is None:
            L.append(f"  {cat:8}{'(no data)':>7}  -- econ just mapped; pass --econ-edge to model it")
            continue
        r = roc_row(cat, e, exit_haircut)
        unprof = (e - exit_haircut) <= 0                     # early-exit costs more than the edge -> forced to hold
        early = f"{'UNPROFITABLE':>18}" if unprof else f"{r['early_turns_mo']:>10.0f}{r['early_roc_mo_pct']:>8.0f}%"
        L.append(f"  {cat:8}{r['edge_c']:>6.1f}c{r['roc_per_turn_pct']:>9.1f}%  |  "
                 f"{r['early_lockup_d']:>16.1f}d{early}  |  "
                 f"{r['passive_lockup_d']:>14.1f}d{r['passive_turns_mo']:>10.1f}{r['passive_roc_mo_pct']:>8.0f}%"
                 + ("   <- edge < exit cost: NO early-out, stuck with passive lockup" if unprof else ""))
    L += ["",
          "  READ: per-turn RoC is similar across categories (the EDGE is similar); the difference is VELOCITY.",
          "  Early-exit lets weather + sports recycle ~same-day (outcome known hours after entry); econ CANNOT",
          "  (its outcome is known only at the far-future release), so econ's velocity is fixed low regardless.",
          "  => at equal edge, weather/sports earn many-x more PER DOLLAR PER MONTH than econ -- capital velocity,",
          "     not edge, is econ's problem (consistent with the endDate probe: weather ~1.2d, sports/econ ~15d).",
          "",
          "  CAVEATS (do not read monthly-RoC as achievable): lockup-days are ESTIMATES; early-exit assumes",
          "  post-resolution EXIT LIQUIDITY at the converged price (UNMEASURED - thin pmus books may force a",
          "  bigger discount than the haircut); and real turns/mo are capped by OPPORTUNITY arrival (breadth),",
          "  not just capital - for the fast categories that cap, not lockup, is binding. This isolates VELOCITY.",
          "=" * 92]
    return "\n".join(L)


def _selftest():
    print("capital-velocity self-test")
    r = roc_row("weather", 0.04, 0.02)
    assert abs(r["early_turns_mo"] - 100.0) < 1e-9 and abs(r["passive_turns_mo"] - 25.0) < 1e-9   # 30/0.3, 30/1.2
    assert r["early_roc_mo_pct"] > r["passive_roc_mo_pct"]            # weather: fast recycle wins despite haircut
    s = roc_row("sports", 0.04, 0.02)
    assert s["passive_turns_mo"] == 2.0                              # 30/15
    assert s["early_turns_mo"] / s["passive_turns_mo"] == 75.0 / 2.0  # early-exit ~37x more turns than passive hold
    ec = roc_row("econ", 0.04, 0.02)
    assert ec["early_turns_mo"] == ec["passive_turns_mo"]            # econ: no early-out -> same low velocity
    # higher exit haircut lowers early monthly RoC
    assert roc_row("weather", 0.04, 0.03)["early_roc_mo_pct"] < r["early_roc_mo_pct"]
    print("OK - velocity math: early vs passive turns, econ no-early-out, exit-haircut effect")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="capital-velocity / early-exit lens")
    ap.add_argument("--data-dir", default=os.path.join(os.path.dirname(__file__), "..", "..", "data", "cross-arb"))
    ap.add_argument("--exit-haircut", type=float, default=0.02, help="early-exit spread cost on the winning leg (frac)")
    ap.add_argument("--econ-edge", type=float, default=None, help="model econ at this edge (no econ data yet)")
    ap.add_argument("--edge", type=float, default=None, help="override ALL categories' edge (scenario view, e.g. 0.04 tradeable)")
    ap.add_argument("--edge-min", type=float, default=0.01)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        _selftest(); sys.exit(0)
    import glob
    dd = os.path.abspath(a.data_dir)
    edges = edges_by_cat(dd, a.edge_min) if glob.glob(os.path.join(dd, "transitions-*.jsonl*")) else {"weather": None, "sports": None, "econ": None}
    if a.econ_edge is not None: edges["econ"] = a.econ_edge
    if a.edge is not None: edges = {k: a.edge for k in edges}   # scenario: same edge across categories
    print(f"median capturable edge by category (from {dd}): "
          f"{ {k: (round(v*100,2) if v else None) for k,v in edges.items()} } (c)\n")
    print(report(edges, a.exit_haircut))
