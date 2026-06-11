"""scripts/strategy_idea_tests.py — test the 3 "rust trading review" strategy ideas, read-only.

HYPOTHESIS-GENERATION on ~0.87 d of post-0013 data (effective-n ~= 1 event-day: 82% of candidate
arbs carry event-date 2026-06-10). NOT validation. Every result reports n AND effective-n (distinct
event-days, not episode count) and names its deflator. Watch for data-dredging (3 ideas on ~1 day).

REUSES the proven loaders/economics ([L20] — never re-implement):
  analyze_persistence.load / build_episodes / category / ECON_REMAP_DEPLOY_TS
  capital_sim.capturable / one_per_market / settle_t / _profit_per economics (via account_sim path)
  adverse_selection.open_classes / attribute / episodes_with_px  (H1 at-open direction + toxicity)
  shadow_fill.build_trajectories / shadow_fill / capturable        (H1 realized-edge & naked-leg)
  capital_velocity.LOCKUP_PASSIVE                                   (H2 lock-day priors — FROZEN)
  account_sim.run_account                                          (H2 bankroll walk)

Does NOT modify the frozen 0014 scripts (account_sim/alloc_policy/capital_sim/analyze_persistence are
imported, never edited). H2 is EXPLORATORY — labeled NOT the 0014 confirmatory result everywhere.

  python scripts/strategy_idea_tests.py --selftest
  python scripts/strategy_idea_tests.py [--data-dir PATH] [--json-out PATH]   (post-epoch by default)
"""
import os, sys, re, json, argparse
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from analyze_persistence import load, build_episodes, category, ECON_REMAP_DEPLOY_TS
from capital_sim import capturable, one_per_market, settle_t, void_haircut, DEPTH_BOUNDARY_NET
from adverse_selection import open_classes, attribute, episodes_with_px, _z_two_prop
from shadow_fill import build_trajectories, shadow_fill
from capital_velocity import LOCKUP_PASSIVE

_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})")
GATE_L = (0.05, 0.15, 1.0, 2.0)          # latencies of interest; 150ms is the prompt's gate point


def event_date(market):
    m = _DATE.search(market or "")
    return m.group(1) if m else "NODATE"


def effective_n(episodes):
    """effective-n = number of DISTINCT event-dates spanned (independent market-days), with the share
    on the dominant day. This is the honest unit for an edge-existence claim, not episode count."""
    days = {}
    for e in episodes:
        days[event_date(e["market"])] = days.get(event_date(e["market"]), 0) + 1
    if not days:
        return {"eff_n": 0, "days": {}, "dominant_share": None}
    tot = sum(days.values())
    dom = max(days.values())
    return {"eff_n": len(days), "days": dict(sorted(days.items())),
            "dominant_share": round(dom / tot, 3)}


def _median(xs):
    if not xs:
        return None
    s = sorted(xs); m = len(s) // 2
    return s[m] if len(s) % 2 else 0.5 * (s[m - 1] + s[m])


# =============================================================================================
# H1 — toxicity-direction / leg-sequencing gate
# =============================================================================================
def h1_direction_gate(recs, sessions, edge_min=0.01, latencies=GATE_L):
    """Split the CAPTURABLE cohort by at-OPEN direction (cheap_made vs dear_made) and measure, per
    class, overall + per category: (a) close-toxicity (dear_fell share), (b) episode duration,
    (c) shadow-fill realized edge + naked-leg (1-survival) at each latency. Then simulate a gate that
    SKIPS cheap_made (the prior-probe toxic class) and report the lift in the realized-edge distribution.

    REUSE: capturable (shared gate), open_classes (at-open classifier), attribute (close toxicity),
    build_trajectories+shadow_fill (realized edge / naked leg). Pairs the at-open class onto each
    capturable episode by (market, open_t)."""
    eps = build_episodes(recs, sessions)
    build_trajectories(recs, sessions, eps)
    cap = capturable(eps, edge_min, 0)                      # shared chokepoint ([L20]); window_min=0
    oc_rows, oc_skips = open_classes(recs, sessions)
    oc = {(r["market"], r["open_t"]): r for r in oc_rows}
    close_tag = {}
    for o, c in episodes_with_px(recs, sessions):
        tag, _, _ = attribute(o.get("dir"), o.get("px"), c.get("px"))
        if tag:
            close_tag[(o["market"], o["t"])] = tag

    def cls_of(e):
        r = oc.get((e["market"], e["open_t"]))
        return r["cls"] if r else "unclassified"

    def cohort_stats(cohort):
        """per at-open class stats on a (sub)cohort."""
        per = {}
        for e in cohort:
            per.setdefault(cls_of(e), []).append(e)
        out = {}
        for cls, es in per.items():
            durs = [e["duration"] for e in es]
            tags = [close_tag.get((e["market"], e["open_t"])) for e in es]
            ntag = sum(1 for t in tags if t)
            ntox = sum(1 for t in tags if t == "dear_fell")
            rows, _ = shadow_fill(es, edge_min, latencies=latencies)
            shadow = {str(r["L"]): {"survival": r["survival"], "naked": round(1 - r["survival"], 4),
                                    "realized_median": r["realized_median"],
                                    "realized_mean": r["realized_mean"],
                                    "n_survived": r["n_survived"]} for r in rows}
            bycat = {}
            for e in es:
                bycat[e["cat"]] = bycat.get(e["cat"], 0) + 1
            out[cls] = {"n": len(es), "by_cat": bycat,
                        "dur_median": _median(durs), "dur_mean": (sum(durs) / len(durs)) if durs else None,
                        "close_attributed": ntag, "n_toxic": ntox,
                        "close_toxic_share": (ntox / ntag) if ntag else None,
                        "eff_n": effective_n(es), "shadow": shadow}
        return out

    overall = cohort_stats(cap)
    by_cat = {cat: cohort_stats([e for e in cap if e["cat"] == cat])
              for cat in ("weather", "sports", "econ")}

    # toxicity z (cheap vs dear) overall + per category
    def tox_z(stats):
        cm, dm = stats.get("cheap_made"), stats.get("dear_made")
        if not (cm and dm and cm["close_attributed"] and dm["close_attributed"]):
            return None
        return _z_two_prop(cm["n_toxic"], cm["close_attributed"],
                           dm["n_toxic"], dm["close_attributed"])

    # --- the GATE: take-all vs skip-cheap_made. Report realized-edge distribution at each latency. ---
    take_all = cap
    gated = [e for e in cap if cls_of(e) != "cheap_made"]    # skip the (prior-)toxic class
    def realized_dist(cohort, L):
        rows, _ = shadow_fill(cohort, edge_min, latencies=(L,))
        r = rows[0]
        return {"L": L, "n_cohort": len(cohort), "survival": r["survival"],
                "naked": round(1 - r["survival"], 4), "n_survived": r["n_survived"],
                "realized_median": r["realized_median"], "realized_mean": r["realized_mean"],
                "realized_p25": r["realized_p25"]}
    gate_cmp = {str(L): {"take_all": realized_dist(take_all, L), "skip_cheap_made": realized_dist(gated, L)}
                for L in latencies}

    # per-category gate effect (does the lift survive beyond weather?)
    gate_by_cat = {}
    for cat in ("weather", "sports"):
        ta = [e for e in cap if e["cat"] == cat]
        gt = [e for e in ta if cls_of(e) != "cheap_made"]
        gate_by_cat[cat] = {str(L): {"take_all": realized_dist(ta, L), "skip_cheap_made": realized_dist(gt, L)}
                            for L in latencies}

    return {"edge_min": edge_min, "n_capturable": len(cap), "open_class_skips": oc_skips,
            "n_opens_classified": len(oc_rows), "eff_n_capturable": effective_n(cap),
            "overall": overall, "by_cat": by_cat,
            "toxicity_z": {"overall": tox_z(overall),
                           **{c: tox_z(by_cat[c]) for c in ("weather", "sports")}},
            "gate_take_all_vs_skip_cheap": gate_cmp, "gate_by_cat": gate_by_cat}


# =============================================================================================
# H2 — edge-RATE (velocity-adjusted) allocation — EXPLORATORY, OUTSIDE THE 0014 FREEZE.
# =============================================================================================
def _lock_days(market):
    """FROZEN per-category lock-day prior from capital_velocity.LOCKUP_PASSIVE (the pmus endDate horizon
    proxy: weather 1.2 d, sports 15 d, econ 21 d). Hardcoded estimates, not measurements (0014 audit C3d)."""
    return LOCKUP_PASSIVE.get(category(market), 15.0)


def _profit_per(e, haircut, void_mult):
    """Per-contract booked edge — IDENTICAL to account_sim/alloc_policy economics ([L20])."""
    avg_edge = max(DEPTH_BOUNDARY_NET, (e["open_net"] + DEPTH_BOUNDARY_NET) / 2.0)
    return max(0.0, avg_edge - haircut - void_haircut(e["market"], void_mult))


def h2_edge_rate_alloc(recs, sessions, capital0=500.0, max_clip=1000, edge_min=0.0,
                       offset_h=28.0, haircut=0.0, void_mult=1.0):
    """EXPLORATORY method-demo — NOT the 0014 confirmatory result (that needs >=14 event-days and the
    frozen fold protocol). Rank the capturable arbs two ways and walk a fixed $500 bankroll:
      LEVEL : booked_edge per contract, descending      (raw edge-level)
      RATE  : booked_edge / expected_lock_days, desc.   (velocity-adjusted)
    Compare $/day, capital deployed, capital locked, category mix funded. Uses the SAME per-contract
    economics + settle_t recycling as account_sim; only the consideration ORDER differs."""
    eps = build_episodes(recs, sessions)
    cap = one_per_market(capturable(eps, edge_min, 0))
    if not eps:
        return None
    window_end = max(e["close_t"] if e.get("close_t") else e["open_t"] for e in eps)
    t0 = min(e["open_t"] for e in eps); t1 = max(e["open_t"] for e in eps)
    span_d = max((t1 - t0) / 86400.0, 1e-9)

    def order(rule):
        if rule == "level":
            return sorted(cap, key=lambda e: (-_profit_per(e, haircut, void_mult), e["market"]))
        if rule == "rate":
            return sorted(cap, key=lambda e: (-_profit_per(e, haircut, void_mult) / _lock_days(e["market"]),
                                              e["market"]))
        if rule == "fifo":
            return sorted(cap, key=lambda e: e["open_t"])
        raise ValueError(rule)

    def walk(rule):
        cands = order(rule)
        cash = float(capital0); held = []; entered = []
        skip_cap = 0
        # NOTE: a pure edge-ranked walk ignores arrival causality (clairvoyant ordering) — same posture
        # as alloc_policy.offline_knapsack. We still process settlements at each pick's open_t so any
        # recycle the data permits is granted; in this window recycling is ~nil (reported via n_realized).
        for e in cands:
            now = e["open_t"]
            keep = []
            for p in held:
                if p["settle_t"] <= now:
                    cash += p["capital"] + p["profit"]      # recycle settled capital + booked edge
                else:
                    keep.append(p)
            held = keep
            cost_per = max(0.1, 1.0 - e["open_net"])
            want = min(e["open_c2"], max_clip)
            size = min(want, int(cash // cost_per))
            if size < 1:
                skip_cap += 1; continue
            per = _profit_per(e, haircut, void_mult)
            capital = size * cost_per; profit = size * per
            cash -= capital
            pos = {"settle_t": settle_t(e["market"], e["open_t"], offset_h), "capital": capital,
                   "profit": profit, "market": e["market"], "cat": e["cat"], "size": size,
                   "lock_d": _lock_days(e["market"]), "open_t": e["open_t"]}
            held.append(pos); entered.append(pos)
        # settle_t is deterministic per position, so the realized/unrealized split is just a filter on
        # `entered` against window_end (recycling above only affects intra-walk affordability, not PnL).
        realized_set = [p for p in entered if p["settle_t"] <= window_end]
        unreal_set = [p for p in entered if p["settle_t"] > window_end]
        rpnl = sum(p["profit"] for p in realized_set)
        upnl = sum(p["profit"] for p in unreal_set)
        locked = sum(p["capital"] for p in unreal_set)
        deployed = sum(p["capital"] for p in entered)
        # capital-weighted lock days (how slow is the locked book)
        lw = (sum(p["capital"] * p["lock_d"] for p in entered) / deployed) if deployed else None
        # $/lock-day = total booked PnL / capital-weighted lock-days (a velocity proxy)
        total = rpnl + upnl
        cat_funded = {}
        cat_cap = {}
        for p in entered:
            cat_funded[p["cat"]] = cat_funded.get(p["cat"], 0) + 1
            cat_cap[p["cat"]] = cat_cap.get(p["cat"], 0.0) + p["capital"]
        return {"rule": rule, "n_funded": len(entered), "skip_capital": skip_cap,
                "realized_pnl": rpnl, "unrealized_pnl": upnl, "total_pnl": total,
                "deployed_capital": deployed, "locked_capital": locked,
                "cap_weighted_lock_days": lw,
                "pnl_per_lockday": (total / lw) if lw else None,
                "cat_funded": cat_funded,
                "cat_capital": {k: round(v, 2) for k, v in cat_cap.items()},
                "n_realized_in_window": len(realized_set)}

    runs = {r: walk(r) for r in ("fifo", "level", "rate")}
    return {"capital0": capital0, "span_d": span_d, "n_candidates": len(cap),
            "eff_n_candidates": effective_n(cap),
            "lock_day_priors": dict(LOCKUP_PASSIVE), "runs": runs,
            "EXPLORATORY": "method-demo — NOT the 0014 confirmatory result (needs >=14 event-days)"}


# =============================================================================================
# H3 — time-of-day arrival window (13-20Z = ~9am-4pm ET)
# =============================================================================================
def h3_time_of_day(recs, sessions, edge_min=0.01, window=(13, 20), latencies=GATE_L):
    """Does restricting capturable arbs to the 13-20Z (~9am-4pm ET) arrival peak change
    capturable-edge-per-$, toxicity share, or fill-survival vs all-day? Or is the peak just where the
    VOLUME is (no quality difference)? Classify each capturable episode IN / OUT of the window by its
    OPEN-time UTC hour, then compare the two sub-cohorts on the same three metrics. REUSE: capturable,
    attribute (toxicity), shadow_fill (survival)."""
    eps = build_episodes(recs, sessions)
    build_trajectories(recs, sessions, eps)
    cap = capturable(eps, edge_min, 0)
    close_tag = {}
    for o, c in episodes_with_px(recs, sessions):
        tag, _, _ = attribute(o.get("dir"), o.get("px"), c.get("px"))
        if tag:
            close_tag[(o["market"], o["t"])] = tag
    lo, hi = window

    def in_win(e):
        h = int((e["open_t"] // 3600) % 24)
        return lo <= h < hi

    # arrival histogram (all capturable) — does the peak even reproduce?
    hist = [0] * 24
    for e in cap:
        hist[int((e["open_t"] // 3600) % 24)] += 1

    def cohort_metrics(cohort, label):
        if not cohort:
            return {"label": label, "n": 0}
        tags = [close_tag.get((e["market"], e["open_t"])) for e in cohort]
        ntag = sum(1 for t in tags if t)
        ntox = sum(1 for t in tags if t == "dear_fell")
        # edge-per-$ = sum(open_net * size) / sum(cost) ~ booked-edge-weighted; use book-avg per contract
        num = sum(_profit_per(e, 0.0, 1.0) * min(e["open_c2"], 10**9) for e in cohort)
        den = sum(max(0.1, 1.0 - e["open_net"]) * min(e["open_c2"], 10**9) for e in cohort)
        rows, _ = shadow_fill(cohort, edge_min, latencies=latencies)
        shadow = {str(r["L"]): {"survival": r["survival"], "naked": round(1 - r["survival"], 4),
                                "realized_median": r["realized_median"]} for r in rows}
        bycat = {}
        for e in cohort:
            bycat[e["cat"]] = bycat.get(e["cat"], 0) + 1
        return {"label": label, "n": len(cohort), "by_cat": bycat, "eff_n": effective_n(cohort),
                "open_net_median": _median([e["open_net"] for e in cohort]),
                "edge_per_dollar_capweighted": (num / den) if den else None,
                "close_attributed": ntag, "toxic_share": (ntox / ntag) if ntag else None,
                "shadow": shadow}

    return {"edge_min": edge_min, "window_Z": list(window), "n_capturable": len(cap),
            "arrival_hist_Z": hist,
            "in_window": cohort_metrics([e for e in cap if in_win(e)], f"{lo}-{hi}Z"),
            "out_window": cohort_metrics([e for e in cap if not in_win(e)], f"outside {lo}-{hi}Z")}


# =============================================================================================
def run_all(data_dir, post_epoch=True):
    recs, sessions = load(data_dir)
    note = None
    if post_epoch:
        n0 = len(recs)
        recs = [r for r in recs if r["t"] >= ECON_REMAP_DEPLOY_TS]
        note = f"POST-EPOCH: {len(recs)}/{n0} records at t>={ECON_REMAP_DEPLOY_TS}"
    span_d = (recs[-1]["t"] - recs[0]["t"]) / 86400.0 if recs else 0.0
    return {"data_dir": data_dir, "post_epoch": post_epoch, "note": note, "span_d": span_d,
            "n_records": len(recs),
            "H1_direction_gate": h1_direction_gate(recs, sessions),
            "H2_edge_rate_alloc": h2_edge_rate_alloc(recs, sessions),
            "H3_time_of_day": h3_time_of_day(recs, sessions)}, recs, sessions


def render(res):
    L = []
    P = L.append
    P("=" * 80)
    P("STRATEGY-IDEA TESTS  (hypothesis-generation on ~0.87d post-0013; effective-n ~= 1 event-day)")
    P("=" * 80)
    if res["note"]:
        P(res["note"])
    P(f"span {res['span_d']:.2f} d   records {res['n_records']}")

    h1 = res["H1_direction_gate"]
    P("\n" + "-" * 80)
    P("H1 — at-OPEN direction / leg-sequencing gate")
    P("-" * 80)
    en = h1["eff_n_capturable"]
    P(f"  capturable n={h1['n_capturable']}  effective-n={en['eff_n']} event-days "
      f"(dominant {100*en['dominant_share']:.0f}%)  days={en['days']}")
    P("  OVERALL by at-open class (close-toxic = dear_fell = you fill the wrong leg):")
    for cls in ("cheap_made", "dear_made", "unclassified"):
        s = h1["overall"].get(cls)
        if not s:
            continue
        tox = "n/a" if s["close_toxic_share"] is None else f"{100*s['close_toxic_share']:.0f}% (n={s['close_attributed']})"
        sh = s["shadow"]
        P(f"   [{cls:12}] n={s['n']:3d} {s['by_cat']}  dur_med {s['dur_median']:.2f}s  toxic {tox}")
        P(f"        naked@150ms {100*sh['0.15']['naked']:.0f}%  realized_med@150ms "
          f"{100*sh['0.15']['realized_median']:.2f}c   naked@1s {100*sh['1.0']['naked']:.0f}%")
    P(f"  toxicity z (cheap_made vs dear_made): overall {_fz(h1['toxicity_z']['overall'])}  "
      f"weather {_fz(h1['toxicity_z']['weather'])}  sports {_fz(h1['toxicity_z']['sports'])}")
    P("  PER-CATEGORY at-open toxicity (capturable cohort):")
    for cat in ("weather", "sports"):
        P(f"    [{cat}]")
        for cls in ("cheap_made", "dear_made"):
            s = h1["by_cat"][cat].get(cls)
            if not s:
                P(f"       {cls:12}: (none)")
                continue
            tox = "n/a" if s["close_toxic_share"] is None else f"{100*s['close_toxic_share']:.0f}% (n={s['close_attributed']})"
            P(f"       {cls:12}: n={s['n']:3d}  toxic {tox}  dur_med {s['dur_median']:.2f}s")
    P("  GATE (skip cheap_made) vs TAKE-ALL — realized-edge distribution:")
    for Lk in ("0.15", "1.0"):
        ta = h1["gate_take_all_vs_skip_cheap"][Lk]["take_all"]
        sk = h1["gate_take_all_vs_skip_cheap"][Lk]["skip_cheap_made"]
        P(f"    @L={Lk}s  take-all: n={ta['n_cohort']} naked {100*ta['naked']:.0f}% "
          f"realized_med {100*ta['realized_median']:.2f}c mean {100*ta['realized_mean']:.2f}c")
        P(f"             skip-cm:  n={sk['n_cohort']} naked {100*sk['naked']:.0f}% "
          f"realized_med {100*sk['realized_median']:.2f}c mean {100*sk['realized_mean']:.2f}c")
    P("  GATE per category @150ms (does lift survive beyond weather?):")
    for cat in ("weather", "sports"):
        ta = h1["gate_by_cat"][cat]["0.15"]["take_all"]
        sk = h1["gate_by_cat"][cat]["0.15"]["skip_cheap_made"]
        P(f"    [{cat}] take-all n={ta['n_cohort']} realized_med {100*ta['realized_median']:.2f}c "
          f"naked {100*ta['naked']:.0f}%  -> skip-cm n={sk['n_cohort']} "
          f"realized_med {100*sk['realized_median']:.2f}c naked {100*sk['naked']:.0f}%")

    h2 = res["H2_edge_rate_alloc"]
    P("\n" + "-" * 80)
    P("H2 — edge-RATE allocation  *** EXPLORATORY method-demo — NOT the 0014 confirmatory result ***")
    P("-" * 80)
    en2 = h2["eff_n_candidates"]
    P(f"  $ {h2['capital0']:.0f} bankroll  candidates(one/market) n={h2['n_candidates']}  "
      f"effective-n={en2['eff_n']} (dominant {100*en2['dominant_share']:.0f}%)")
    P(f"  FROZEN lock-day priors (capital_velocity, hardcoded est not measured): {h2['lock_day_priors']}")
    P(f"  {'rule':<6} {'n_fund':>6} {'total$':>9} {'deployed$':>10} {'locked$':>9} "
      f"{'capwt_lockd':>11} {'$/lockday':>10}  cat_funded")
    for rule in ("fifo", "level", "rate"):
        r = h2["runs"][rule]
        lw = f"{r['cap_weighted_lock_days']:.2f}" if r["cap_weighted_lock_days"] is not None else "n/a"
        pl = f"{r['pnl_per_lockday']:.4f}" if r["pnl_per_lockday"] is not None else "n/a"
        P(f"  {rule:<6} {r['n_funded']:>6} {r['total_pnl']:>9.4f} {r['deployed_capital']:>10.2f} "
          f"{r['locked_capital']:>9.2f} {lw:>11} {pl:>10}  {r['cat_funded']}  cap={r['cat_capital']}")
    lvl, rt = h2["runs"]["level"], h2["runs"]["rate"]
    if lvl["total_pnl"]:
        P(f"  -> RATE vs LEVEL: total PnL {100*(rt['total_pnl']-lvl['total_pnl'])/lvl['total_pnl']:+.1f}%, "
          f"cap-wt lock-days {lvl['cap_weighted_lock_days']:.2f} -> {rt['cap_weighted_lock_days']:.2f}d")

    h3 = res["H3_time_of_day"]
    P("\n" + "-" * 80)
    P("H3 — time-of-day arrival window (13-20Z = ~9am-4pm ET)")
    P("-" * 80)
    P(f"  capturable n={h3['n_capturable']}   arrival by Z-hour: "
      + " ".join(f"{h:02d}={c}" for h, c in enumerate(h3['arrival_hist_Z']) if c))
    for key in ("in_window", "out_window"):
        m = h3[key]
        if m["n"] == 0:
            P(f"  [{m['label']}] n=0")
            continue
        tox = "n/a" if m["toxic_share"] is None else f"{100*m['toxic_share']:.0f}% (n={m['close_attributed']})"
        ed = m["edge_per_dollar_capweighted"]
        P(f"  [{m['label']}] n={m['n']} {m['by_cat']} eff-n={m['eff_n']['eff_n']}  "
          f"open_net_med {100*m['open_net_median']:.2f}c  edge/$ {100*ed:.3f}c  toxic {tox}")
        P(f"        survival@150ms {100*m['shadow']['0.15']['survival']:.0f}%  "
          f"@1s {100*m['shadow']['1.0']['survival']:.0f}%  realized_med@150ms "
          f"{100*m['shadow']['0.15']['realized_median']:.2f}c")
    P("=" * 80)
    return "\n".join(L)


def _fz(z):
    return "n/a" if z is None else f"z={z:.2f}"


# =============================================================================================
def _selftest():
    print("strategy-idea-tests self-test")
    E = ECON_REMAP_DEPLOY_TS
    # effective_n + event_date
    assert event_date("tc-temp-x-2026-06-10-gte70") == "2026-06-10"
    assert event_date("nodate") == "NODATE"
    en = effective_n([{"market": "tc-a-2026-06-10-x"}, {"market": "tc-b-2026-06-10-x"},
                      {"market": "tc-c-2026-06-11-x"}])
    assert en["eff_n"] == 2 and en["days"] == {"2026-06-10": 2, "2026-06-11": 1}, en
    assert abs(en["dominant_share"] - 0.667) < 1e-3, en       # rounded to 3 dp

    # _lock_days pulls the FROZEN priors
    assert _lock_days("tc-x-2026-06-10") == LOCKUP_PASSIVE["weather"] == 1.2
    assert _lock_days("aec-mlb-x-2026-06-10") == LOCKUP_PASSIVE["sports"] == 15.0
    assert _lock_days("urc-x-2026-07-02") == LOCKUP_PASSIVE["econ"] == 21.0

    # H1 end-to-end on synthetic post-epoch recs: a weather market makes a cheap_made (toxic) open and a
    # sports market makes a dear_made (benign) open; both capturable. The gate must drop the weather one.
    recs = [
        # weather W: prior CLOSE, then OPEN where the CHEAP side fell (cheap_made), dies fast, dear_fell close
        {"t": E + 0,   "market": "tc-temp-w-2026-06-10-gte70", "transition": "CLOSE", "dir": "P",
         "net_edge": -0.01, "px": {"p_ya": 0.60, "k_yb": 0.60}},
        {"t": E + 10,  "market": "tc-temp-w-2026-06-10-gte70", "transition": "OPEN", "dir": "P",
         "net_edge": 0.03, "depth": {"c2": 200, "c1": 200, "c0": 200}, "px": {"p_ya": 0.55, "k_yb": 0.60}},
        {"t": E + 10.4, "market": "tc-temp-w-2026-06-10-gte70", "transition": "CLOSE", "dir": "P",
         "net_edge": -0.01, "px": {"p_ya": 0.55, "k_yb": 0.55}},   # dear fell -> TOXIC
        # sports S: prior CLOSE, OPEN where DEAR side moved (dear_made), long-lived, benign cheap_rose close
        {"t": E + 5,   "market": "aec-mlb-s-t-2026-06-10", "transition": "CLOSE", "dir": "P",
         "net_edge": -0.01, "px": {"p_ya": 0.50, "k_yb": 0.50}},
        {"t": E + 20,  "market": "aec-mlb-s-t-2026-06-10", "transition": "OPEN", "dir": "P",
         "net_edge": 0.03, "depth": {"c2": 300, "c1": 300, "c0": 300}, "px": {"p_ya": 0.50, "k_yb": 0.55}},
        {"t": E + 140, "market": "aec-mlb-s-t-2026-06-10", "transition": "CLOSE", "dir": "P",
         "net_edge": -0.01, "px": {"p_ya": 0.55, "k_yb": 0.55}},   # cheap rose -> benign
    ]
    recs.sort(key=lambda r: r["t"])
    h1 = h1_direction_gate(recs, [], edge_min=0.01, latencies=(0.15, 1.0))
    assert h1["n_capturable"] == 2, h1["n_capturable"]
    cm = h1["overall"]["cheap_made"]; dm = h1["overall"]["dear_made"]
    assert cm["n"] == 1 and cm["close_toxic_share"] == 1.0, cm
    assert dm["n"] == 1 and dm["close_toxic_share"] == 0.0, dm
    # the gate skips cheap_made -> only the sports (benign, long-lived) episode remains; @1s it survives
    g = h1["gate_take_all_vs_skip_cheap"]["1.0"]
    assert g["take_all"]["n_cohort"] == 2 and g["skip_cheap_made"]["n_cohort"] == 1, g
    assert g["skip_cheap_made"]["naked"] == 0.0, g            # the survivor lives past 1s
    assert g["take_all"]["naked"] == 0.5, g                   # the toxic weather one is naked at 1s

    # H2 end-to-end: weather (fast lock) vs sports (slow lock), equal edge. RATE must prefer weather;
    # capital-weighted lock-days under RATE must be <= under LEVEL (LEVEL ties broken by market name).
    h2 = h2_edge_rate_alloc(recs, [], capital0=500.0, max_clip=1000, edge_min=0.01)
    assert h2["runs"]["rate"]["cap_weighted_lock_days"] <= h2["runs"]["level"]["cap_weighted_lock_days"] + 1e-9, h2["runs"]
    assert "EXPLORATORY" in h2, "H2 must be labelled exploratory"

    # H3 end-to-end: both synthetic opens fall at the same Z-hour; put the window around it -> all in.
    h = int(((E + 10) // 3600) % 24)
    h3 = h3_time_of_day(recs, [], edge_min=0.01, window=(h, h + 1), latencies=(0.15, 1.0))
    assert h3["in_window"]["n"] == 2 and h3["out_window"].get("n", 0) == 0, (h3["in_window"], h3["out_window"])

    # render runs end-to-end
    txt = render({"note": "t", "span_d": 0.1, "n_records": len(recs),
                  "H1_direction_gate": h1, "H2_edge_rate_alloc": h2, "H3_time_of_day": h3})
    assert "H1" in txt and "EXPLORATORY" in txt and "H3" in txt
    print("  OK — effective_n, frozen lock-days, H1 gate (cheap_made dropped), H2 rate<=level lock-days, H3 window")
    print("self-test passed.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="test the 3 rust-review strategy ideas (read-only, hypothesis-gen)")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--data-dir", default=os.path.join(os.path.dirname(__file__), "..", "..", "data", "cross-arb"))
    ap.add_argument("--all-epoch", action="store_true", help="include pre-0013 records (default: post-epoch only)")
    ap.add_argument("--json-out", default=None)
    a = ap.parse_args()
    if a.selftest:
        _selftest(); sys.exit(0)
    import glob
    dd = os.path.abspath(a.data_dir)
    if not glob.glob(os.path.join(dd, "transitions-*.jsonl*")):
        print(f"no data at {dd} — run `pwsh deploy/pull-data.ps1` first, or `--selftest`."); sys.exit(0)
    res, _, _ = run_all(dd, post_epoch=not a.all_epoch)
    print(render(res))
    if a.json_out:
        with open(a.json_out, "w", encoding="utf-8") as f:
            json.dump(res, f, indent=1)
        print(f"\njson -> {a.json_out}")
