"""scripts/velocity_gate_experiment.py  -  backtest the TWO capital-velocity gates, decomposed.

THE QUESTION (owner, 2026-06-13): how does the backtest PnL change across three configs  - 
  (1) NEITHER velocity gate, (2) PROXIMITY gate only, (3) PROXIMITY + EDGE-RATE gate?

The two gates:
  * PROXIMITY  (bot `MAX_DAYS_TO_EVENT`, risk.rs gate 1b): skip an arb > N days before its settlement
    EVENT (game date / econ release). Capital only freezes on entry; monitoring is free.
  * EDGE-RATE  (bot `MIN_EDGE_RATE_CPD`, risk.rs gate 4a, decision 0017): skip an arb whose
    `booked_edge / lock_days` (c per $-day) is below a floor  -  reserve scarce capital for high-VELOCITY
    arbs. lock_days = the CORRECTED days-to-grade model (weather 1.2 / sports = days_to_event / econ 21).

WHY THIS IS A PREVIEW, NOT A VERDICT (read this):
  * The bot is WEATHER-ONLY today (sports/econ settlement-unverified until recon ~Jun 23 / Jul 2). Weather
    is same-day (proximity-exempt) + single lock-days, so BOTH velocity gates are INERT on the live universe.
    They only bite the SPORTS+ECON cohort -> this runs on the ALL-VERIFIED cohort = a POST-RECON PREVIEW.
  * Effective-n ~ 1 (~4 post-epoch event-days, ~91% on 2026-06-10). DESCRIPTIVE method-demo (0014/L19):
    magnitudes are NOISE; this may NOT be used to re-tune shipped defaults (that needs a fresh prereg).
  * The edge-rate threshold here is ILLUSTRATIVE / un-calibrated (swept). 0014-H2's frozen BACKTEST priors
    (sports 15) are NOT touched; this uses the LIVE bot's corrected lock-days (decision 0017).

THE MECHANISM IT SHOWS: `cap_constrained_entries` is genuinely capital-bound (the bot's $20 total cap)
with settlement recycling. A slow econ arb settles weeks out (settle_t = release + 28h) and locks the cap
for the ENTIRE data window  -  blocking faster arbs. Dropping it (proximity) or down-ranking it (edge-rate)
frees throughput. So the gates can only HELP via freed velocity, never by adding arbs.

REUSES the proven harness ([L20]  -  never re-implement loaders/economics/gates). Does NOT modify
backtest_current_strategy.py, account_sim.py, or any frozen 0014 script. READ-ONLY. `--selftest` is offline.

  python scripts/velocity_gate_experiment.py --selftest
  python scripts/velocity_gate_experiment.py [--data-dir PATH] [--capital 500]
"""
import os, sys, re, argparse, glob, calendar, datetime as dt
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from analyze_persistence import load, build_episodes
from backtest_current_strategy import (
    BotConfig, SETTLE_VERIFIED_ALL, run_funnel, cap_constrained_entries,
    open_direction_index, open_px_index, cohort_edge_stats, _fmt_catmix, _catmix, _eff_n_days,
)

# Corrected lock-day priors  -  MIRROR bot-rs/src/risk.rs::lock_days (decision 0017, days-to-grade). NOT the
# frozen 0014-H2 backtest priors (sports 15). Econ uses the bot's 21 fallback (real release ~3wk ~ same).
LOCK_WEATHER = 1.2
LOCK_SPORTS_FLOOR = 0.4
LOCK_ECON_FALLBACK = 21.0

MAX_CLIP = 1000              # apples-to-apples clip (prior-backtest setting)
PROX_DAYS = 2.0             # bot default MAX_DAYS_TO_EVENT
EDGE_RATE_SWEEP = [0.0, 0.5, 1.0, 2.0]   # illustrative c/$-day floors (0.0 = OFF)
SEP = "=" * 92


# ---- per-arb velocity quantities (mirror the bot) -------------------------------------------------
_DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def event_epoch(market):
    """Settlement-event midnight UTC from the slug's event-date, or None if the slug carries no date."""
    m = _DATE.search(market or "")
    if not m:
        return None
    y, mo, d = map(int, m.groups())
    return calendar.timegm((y, mo, d, 0, 0, 0, 0, 0, 0))


def days_to_event(e):
    """Days from the arb's OPEN to its settlement event (slug date). None if undated (-> proximity dormant,
    matching risk.rs `if let Some(d) = days_to_event`). Negative (event already 'today') is fine: <= any N."""
    ev = event_epoch(e["market"])
    if ev is None:
        return None
    return (ev - e["open_t"]) / 86400.0


def lock_days_live(e):
    """Entry->grade lock horizon, mirroring risk.rs::lock_days (0017). ALWAYS finite & > 0."""
    cat = e["cat"]
    if cat == "weather":
        return LOCK_WEATHER                                  # settles ~same evening; days_to_event ~0 understates
    if cat == "sports":
        d = days_to_event(e)
        return max(d, LOCK_SPORTS_FLOOR) if (d is not None and d == d) else LOCK_SPORTS_FLOOR  # dynamic, NaN->floor
    return LOCK_ECON_FALLBACK                                # econ/other: bot's 21 fallback (no release calendar)


def edge_rate_cpd(e):
    """booked_edge (c) / lock_days = c per dollar-day (the 0014-H2 velocity metric)."""
    return e["open_net"] * 100.0 / lock_days_live(e)


def passes_proximity(e, max_days):
    """risk.rs gate 1b: <=0 disables; None days_to_event stays dormant (kept); else reject if > max_days."""
    if max_days <= 0:
        return True
    d = days_to_event(e)
    if d is None:
        return True
    return d <= max_days


def passes_edge_rate(e, min_cpd):
    """risk.rs gate 4a: <=0 disables; else reject if edge_rate < floor."""
    if min_cpd <= 0:
        return True
    return edge_rate_cpd(e) >= min_cpd


# ---- one config = (proximity?, edge-rate?) filter -> capital-bound walk ---------------------------
def peak_concurrent(entered):
    evs = []
    for p in entered:
        evs.append((p["open_t"], 1)); evs.append((p["settle_t"], -1))
    evs.sort()                                               # ties: -1 (settle) before +1 (open) -> no overcount
    cur = peak = 0
    for _, delta in evs:
        cur += delta
        peak = max(peak, cur)
    return peak


def run_config(base, cfg, max_days, min_cpd, capital0, window_end):
    """Filter the all-verified base cohort by the two velocity gates, then the bot's capital-bound walk."""
    elig = [e for e in base if passes_proximity(e, max_days) and passes_edge_rate(e, min_cpd)]
    entered = cap_constrained_entries(elig, cfg, MAX_CLIP, 0.0, 1.0, capital0)
    realized = sum(p["profit"] for p in entered if p["settle_t"] <= window_end)
    unreal = sum(p["profit"] for p in entered if p["settle_t"] > window_end)
    locked = sum(p["capital"] for p in entered if p["settle_t"] > window_end)
    turns = sum(1 for p in entered if p["settle_t"] <= window_end)
    dep = sum(p["capital"] for p in entered) or 0.0
    cwld = (sum(p["capital"] * lock_days_live(p) for p in entered) / dep) if dep else None
    return {"eligible": len(elig), "entered": entered, "by_cat": _catmix(entered),
            "realized": realized, "unreal": unreal, "locked": locked, "turns": turns,
            "peak_conc": peak_concurrent(entered), "cap_wt_lock": cwld}


def _quantiles(xs):
    if not xs:
        return None
    s = sorted(xs); n = len(s)
    q = lambda f: s[min(n - 1, int(f * n))]
    return {"n": n, "min": s[0], "q25": q(0.25), "med": q(0.5), "q75": q(0.75), "max": s[-1]}


# ---- report --------------------------------------------------------------------------------------
def build_report(eps, recs, sessions, cfg, capital0, data_dir):
    L = []; P = L.append
    t0, t1 = recs[0]["t"], recs[-1]["t"]
    span_d = (t1 - t0) / 86400.0
    window_end = t1

    dir_idx, _ = open_direction_index(recs, sessions)
    px_idx = open_px_index(recs)
    # ALL-VERIFIED tradeable base cohort (post settlement+mid-div+2c-floor+H1, pre-cap)  -  the ONLY universe
    # where the velocity gates bite (weather-only today makes them inert). Same machinery as sec 5a of
    # backtest_current_strategy.py, so the cohort must match that script on the same data.
    stages, _ = run_funnel(eps, cfg, SETTLE_VERIFIED_ALL, dir_idx, px_idx, MAX_CLIP, 0.0, 1.0, capital0)
    base = stages["directional"]

    P(SEP)
    P("CROSS-ARB  -  VELOCITY-GATE EXPERIMENT (proximity x edge-rate, decomposed)")
    P(SEP)
    P(f"generated : {dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')}")
    P(f"data dir  : {data_dir}")
    P(f"span      : {span_d:.2f} d   transitions {len(recs)}   restarts/censors {len(sessions)}")
    P("")
    P("*** PREVIEW, NOT A VERDICT  -  read before any number below: ***")
    P("  * ALL-VERIFIED cohort = a POST-RECON PREVIEW. The live bot is WEATHER-ONLY (sports/econ unverified")
    P("    until recon ~Jun 23 / Jul 2); on that universe BOTH velocity gates are INERT. This shows what they")
    P("    WILL do once sports/econ come online (proximity on econ also assumes the econ-calendar follow-up).")
    P(f"  * EFFECTIVE-N ~ 1 ({span_d:.1f} d, ~91% of arbs on one event-date). DESCRIPTIVE method-demo (0014/L19):")
    P("    magnitudes are NOISE; do NOT quote them; do NOT re-tune shipped defaults (needs a fresh prereg).")
    P("  * Edge-rate threshold is ILLUSTRATIVE (swept). Lock-days = corrected days-to-grade (0017); the frozen")
    P("    0014-H2 BACKTEST priors (sports 15) are untouched. PAPER/GROSS (no latency/leg-fill/void netted).")

    # ---- A. base cohort ---------------------------------------------------------------------------
    P(""); P(SEP); P("A.  ALL-VERIFIED BASE COHORT (the universe the gates act on)"); P(SEP)
    be = cohort_edge_stats(base)
    P(f"  tradeable arbs (post settlement+mid-div+2c-floor+H1, one/market): {be['n']}")
    P(f"  by category : {_fmt_catmix(be['by_cat'])}")
    P(f"  effective event-days in cohort : {_eff_n_days(base)}")
    P(f"  (consistency: this must equal backtest_current_strategy.py sec 5a all-verified cohort on the same data.)")

    # ---- B. edge_rate distribution ----------------------------------------------------------------
    P(""); P(SEP); P("B.  EDGE-RATE DISTRIBUTION per category (c/$-day)  -  what the edge-rate gate ranks on"); P(SEP)
    P("  lock-days: weather 1.2 (const) | sports = days_to_event (floor 0.4) | econ 21 (bot fallback)")
    P(f"  {'cat':<10}{'n':>5}{'min':>8}{'q25':>8}{'med':>8}{'q75':>8}{'max':>8}   {'med lock-days':>14}")
    P(f"  {'-'*10}{'-'*5}{'-'*8}{'-'*8}{'-'*8}{'-'*8}{'-'*8}   {'-'*14}")
    for cat in ("weather", "sports", "econ"):
        sub = [e for e in base if e["cat"] == cat]
        q = _quantiles([edge_rate_cpd(e) for e in sub])
        if not q:
            P(f"  {cat:<10}{0:>5}{' - ':>8}{' - ':>8}{' - ':>8}{' - ':>8}{' - ':>8}")
            continue
        ld = _quantiles([lock_days_live(e) for e in sub])
        P(f"  {cat:<10}{q['n']:>5}{q['min']:>8.2f}{q['q25']:>8.2f}{q['med']:>8.2f}{q['q75']:>8.2f}{q['max']:>8.2f}"
          f"   {ld['med']:>14.1f}")
    P("  (post-2c-floor so every edge >=2c: weather cpd >= 2/1.2~1.7; near-game sports high; econ ~ 2/21~0.1.)")

    # ---- C. capital-binding diagnostic (config 1: no gates) ---------------------------------------
    cfg1 = run_config(base, cfg, 0.0, 0.0, capital0, window_end)
    P(""); P(SEP); P("C.  CAPITAL-BINDING DIAGNOSTIC (no gates)  -  the gates can only help if capital BINDS"); P(SEP)
    P(f"  eligible arbs           : {cfg1['eligible']}")
    P(f"  ENTERED (capital-bound) : {len(cfg1['entered'])}   ({_fmt_catmix(cfg1['by_cat'])})")
    P(f"  peak concurrent positions: {cfg1['peak_conc']}  (bot concurrency cap = {cfg.max_concurrent_positions})")
    P(f"  capital still LOCKED at window end: ${cfg1['locked']:.2f} of ${cfg.max_total_notional:.0f} total cap"
      f"  ({100.0*cfg1['locked']/cfg.max_total_notional:.0f}% of the cap frozen by un-settled arbs)")
    binds = len(cfg1['entered']) < cfg1['eligible'] or cfg1['locked'] > 0.5 * cfg.max_total_notional
    P(f"  => capital {'BINDS' if binds else 'does NOT bind'} on this cohort. "
      f"{'Velocity gates can free throughput.' if binds else 'Gates can only REMOVE arbs -> PnL can only drop.'}")

    # ---- D. headline 3-config table (edge-rate swept) ---------------------------------------------
    P(""); P(SEP); P("D.  THE THREE CONFIGS (proximity = 2 d; edge-rate swept)  -  the owner's ask"); P(SEP)
    P(f"  bankroll ${capital0:.0f}; bot caps $1/pair | $5/cluster | $20 total | 1 ctr/pair | 5 concurrent.")
    P("")
    hdr = f"  {'config':<34}{'entered':>9}{'turns':>7}{'realized$':>11}{'unreal$':>10}{'locked$':>9}{'cwLockD':>9}"
    P(hdr); P("  " + "-" * (len(hdr) - 2))

    def line(label, r):
        P(f"  {label:<34}{len(r['entered']):>9}{r['turns']:>7}{r['realized']:>11.2f}{r['unreal']:>10.2f}"
          f"{r['locked']:>9.2f}{(r['cap_wt_lock'] if r['cap_wt_lock'] is not None else float('nan')):>9.1f}")

    line("(1) NEITHER gate", cfg1)
    prox = run_config(base, cfg, PROX_DAYS, 0.0, capital0, window_end)
    line(f"(2) PROXIMITY only (<={PROX_DAYS:.0f}d)", prox)
    for cpd in EDGE_RATE_SWEEP:
        if cpd <= 0:
            continue
        both = run_config(base, cfg, PROX_DAYS, cpd, capital0, window_end)
        line(f"(3) BOTH  (prox + rate>={cpd:.1f})", both)
    P("")
    P("  by-category ENTERED mix per config:")
    P(f"    (1) neither        : {_fmt_catmix(cfg1['by_cat'])}")
    P(f"    (2) proximity only : {_fmt_catmix(prox['by_cat'])}")
    for cpd in EDGE_RATE_SWEEP:
        if cpd <= 0:
            continue
        both = run_config(base, cfg, PROX_DAYS, cpd, capital0, window_end)
        P(f"    (3) both rate>={cpd:.1f}    : {_fmt_catmix(both['by_cat'])}")

    # ---- E. full 2x2 (isolate each gate's marginal effect) ---------------------------------------
    rep_cpd = 1.0
    P(""); P(SEP); P(f"E.  FULL 2x2 LEVER DECOMPOSITION (edge-rate floor = {rep_cpd:.1f} c/$-day)"); P(SEP)
    P("  isolates each gate's marginal effect + the joint (the 0014 lever-decomposition shape).")
    P(hdr); P("  " + "-" * (len(hdr) - 2))
    line("neither", cfg1)
    line("proximity only", prox)
    line("edge-rate only", run_config(base, cfg, 0.0, rep_cpd, capital0, window_end))
    line("both", run_config(base, cfg, PROX_DAYS, rep_cpd, capital0, window_end))

    # ---- F. honest read ---------------------------------------------------------------------------
    P(""); P(SEP); P("F.  HONEST READ"); P(SEP)
    P("  * A PnL gain from a gate is ALWAYS freed-throughput, never added arbs (gates only remove). Read the")
    P("    `turns` + `locked$` columns, not just realized$  -  that is where the velocity mechanism shows.")
    P("  * On this data, proximity (<=2d) and the edge-rate floor are largely REDUNDANT on ECON (both drop the")
    P("    weeks-out econ arbs); they differ mainly on far-dated SPORTS (few) + (at high floors) thin weather.")
    P("  * EFFECTIVE-N ~ 1: the sign and size of any delta is noise. This is a MECHANISM PREVIEW, not a result.")
    P("  * NEXT: re-run on the multi-week post-recon data the 0014 protocol requires; do not re-tune defaults")
    P("    off this run (needs a fresh prereg). Live bot stays SAFE-BY-DEFAULT (both gates off until calibrated).")
    P(SEP)
    return "\n".join(L)


# ---- selftest (offline, synthetic) ---------------------------------------------------------------
def _selftest():
    print("velocity-gate-experiment self-test")

    def ep(mkt, cat, t, net, c2=200, dir_="P"):
        return {"market": mkt, "cat": cat, "open_t": t, "open_net": net, "open_c2": c2, "peak_c2": c2,
                "duration": 100, "censored": "none", "dir": dir_, "close_t": t + 100, "open_age": 0,
                "open_flat": False}

    # --- days_to_event + lock_days_live mirror risk.rs ---
    t = 1781000000.0
    wx = ep("tc-temp-nychigh-2026-06-10-gte90f", "weather", t, 0.03)
    # game 2 days out (event epoch = midnight of date; choose t so dte ~2.0)
    sp_far = ep("aec-mlb-lad-pit-2026-06-12", "sports", calendar.timegm((2026, 6, 10, 0, 0, 0, 0, 0, 0)), 0.03)
    sp_near = ep("aec-mlb-lad-pit-2026-06-10", "sports", calendar.timegm((2026, 6, 10, 12, 0, 0, 0, 0, 0)), 0.03)
    ec = ep("nfpc-uschange-gte-june-2026-07-02-atl100k", "econ",
            calendar.timegm((2026, 6, 10, 0, 0, 0, 0, 0, 0)), 0.04)
    assert abs(days_to_event(sp_far) - 2.0) < 1e-9, days_to_event(sp_far)
    assert -1.0 < days_to_event(sp_near) <= 0.0, days_to_event(sp_near)   # game 'today', opened midday
    assert days_to_event(ec) > 20.0
    assert days_to_event(ep("tc-nodate", "weather", t, 0.03)) is None      # undated -> None
    assert lock_days_live(wx) == LOCK_WEATHER
    assert lock_days_live(sp_far) == max(2.0, LOCK_SPORTS_FLOOR) == 2.0    # DYNAMIC (the correction)
    assert lock_days_live(sp_near) == LOCK_SPORTS_FLOOR                    # same-day game -> floor 0.4
    assert lock_days_live(ec) == LOCK_ECON_FALLBACK                        # econ -> 21 fallback
    # NaN-safe: a sports arb whose days_to_event is None -> floor (finite)
    assert lock_days_live(ep("aec-mlb-x-nodate", "sports", t, 0.03)) == LOCK_SPORTS_FLOOR
    print("  OK  -  days_to_event + lock_days_live mirror risk.rs (weather const, sports dynamic, econ fallback)")

    # --- edge_rate + gate predicates ---
    assert abs(edge_rate_cpd(wx) - 3.0 / 1.2) < 1e-9                       # 3c / 1.2d = 2.5
    assert abs(edge_rate_cpd(ec) - 4.0 / 21.0) < 1e-9                      # 4c / 21d ~ 0.19 (slow -> low)
    assert edge_rate_cpd(ec) < edge_rate_cpd(wx)                           # fat-but-slow econ < thin-but-fast wx
    assert passes_proximity(wx, 0.0) and passes_proximity(wx, 2.0)         # weather ~same-day, always in window
    assert passes_proximity(sp_far, 3.0) and not passes_proximity(sp_far, 1.0)   # 2d out: in at 3, out at 1
    assert not passes_proximity(ec, 2.0)                                   # econ weeks out -> cut at 2d
    assert passes_proximity(ec, 0.0)                                       # <=0 disables -> kept
    assert passes_edge_rate(wx, 1.0) and not passes_edge_rate(ec, 1.0)     # wx 2.5 passes, econ 0.19 fails
    assert passes_edge_rate(ec, 0.0)                                       # <=0 disables -> kept
    print("  OK  -  edge_rate + proximity/edge-rate predicates (incl. <=0 disables, None dormant)")

    # --- MECHANISM: one slow far-econ arb blocks the cap; gates free throughput -> more turns/PnL ---
    # tiny cap so the econ arb (settles weeks out) freezes capital; several fast same-day weather arbs queue.
    cfg = type("C", (BotConfig,), {"max_total_notional": 1.0, "max_notional_per_cluster": 1.0,
                                   "max_concurrent_positions": 1, "max_contracts_per_pair": 1})
    base = []
    base.append(ep("nfpc-uschange-gte-june-2026-07-02-atl100k", "econ",
                   calendar.timegm((2026, 6, 10, 0, 0, 0, 0, 0, 0)), 0.05, c2=10))   # SLOW: arrives first, locks cap
    for i in range(6):                                                               # FAST weather, same event-day
        base.append(ep(f"tc-temp-c{i}high-2026-06-10-gte70f", "weather",
                       calendar.timegm((2026, 6, 10, 1 + i, 0, 0, 0, 0, 0)), 0.05, c2=10))
    we = calendar.timegm((2026, 6, 13, 0, 0, 0, 0, 0, 0))                            # window end = a few days later
    none_cfg = run_config(base, cfg, 0.0, 0.0, 500.0, we)
    both_cfg = run_config(base, cfg, PROX_DAYS, 1.0, 500.0, we)                       # gates drop the slow econ
    # With the econ arb gone, the fast weather arbs recycle the cap -> strictly MORE settled turns.
    assert both_cfg["turns"] > none_cfg["turns"], (none_cfg["turns"], both_cfg["turns"])
    assert both_cfg["realized"] >= none_cfg["realized"]
    assert not any(p["cat"] == "econ" for p in both_cfg["entered"])                   # econ reserved out
    print(f"  OK  -  mechanism: gates free throughput ({none_cfg['turns']} -> {both_cfg['turns']} turns) "
          f"by dropping the cap-blocking slow econ arb")

    print("self-test passed.")


# ---- main ----------------------------------------------------------------------------------------
if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="backtest the proximity x edge-rate velocity gates (decomposed)")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--data-dir", default=os.path.join(os.path.dirname(__file__), "..", "..", "data", "cross-arb"))
    ap.add_argument("--capital", type=float, default=500.0, help="starting bankroll $ (default 500)")
    ap.add_argument("--out", default=None, help="write the report to this path (also prints)")
    a = ap.parse_args()
    if a.selftest:
        _selftest(); sys.exit(0)
    dd = os.path.abspath(a.data_dir)
    if not glob.glob(os.path.join(dd, "transitions-*.jsonl*")):
        print(f"no data at {dd}  -  run `pwsh deploy/pull-data.ps1` first, or `--selftest`."); sys.exit(0)
    recs, sessions = load(dd)
    eps = build_episodes(recs, sessions)
    report = build_report(eps, recs, sessions, BotConfig, a.capital, dd)
    print(report)
    if a.out:
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(report + "\n")
        print(f"\n[written to {a.out}]")
