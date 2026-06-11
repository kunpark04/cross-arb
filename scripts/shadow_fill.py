"""scripts/shadow_fill.py - SHADOW-FILL simulator: a read-only proxy for leg-fill risk WITHOUT orders.

THE QUESTION. The thesis is "buy YES on the cheap venue + NO on the dear venue, hold to settlement".
That only works if you actually GET BOTH LEGS at (near) the edge you saw. When an arb OPENs you decide
to enter, but the two legs only land after some entry LATENCY L (queue + round-trips + your own decision
loop). In that window the edge can shrink (NARROW), flip sign (FLIP), or vanish (the episode CLOSEs) - and
if it CLOSEs before both legs land you are left holding a NAKED directional leg (the dangerous outcome).

This simulator replays the live transition archive and, for a swept entry latency L, measures over every
CAPTURABLE OPEN->CLOSE episode (open_net >= 1c, measured depth c2 >= 1):

  (a) FILL-SURVIVAL rate  - fraction whose edge is still > 0 at open_t + L (episode hasn't CLOSEd yet AND
                            the interpolated/last-known net at open_t+L is still positive).
  (b) REALIZED edge at fill - the net_edge in force at open_t+L (the DEGRADED edge you actually lock).
                            Reported as median + mean over the survivors (a fill at <=0 locks nothing).
  (c) LEG-FILL-FAILURE proxy - fraction that CLOSE within L (edge gone before both legs landed -> naked leg).

Reuses analyze_persistence.load + build_episodes for episode boundaries, then reconstructs each episode's
net_edge TRAJECTORY from the raw transitions (OPEN at open_t, then WIDEN/NARROW/FLIP continuations, CLOSE
at close_t) to read the edge at open_t + L by left-continuous (step) interpolation: the edge in force at a
time is the value set by the most recent transition at or before it.

CAVEAT (state in any report): transitions only fire on moves bigger than the monitor's epsilon, so the
trajectory is COARSE and the survival/realized-edge numbers are OPTIMISTIC - real books tick more often and
move between logged transitions. This BOUNDS achievable edge FROM ABOVE; true fills are no better than this.

READ-ONLY. `--selftest` runs the offline synthetic verification; no args runs on the real archive.
`--post-epoch` restricts to records stamped by the 0013 monitor (detection-time CLOSEs) — the ONLY
records on which the sub-second grid points are real (pre-0013 stamps carry the ~1.25s flush lag the
shared loader corrects, so nothing below ~1s is resolvable there; see decision 0013 / [L22]).

  python scripts/shadow_fill.py --selftest
  python scripts/shadow_fill.py [--data-dir PATH] [--edge-min 0.01] [--post-epoch]
                                [--window-min S] [--liq-floor N] [--by-category] [--json-out PATH]
"""
import os, sys, argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from analyze_persistence import load, build_episodes, ECON_REMAP_DEPLOY_TS  # reuse - do not duplicate
from capital_sim import capturable as _shared_capturable   # the ONE quality gate ([L20])

# Latency sweep (seconds): 0 = idealized instant fill; 0.05-0.5 = the sub-second regime where the
# measured 86-261ms order RTT lives (real only on post-0013 detection-time stamps); up to 10s sluggish.
LATENCIES = (0, 0.05, 0.1, 0.15, 0.25, 0.5, 1, 2, 5, 10)
CONT = ("WIDEN", "NARROW", "FLIP")   # in-episode continuations that move net_edge


def post_epoch(records, epoch=ECON_REMAP_DEPLOY_TS):
    """Records stamped by the 0013 monitor only (t >= deploy epoch). The straddling deploy restart is a
    session_start, so any episode cut by this filter was restart-censored anyway."""
    return [r for r in records if r["t"] >= epoch]


def build_trajectories(records, sessions, episodes):
    """For each episode attach `traj` = sorted [(t, net_edge), ...] step-points: the OPEN at open_t plus every
    WIDEN/NARROW/FLIP for that market strictly after open_t and at/before close_t. Left-continuous: the edge
    'in force' at time x is the net of the latest point with t <= x. The terminal CLOSE is NOT a step-point
    (it marks the edge going <=0 AT close_t); survival past close_t is handled by the caller via close_t.

    We bucket raw records to episodes by (market, open_t <= t <= close_t). Episodes for the same market never
    overlap in time (build_episodes opens at most one per market at a time), so the bucketing is unambiguous.
    """
    # index episodes per market, time-ordered
    by_mkt = {}
    for e in episodes:
        by_mkt.setdefault(e["market"], []).append(e)
    for es in by_mkt.values():
        es.sort(key=lambda e: e["open_t"])
        for e in es:
            e["traj"] = [(e["open_t"], e["open_net"])]
            e["flip_ts"] = []                    # FLIP times: after a flip the ORIGINAL legs are the wrong
                                                 # direction — the post-flip net belongs to the OPPOSITE trade
    def _find(es, t):
        # the episode whose [open_t, close_t] contains t (half-open at the front for the OPEN already seeded)
        for e in es:
            if e["open_t"] <= t <= e["close_t"]:
                return e
        return None

    for r in records:
        if r["transition"] not in CONT:
            continue
        es = by_mkt.get(r["market"])
        if not es:
            continue
        e = _find(es, r["t"])
        if e is None or r["t"] <= e["open_t"]:   # strictly-after-open continuations only
            continue
        e["traj"].append((r["t"], r["net_edge"]))
        if r["transition"] == "FLIP":
            e["flip_ts"].append(r["t"])

    for es in by_mkt.values():
        for e in es:
            e["traj"].sort(key=lambda p: p[0])
    return episodes


def edge_at(ep, x):
    """Left-continuous net_edge in force at absolute time x for episode ep.
    x before open_t -> None (not entered yet). x at/after close_t -> the episode has CLOSEd (edge gone): 0.0
    is NOT returned here; the caller treats x >= close_t as 'closed' (leg-fail). For open_t <= x < close_t,
    return the net of the latest traj step with t <= x."""
    if x < ep["open_t"]:
        return None
    if x >= ep["close_t"]:
        return None  # CLOSEd by now -> caller flags leg-fail; no positive edge survives
    net = ep["traj"][0][1]
    for t, v in ep["traj"]:
        if t <= x:
            net = v
        else:
            break
    return net


def capturable(episodes, edge_min, window_min=0, liq_floor=1):
    """Quality gate via the SHARED chokepoint capital_sim.capturable ([L20] - never a private copy).
    Defaults (window_min=0, liq_floor=1) reproduce the historical shadow-fill cohort: measured episodes
    (restart-censored phantoms dropped, clean+eod kept), open_net >= edge_min, measured depth c2 >= 1.
    window_min/liq_floor are OPT-IN analysis lenses ([L15]) - callers must show the unfiltered baseline."""
    return _shared_capturable(episodes, edge_min, window_min, liq_floor=liq_floor)


def shadow_fill(episodes, edge_min, latencies=LATENCIES, window_min=0, liq_floor=1):
    """Return per-L rows: {L, n, survival, fill_fail, realized_median, realized_mean, realized_p25}.
       survival/fill_fail are fractions of the SAME capturable cohort; realized_* are over survivors only."""
    cohort = capturable(episodes, edge_min, window_min, liq_floor)
    rows = []
    n = len(cohort)
    for L in latencies:
        survived, realized, failed = 0, [], 0
        for e in cohort:
            x = e["open_t"] + L
            # leg-fail: the episode CLOSEd at or before our legs would land
            if x >= e["close_t"]:
                failed += 1
                continue
            # a FLIP at/before fill time is ALSO a leg-fail for the direction entered at OPEN: the
            # episode's post-flip net is the OPPOSITE-direction trade's edge, not ours — our legs are
            # now the wrong way around (counting it as survival overstated the hit-rate).
            if any(ft <= x for ft in e.get("flip_ts", ())):
                failed += 1
                continue
            net = edge_at(e, x)
            if net is not None and net > 0:
                survived += 1
                realized.append(net)
            # net <= 0 while still 'open' (a FLIP/NARROW to <=0 between logged moves can't happen since a
            # non-positive net is itself a CLOSE in the monitor; defensive branch only)
        rows.append({
            "L": L, "n": n,
            "survival": (survived / n) if n else 0.0,
            "fill_fail": (failed / n) if n else 0.0,
            "realized_median": _median(realized),
            "realized_mean": (sum(realized) / len(realized)) if realized else 0.0,
            "realized_p25": _pct(realized, 25),
            "n_survived": survived,
        })
    return rows, n


def _median(xs):
    if not xs:
        return 0.0
    s = sorted(xs); m = len(s) // 2
    return s[m] if len(s) % 2 else 0.5 * (s[m - 1] + s[m])


def _pct(xs, p):
    if not xs:
        return 0.0
    s = sorted(xs); k = (len(s) - 1) * p / 100.0
    lo = int(k); hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def _c(x):
    return 100.0 * x


DUR_CUTS = (0.25, 0.5, 1.0, 2.0, 5.0, 30.0)

def duration_dist(cohort, cuts=DUR_CUTS):
    """Share of the cohort whose episode duration is < each cut (seconds). eod-censored durations are
    LOWER BOUNDS, so the sub-cut shares are slightly conservative-high if eod episodes sit below a cut."""
    n = len(cohort)
    return {c: (sum(1 for e in cohort if e["duration"] < c) / n if n else 0.0) for c in cuts}


def render(rows, n_cohort, edge_min, span_d=None, label=""):
    out = []
    P = out.append
    P("=" * 74)
    P("SHADOW-FILL  (read-only leg-fill-risk proxy; OPTIMISTIC upper bound on achievable edge)" +
      (f"  [{label}]" if label else ""))
    P(f"  cohort: {n_cohort} capturable episodes (open_net >= {_c(edge_min):.1f}c, measured depth c2 >= 1)")
    if span_d is not None:
        P(f"  archive span: {span_d:.2f} d")
    if n_cohort == 0:
        P("  (no capturable episodes - nothing to fill)")
        P("=" * 74)
        return "\n".join(out)
    P("")
    P("   L(s) | survival% | realized edge @fill (c)         | leg-fail%")
    P("        |           |  median    mean     p25         |")
    P("  " + "-" * 68)
    for r in rows:
        P("  {L:>5} | {sv:>7.1f}%  |  {med:>6.2f}   {mean:>6.2f}   {p25:>6.2f}        | {ff:>6.1f}%".format(
            L=(f"{r['L']:g}"), sv=100 * r["survival"], med=_c(r["realized_median"]),
            mean=_c(r["realized_mean"]), p25=_c(r["realized_p25"]), ff=100 * r["fill_fail"]))
    P("")
    # headline: how fast the edge erodes as latency grows
    r0 = rows[0]
    r2 = next((r for r in rows if r["L"] == 2), rows[-1])
    rN = rows[-1]
    P("HEADLINE")
    P(f"  at L=0   : {100*r0['survival']:.0f}% survive, median realized {_c(r0['realized_median']):.2f}c")
    P(f"  at L=2s  : {100*r2['survival']:.0f}% survive, median realized {_c(r2['realized_median']):.2f}c, "
      f"{100*r2['fill_fail']:.0f}% leg-fail")
    P(f"  at L={rN['L']:g}s : {100*rN['survival']:.0f}% survive, median realized {_c(rN['realized_median']):.2f}c, "
      f"{100*rN['fill_fail']:.0f}% leg-fail")
    if r0["survival"] > 0:
        drop = 100 * (r0["survival"] - r2["survival"]) / r0["survival"]
        P(f"  => a 2s entry latency erodes survival by {drop:.0f}% vs instant.")
    P("")
    P("CAVEAT: transitions fire only on >epsilon moves -> coarse, OPTIMISTIC proxy (real books tick more")
    P("        often and move between logged transitions). Bounds achievable edge FROM ABOVE.")
    P("=" * 74)
    return "\n".join(out)


# ============================================================================================
# SELF-TEST  (synthetic episodes; no I/O)
# ============================================================================================
def _selftest():
    print("shadow-fill self-test")

    def tr(t, m, lab, net, d="PK", c2=None):
        r = {"t": t, "market": m, "transition": lab, "dir": d, "net_edge": net}
        if c2 is not None:
            r["depth"] = {"c2": c2, "c1": c2, "c0": c2}
        return r

    # E1: opens 3c at t0, CLOSEs at t=1.5 -> any L >= 2 must leg-fail / not survive.
    # E2: opens 4c at t10, NARROWs to 1c at t12, holds, CLOSEs at t20 -> long-lived; realized degrades with L.
    E1 = "aec-mlb-aa-bb-2026-06-10"
    E2 = "aec-mlb-cc-dd-2026-06-10"
    recs = [
        tr(0,  E1, "OPEN",  0.03, c2=100),
        tr(1,  E1, "WIDEN", 0.05, c2=100),
        tr(1,  E1, "CLOSE", -0.01),                      # E1 closes at t~1.5 -> use t=1; treat close_t=1
        tr(10, E2, "OPEN",  0.04, c2=200),
        tr(12, E2, "NARROW", 0.01, c2=200),
        tr(20, E2, "CLOSE", -0.02),
    ]
    # adjust E1 close to t=1.5 by re-stamping (build_episodes uses the CLOSE record time):
    recs[2] = tr(1.5, E1, "CLOSE", -0.01)
    recs.sort(key=lambda r: r["t"])
    eps = build_episodes(recs, sessions=[], close_lag=0)   # exact-timing asserts -> lag correction off
    build_trajectories(recs, [], eps)
    by = {e["market"]: e for e in eps}

    e1, e2 = by[E1], by[E2]
    assert e1["close_t"] == 1.5 and e1["open_net"] == 0.03, e1
    assert e2["close_t"] == 20 and e2["open_net"] == 0.04, e2
    # E1 trajectory: OPEN 3c @0, WIDEN 5c @1
    assert e1["traj"] == [(0, 0.03), (1, 0.05)], e1["traj"]
    # E2 trajectory: OPEN 4c @10, NARROW 1c @12
    assert e2["traj"] == [(10, 0.04), (12, 0.01)], e2["traj"]

    # edge_at checks
    assert abs(edge_at(e1, 0) - 0.03) < 1e-12          # at open
    assert abs(edge_at(e1, 0.5) - 0.03) < 1e-12        # before the widen
    assert abs(edge_at(e1, 1) - 0.05) < 1e-12          # at/after the widen step
    assert edge_at(e1, 1.5) is None                    # at close -> gone
    assert edge_at(e1, 2) is None                      # past close -> gone
    assert abs(edge_at(e2, 11) - 0.04) < 1e-12         # before narrow
    assert abs(edge_at(e2, 13) - 0.01) < 1e-12         # after narrow

    rows, n = shadow_fill(eps, edge_min=0.01)
    assert n == 2, n
    byL = {r["L"]: r for r in rows}

    # L=0: both survive (E1 @0 = 3c, E2 @10 = 4c). survival 100%, leg-fail 0.
    assert byL[0]["n_survived"] == 2 and byL[0]["fill_fail"] == 0.0, byL[0]
    # median of {0.03, 0.04} = 0.035
    assert abs(byL[0]["realized_median"] - 0.035) < 1e-12, byL[0]

    # L=1: E1 @1 = 5c (widened, still open), E2 @11 = 4c. both survive.
    assert byL[1]["n_survived"] == 2 and byL[1]["fill_fail"] == 0.0, byL[1]

    # L=2: E1 @2 -> past its t=1.5 close -> leg-fail. E2 @12 -> NARROW step lands exactly at 12 = 1c, still open.
    assert byL[2]["fill_fail"] == 0.5, byL[2]                       # E1 failed
    assert byL[2]["n_survived"] == 1, byL[2]                        # only E2
    assert abs(byL[2]["realized_median"] - 0.01) < 1e-12, byL[2]    # E2 realized = 1c (narrowed)
    print("  OK - L>=2 drops E1 to leg-fail (survival 0 for it); E2 realized degrades 4c->1c with latency")

    # the prompt's specific assertion: an edge that closes at t=1.5 has survival 0 for L>=2 (for that episode)
    for L in (2, 5, 10):
        # E1 alone must never survive at L>=2
        single = shadow_fill([e1], edge_min=0.01)[0]
        rr = {r["L"]: r for r in single}
        assert rr[L]["n_survived"] == 0 and rr[L]["fill_fail"] == 1.0, (L, rr[L])
    print("  OK - isolated t=1.5-close episode: survival 0, leg-fail 100% for all L in {2,5,10}")

    # FLIP = leg-fail for the entered direction: E3 opens dir PK, FLIPs to KP at t=102 (still positive
    # net), closes at t=110. At L<2 you fill pre-flip (survive); at L>=2 your PK legs are the WRONG way
    # around — the +3c trajectory belongs to the KP trade — so it must count as leg-fail, not survival.
    E3 = "aec-mlb-ee-ff-2026-06-10"
    frecs = [tr(100, E3, "OPEN", 0.04, c2=100), {**tr(102, E3, "FLIP", 0.03, c2=100), "dir": "KP"},
             tr(110, E3, "CLOSE", -0.01)]
    feps = build_episodes(frecs, sessions=[], close_lag=0)
    build_trajectories(frecs, [], feps)
    frr = {r["L"]: r for r in shadow_fill(feps, edge_min=0.01)[0]}
    assert frr[1]["n_survived"] == 1 and frr[1]["fill_fail"] == 0.0, frr[1]    # pre-flip fill: survives
    assert frr[2]["n_survived"] == 0 and frr[2]["fill_fail"] == 1.0, frr[2]    # at the flip: leg-fail
    assert frr[5]["n_survived"] == 0 and frr[5]["fill_fail"] == 1.0, frr[5]    # post-flip: leg-fail
    print("  OK - FLIP before fill counts as leg-fail (post-flip net is the opposite trade's edge)")

    txt = render(rows, n, 0.01)
    assert "SHADOW-FILL" in txt and "leg-fail" in txt
    print("  OK - render produces the table")

    # sub-second grid points exist and are exercised: E1 (closes t=1.5) survives at L=0.5 but its
    # realized edge at 0.5 is still the open 3c; at L=2 it leg-fails (asserted above).
    assert all(L in byL for L in (0.05, 0.1, 0.15, 0.25, 0.5)), sorted(byL)
    assert byL[0.5]["n_survived"] == 2 and byL[0.5]["fill_fail"] == 0.0, byL[0.5]

    # post-epoch filter: only records at/after the epoch survive
    pe = post_epoch([{"t": 5, "x": 1}, {"t": 15, "x": 2}], epoch=10)
    assert pe == [{"t": 15, "x": 2}], pe

    # duration distribution: E1 dur=1.5, E2 dur=10 -> <2s share 0.5, <30s share 1.0, <1s share 0
    dd = duration_dist(capturable(eps, 0.01))
    assert abs(dd[2.0] - 0.5) < 1e-12 and abs(dd[30.0] - 1.0) < 1e-12 and dd[1.0] == 0.0, dd

    # opt-in lenses flow through the SHARED capital_sim gate: dur>=5s keeps only E2; c2>=150 keeps only E2
    assert [e["market"] for e in capturable(eps, 0.01, window_min=5)] == [E2]
    assert [e["market"] for e in capturable(eps, 0.01, liq_floor=150)] == [E2]
    rows_w, n_w = shadow_fill(eps, 0.01, window_min=5)
    assert n_w == 1 and {r["L"]: r for r in rows_w}[2]["fill_fail"] == 0.0   # E2 alone, alive at 2s
    print("  OK - sub-second grid + post-epoch filter + duration dist + shared-gate lenses")
    print("self-test passed.")


def _dump_subset(eps, edge_min, window_min=0, liq_floor=1, label=""):
    """Run the grid on one (optionally lensed) cohort; return (printable, json-safe dict)."""
    rows, n = shadow_fill(eps, edge_min, window_min=window_min, liq_floor=liq_floor)
    cohort = capturable(eps, edge_min, window_min, liq_floor)
    dd = duration_dist(cohort)
    txt = [render(rows, n, edge_min, label=label), "  duration < cut (share of cohort): " +
           "  ".join(f"<{c:g}s {100*v:.1f}%" for c, v in sorted(dd.items()))]
    return "\n".join(txt), {"label": label, "filter": {"edge_min": edge_min, "window_min": window_min,
                            "liq_floor": liq_floor}, "n": n, "rows": rows,
                            "duration_dist": {str(k): v for k, v in dd.items()}}


# ============================================================================================
if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="cross-arb shadow-fill leg-fill-risk proxy")
    ap.add_argument("--selftest", action="store_true", help="offline synthetic verification")
    ap.add_argument("--data-dir", default=os.path.join(os.path.dirname(__file__), "..", "..", "data", "cross-arb"))
    ap.add_argument("--edge-min", type=float, default=0.01, help="min open_net fraction for capturable (default 0.01 = 1c)")
    ap.add_argument("--post-epoch", action="store_true",
                    help="post-0013 records only (detection-time stamps; sub-second grid is real)")
    ap.add_argument("--window-min", type=float, default=0,
                    help="OPT-IN lens ([L15]): also show the duration >= S subset next to the baseline")
    ap.add_argument("--liq-floor", type=int, default=1,
                    help="OPT-IN lens ([L15]): also show the open_c2 >= N subset next to the baseline")
    ap.add_argument("--by-category", action="store_true", help="also break out weather/sports/econ")
    ap.add_argument("--json-out", default=None, help="dump all subset grids to this JSON path")
    args = ap.parse_args()

    if args.selftest:
        _selftest(); sys.exit(0)

    import glob, json
    data_dir = os.path.abspath(args.data_dir)
    if not os.path.isdir(data_dir) or not glob.glob(os.path.join(data_dir, "transitions-*.jsonl*")):
        print(f"no data at {data_dir} - run `pwsh deploy/pull-data.ps1` first, or `--selftest`.")
        sys.exit(0)
    recs, sessions = load(data_dir)
    if args.post_epoch:
        n0 = len(recs)
        recs = post_epoch(recs)
        print(f"POST-EPOCH: {len(recs)}/{n0} records at t >= {ECON_REMAP_DEPLOY_TS} (0013 detection-time stamps)")
    eps = build_episodes(recs, sessions)
    build_trajectories(recs, sessions, eps)
    span_d = (recs[-1]["t"] - recs[0]["t"]) / 86400.0 if recs else 0.0
    print(f"loaded {len(recs)} transitions + {len(sessions)} restarts from {data_dir}\n")

    subsets = []                                       # (eps_subset, window_min, liq_floor, label)
    subsets.append((eps, 0, 1, "ALL capturable (unfiltered baseline)"))
    if args.window_min or args.liq_floor > 1:          # the opt-in decision lens, baseline always shown
        subsets.append((eps, args.window_min, args.liq_floor,
                        f"LENS dur>={args.window_min:g}s c2>={args.liq_floor}"))
        if args.window_min and args.liq_floor > 1:     # isolate each lever ([L19])
            subsets.append((eps, 0, args.liq_floor, f"LENS c2>={args.liq_floor} only (ex-ante observable)"))
    if args.by_category:
        for cat in ("weather", "sports", "econ"):
            subsets.append(([e for e in eps if e["cat"] == cat], 0, 1, f"category={cat}"))

    js = {"data_dir": data_dir, "post_epoch": bool(args.post_epoch), "epoch": ECON_REMAP_DEPLOY_TS,
          "span_d": span_d, "n_records": len(recs), "subsets": []}
    for sub_eps, wm, lf, label in subsets:
        txt, j = _dump_subset(sub_eps, args.edge_min, wm, lf, label)
        print(txt + ("\n  archive span: %.2f d\n" % span_d))
        js["subsets"].append(j)
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(js, f, indent=1)
        print(f"json -> {args.json_out}")
