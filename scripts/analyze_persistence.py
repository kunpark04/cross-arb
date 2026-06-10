"""scripts/analyze_persistence.py -persistence-analysis harness for the cross-arb monitor's data.

Reconstructs cross-venue edge EPISODES (one OPEN -> CLOSE per market) from the event-date-partitioned
transition log and computes the metrics that drive the go/no-go gate (tasks/todo.md):

  • EDGE MAGNITUDE  -net_edge at OPEN (already fee-net; the monitor logs signal()/game_edge net of fees).
  • PERSISTENCE     -how long an edge stays open = the window you have to fill BOTH legs before it fades.
                      (Thesis is lock-both-legs-then-hold-to-settlement, so what you capture is ~the edge
                       at fill; persistence tells you whether a fill was even achievable.)
  • CAPTURABLE RATE -episodes/day that are BOTH large (open_net >= edge_min) AND persistent
                      (duration >= window_min). The headline "is there a real, scalable edge" number.
  • SCALABILITY     -sum of capturable open-edges per day (c per $1 of payout) ~ $/day at unit size.

Reads the pulled archive Kalshi/data/cross-arb/ : transitions-<date>.jsonl[.gz] + sessions.jsonl.
Restarts (session_start) force-close open episodes as restart-censored so a lost-state gap can't fake a
duration. READ-ONLY. Run `--selftest` for the synthetic verification; no args analyzes the real archive.

  python scripts/analyze_persistence.py --selftest
  python scripts/analyze_persistence.py [--data-dir PATH] [--edge-min 0.01] [--window-min 30]
"""
import os, sys, re, gzip, json, glob, argparse

WEATHER_PFX, SPORTS_PFX = "tc-", "aec-"
ECON_PFX = ("cpic-", "gdpc-", "nfpc-", "urc-", "rdc-")   # CPI / GDP / NFP / U-3 / Fed (bot/colisted_map.ECON)
ECON_THRESH_PFX = ("cpic-", "gdpc-", "nfpc-", "urc-")    # the >=T families (rdc-/Fed is categorical: clean)
def category(slug):
    if slug.startswith(WEATHER_PFX): return "weather"
    if slug.startswith(SPORTS_PFX):  return "sports"
    if slug.startswith(ECON_PFX):    return "econ"
    return "other"

# --- data-quality epochs (decision 0013). Each is the droplet-deploy epoch of a monitor fix —
#     the 0013 build (bfa9e2fccf15) went live 2026-06-10 09:03 UTC; its session_start t is the epoch.
#     Records BEFORE it came from the pre-fix monitor and get the corrections below; records after are clean.
# Records from the pre-0013 monitor paired pmus ">=T" econ markets with Kalshi "Above T" (STRICT >) —
# OFF BY ONE print-grid bucket. Their "edge" is the market-priced P(print==T), a settlement-identity
# phantom (observed: a persistent 12-13c U-3 "arb"), NOT a cross-venue mispricing -> QUARANTINED.
ECON_REMAP_DEPLOY_TS = 1781082189
# The pre-0013 FlipDebouncer stamped every flushed CLOSE at FLUSH time (~+1.0..1.5s after the edge
# died). Clean-CLOSE timestamps in that era are shifted back by the midpoint so durations aren't
# systematically inflated (sub-second leg-fill analysis is the #1 execution-risk metric).
DEBOUNCE_STAMP_FIXED_TS = 1781082189
CLOSE_FLUSH_LAG = 1.25                                   # midpoint of the +[1.0, 1.5]s flush-stamp delay


# ============================================================================================
# LOAD
# ============================================================================================
def _open_any(path):
    return gzip.open(path, "rt", encoding="utf-8") if path.endswith(".gz") else open(path, encoding="utf-8")

def load(data_dir):
    """Return (transitions sorted by t, sorted censoring timestamps). AT-MOST-ONE file per event-date: a
    raw transitions-<date>.jsonl wins over a same-date .gz, so a finalized day later recreated on the droplet
    and re-pulled next to its old .gz is NOT double-read (review data-pipeline WARN).
    QUARANTINE (0013): threshold-econ records logged by the pre-remap monitor are DROPPED — their "edge"
    is the off-by-one-bucket boundary mass P(print==T), not an arb; keeping them poisons every metric
    (they were ~20% of the allocation-test headline). Fed (rdc-) categorical records are clean and kept.
    Censoring events: session_start + kalshi_resync + ws_reconnect ALL force-close open episodes (a
    reconnect rebuilds books, so outage-gap changes surface at reconnect time — the phantom class)."""
    trans, sessions, quarantined = [], [], 0
    chosen = {}                                          # event-date token -> path (raw overrides .gz)
    for path in glob.glob(os.path.join(data_dir, "transitions-*.jsonl.gz")) + \
                glob.glob(os.path.join(data_dir, "transitions-*.jsonl")):   # raw second -> wins the key
        m = re.search(r"transitions-(.+?)\.jsonl(?:\.gz)?$", os.path.basename(path))
        chosen[m.group(1) if m else path] = path
    for path in sorted(chosen.values()):
        with _open_any(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                r = json.loads(line)
                if str(r.get("market", "")).startswith(ECON_THRESH_PFX) and \
                        (ECON_REMAP_DEPLOY_TS is None or r["t"] < ECON_REMAP_DEPLOY_TS):
                    quarantined += 1
                    continue
                trans.append(r)
    if quarantined:
        print(f"NOTE: quarantined {quarantined} pre-remap threshold-econ records "
              f"(off-by-one >=T vs >T pairing — phantom edges; decision 0013)")
    spath = os.path.join(data_dir, "sessions.jsonl")
    if os.path.exists(spath):
        with open(spath, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    r = json.loads(line)
                    if r.get("event") in ("session_start", "kalshi_resync", "ws_reconnect"):  # all force-close
                        sessions.append(int(r["t"]))
    trans.sort(key=lambda r: r["t"])
    return trans, sorted(sessions)


# ============================================================================================
# EPISODE RECONSTRUCTION  (pure; offline self-tested)
# ============================================================================================
def build_episodes(records, sessions, close_lag=None):
    """Reconstruct per-market edge episodes. An episode spans OPEN..CLOSE; WIDEN/NARROW/FLIP are
    continuations (the arb persisted). A session_start/resync/reconnect force-closes every open episode
    (state was lost -> restart-censored). Episodes still open at end-of-data are eod-censored. Returns a
    list of dicts: {market, cat, dir, open_t, close_t, duration, censored, open_net, peak_net, twa_net,
    open_c2, peak_c2, open_age, open_age_p, open_age_k, open_flat, n_widen, n_narrow, n_flip}.

    close_lag: correction subtracted from CLOSE-record timestamps logged by the pre-0013 monitor (whose
    debouncer stamped the FLUSH time, ~+1.0-1.5s after the edge died). None = auto: apply
    CLOSE_FLUSH_LAG to records before DEBOUNCE_STAMP_FIXED_TS (all records while that is None).
    Pass 0 to disable (tests asserting exact timing math)."""
    # merge transitions + sessions into one timeline; on a tie, process the session FIRST (order=0) so it
    # closes stale episodes before that restart's fresh OPENs land.
    events = [(t, 0, None) for t in sessions] + [(r["t"], 1, r) for r in records]
    events.sort(key=lambda e: (e[0], e[1]))

    open_ep, done = {}, []

    def _lag(t):                                                   # pre-fix CLOSE stamps carry the flush delay
        if close_lag == 0: return 0.0
        if close_lag is not None: return close_lag
        return CLOSE_FLUSH_LAG if (DEBOUNCE_STAMP_FIXED_TS is None or t < DEBOUNCE_STAMP_FIXED_TS) else 0.0

    def _close(market, tc, why, lag_adj=False):
        ep = open_ep.pop(market)
        ep["_acc"] += ep["_last_net"] * (tc - ep["_seg_t"])        # final open segment
        dur = tc - ep["open_t"]
        ep["close_t"], ep["duration"], ep["censored"] = tc, dur, why
        ep["lag_adj"] = lag_adj
        ep["twa_net"] = ep["_acc"] / dur if dur > 0 else ep["open_net"]
        for k in ("_acc", "_seg_t", "_last_net"):                  # drop scratch
            ep.pop(k, None)
        done.append(ep)

    for t, order, rec in events:
        if order == 0:                                             # session_start/resync/reconnect: state lost
            for m in list(open_ep):
                _close(m, t, "restart")
            continue
        m, lab, net = rec["market"], rec["transition"], rec["net_edge"]
        if lab == "OPEN":
            if m in open_ep:                                       # OPEN while open = restart re-detect / dup -> continuation
                continue
            d = rec.get("depth") or {}
            dc2 = d.get("c2", 0)                               # fillable contracts at gross >= 2c (net-ish)
            ag = rec.get("age") or {}
            open_ep[m] = {"market": m, "cat": category(m), "dir": rec["dir"], "open_t": t,
                          "open_net": net, "peak_net": net, "open_c2": dc2, "peak_c2": dc2,
                          # staleness at open: keep BOTH venues (max alone discarded the k=0
                          # fresh-subscribe tell — the book-init-phantom fingerprint, [L20]/0012)
                          "open_age": max(ag.get("p", 0), ag.get("k", 0)) if ag else None,
                          "open_age_p": ag.get("p") if ag else None,
                          "open_age_k": ag.get("k") if ag else None,
                          # flat ladder (c2==c1==c0 with depth) = one resting level, the other phantom tell
                          "open_flat": bool(d) and d.get("c2", 0) == d.get("c1", -1) == d.get("c0", -2) and dc2 > 0,
                          "n_widen": 0, "n_narrow": 0, "n_flip": 0,
                          "_acc": 0.0, "_seg_t": t, "_last_net": net}
        elif m in open_ep:
            if lab == "CLOSE":
                lag = _lag(t)
                ep = open_ep[m]                                    # clamp: never before the last continuation
                tc = max(t - lag, ep["_seg_t"], ep["open_t"]) if lag else t
                _close(m, tc, "none", lag_adj=bool(lag))
            else:                                                  # WIDEN / NARROW / FLIP
                ep = open_ep[m]
                ep["_acc"] += ep["_last_net"] * (t - ep["_seg_t"]); ep["_seg_t"] = t
                ep["_last_net"] = net; ep["peak_net"] = max(ep["peak_net"], net)
                ep["peak_c2"] = max(ep["peak_c2"], (rec.get("depth") or {}).get("c2", 0))
                if lab == "WIDEN": ep["n_widen"] += 1
                elif lab == "NARROW": ep["n_narrow"] += 1
                elif lab == "FLIP": ep["n_flip"] += 1; ep["dir"] = rec["dir"]
        # else: CLOSE/WIDEN/NARROW/FLIP with no open episode (first obs mid-edge, or post-restart) -> ignore

    if records:
        last_t = records[-1]["t"]
        for m in list(open_ep):
            _close(m, last_t, "eod")
    return done


# ============================================================================================
# SUMMARY
# ============================================================================================
def _pct(xs, p):
    if not xs: return 0
    s = sorted(xs); k = (len(s) - 1) * p / 100.0
    lo = int(k); hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)

def _c(x):  # net_edge fraction -> cents per $1
    return 100.0 * x

def summarize(records, sessions, episodes, edge_min, window_min):
    out = []
    P = out.append
    if not records:
        return "no transitions in the archive yet -nothing to analyze."
    t0, t1 = records[0]["t"], records[-1]["t"]
    span_h = (t1 - t0) / 3600.0
    span_d = (t1 - t0) / 86400.0
    per_day = (lambda n: n / span_d if span_d > 0 else 0.0)
    mkts = {r["market"] for r in records}

    P("=" * 78)
    P("COVERAGE")
    P(f"  span            : {span_h:.1f} h ({span_d:.2f} d)   {t0} .. {t1}")
    P(f"  transitions     : {len(records)}   markets seen: {len(mkts)}   restarts: {len(sessions)}")
    if span_d < 1: P("  NOTE: < 1 day of data -rates below are extrapolated and PRELIMINARY.")
    if len(sessions) > 3: P(f"  NOTE: {len(sessions)} restarts (deploy/test churn) inflate restart-censoring; transient, not real instability.")

    clean = [e for e in episodes if e["censored"] == "none"]
    eod   = [e for e in episodes if e["censored"] == "eod"]
    rstr  = [e for e in episodes if e["censored"] == "restart"]
    # "measured" = duration is a real lower bound we trust for persistence (clean + eod). EVERY headline
    # stat below uses this cohort — restart-censored episodes are book-init phantoms ([L20]) and must not
    # sit in the magnitude/sensitivity numbers either (pre-fix they inflated max/p90 + the grid).
    measured = clean + eod
    P("")
    P("EPISODES (one OPEN->CLOSE per market)")
    P(f"  total {len(episodes)}   clean {len(clean)}   eod-censored {len(eod)}   restart-censored {len(rstr)} (excluded from all stats below)")
    lagged = sum(1 for e in clean if e.get("lag_adj"))
    if lagged:
        P(f"  NOTE: {lagged} pre-0013 clean CLOSEs shifted -{CLOSE_FLUSH_LAG}s (debounce flush-stamp lag correction)")
    by_cat = {}
    for e in episodes: by_cat.setdefault(e["cat"], []).append(e)
    for cat, es in sorted(by_cat.items()):
        P(f"    {cat:8} {len(es)}")

    if not measured:
        P("\n(no clean/eod episodes yet -need OPENs that close without a restart in between)")
        return "\n".join(out)

    opens = [e["open_net"] for e in measured]
    durs  = [e["duration"] for e in measured]
    P("")
    P("EDGE MAGNITUDE at OPEN  (c per $1 of payout; already net of fees; measured episodes)")
    P(f"  median {_c(_pct(opens,50)):.2f}c   p75 {_c(_pct(opens,75)):.2f}c   p90 {_c(_pct(opens,90)):.2f}c   max {_c(max(opens)):.2f}c")
    for thr in (0.005, 0.01, 0.02, 0.05):
        n = sum(1 for x in opens if x >= thr)
        P(f"  open_net >= {_c(thr):>4.1f}c : {n:4d}  ({per_day(n):.0f}/day)")

    P("")
    P("PERSISTENCE  (episode duration = fill window; clean + eod episodes)")
    P(f"  median {_pct(durs,50):.0f}s   p75 {_pct(durs,75):.0f}s   p90 {_pct(durs,90):.0f}s   max {max(durs):.0f}s")
    for w in (5, 30, 60, 300):
        n = sum(1 for d in durs if d >= w)
        P(f"  lasts >= {w:>4d}s : {n:4d}/{len(durs)}  ({100.0*n/len(durs):.0f}%)")

    # CAPTURABLE must be FILLABLE: drop restart-censored (truncated durations) AND require measured depth
    # (open_c2 >= 1) so zero-/unmeasured-depth flickers don't inflate the headline (review WARN; matches capital_sim).
    cap = [e for e in measured if e["open_net"] >= edge_min and e["duration"] >= window_min and e["open_c2"] >= 1]
    cap_cents_day = per_day(sum(e["open_net"] for e in cap)) * 100.0
    floor_tag = "" if window_min > 0 else "  *** NO FILL-WINDOW FLOOR (window=0): counts un-fillable flickers ***"
    P("")
    P(f"CAPTURABLE  (open_net >= {_c(edge_min):.1f}c AND lasts >= {window_min}s AND measured depth c2>=1; excl. restart-censored)")
    P(f"  episodes        : {len(cap)}   ({per_day(len(cap)):.1f}/day)")
    P(f"  by category     : " + "  ".join(f"{c}={sum(1 for e in cap if e['cat']==c)}" for c in sorted(by_cat)))
    P(f"  scalability      : ~{cap_cents_day:.1f}c/day of capturable edge per $1 sized  (x your stake = $/day){floor_tag}")

    P("")
    P("SENSITIVITY  capturable/day  (rows=min edge c, cols=min window s; measured episodes)")
    windows = (5, 30, 60, 300)
    P("        " + "".join(f"{w:>8d}s" for w in windows))
    for em in (0.005, 0.01, 0.02):
        cells = []
        for w in windows:
            n = sum(1 for e in measured if e["open_net"] >= em and e["duration"] >= w)
            cells.append(f"{per_day(n):>8.1f} ")
        P(f"  {_c(em):>4.1f}c " + "".join(cells))

    top = sorted(measured, key=lambda e: e["peak_net"], reverse=True)[:8]
    P("")
    P("TOP EPISODES by peak edge")
    for e in top:
        P(f"  {_c(e['peak_net']):>5.2f}c peak  open {_c(e['open_net']):>4.1f}c  {e['duration']:>5.0f}s  {e['dir']:>3}  "
          f"flips {e['n_flip']}  {e['censored']:>7}  {e['market']}")
    P("=" * 78)
    return "\n".join(out)


# ============================================================================================
# SELF-TEST  (no I/O; synthetic transitions -> known episodes)
# ============================================================================================
def _selftest():
    print("persistence-analysis self-test")
    assert category("urc-us-seasonadj-gte-june-2026-07-02-atl4pt6") == "econ" and category("rdc-usfed-fomc-2026-06-17-maintains") == "econ"
    assert category("tc-temp-x") == "weather" and category("aec-mlb-x") == "sports"
    def tr(t, m, lab, net, d="PK", c2=None, age=None):
        r = {"t": t, "market": m, "transition": lab, "dir": d, "net_edge": net}
        if c2 is not None: r["depth"] = {"c2": c2, "c1": c2, "c0": c2}
        if age is not None: r["age"] = {"p": age[0], "k": age[1]}
        return r
    A = "aec-mlb-x-y-2026-06-10"; B = "tc-temp-laxhigh-2026-06-10-gte73"; C = "aec-mlb-p-q-2026-06-10"; D = "aec-nba-i-j-2026-06-10"
    recs = [
        tr(0, B, "OPEN", 0.02), tr(6, B, "OPEN", 0.02), tr(12, B, "CLOSE", -0.01),                              # B: restart at t4 cuts the 1st
        tr(20, A, "OPEN", 0.03, c2=300, age=(2, 40)), tr(25, A, "WIDEN", 0.05, c2=500), tr(28, A, "NARROW", 0.02), tr(30, A, "CLOSE", -0.01),  # clean dur10
        tr(35, C, "OPEN", 0.04),                                                                                # C: never closes -> eod at last_t=45
        tr(40, D, "OPEN", 0.02), tr(43, D, "OPEN", 0.09), tr(45, D, "CLOSE", -0.01),                            # D: dup OPEN (no restart) -> one episode
    ]
    sessions = [4]                                                                                              # restart hits only B (only open market at t4)
    eps = {e["market"]: e for e in build_episodes(recs, sessions, close_lag=0)}            # B's last (clean) wins
    allB = [e for e in build_episodes(recs, sessions, close_lag=0) if e["market"] == B]    # B has two episodes

    a = eps[A]
    assert a["censored"] == "none" and a["duration"] == 10 and a["open_net"] == 0.03 and a["peak_net"] == 0.05, a
    assert a["n_widen"] == 1 and a["n_narrow"] == 1
    assert a["open_c2"] == 300 and a["peak_c2"] == 500 and a["open_age"] == 40, a   # depth + staleness at open
    assert a["open_age_p"] == 2 and a["open_age_k"] == 40, a   # BOTH venue ages kept (the k=0 phantom tell, 0012)
    assert a["open_flat"] is True, a                           # tr() writes c2==c1==c0 -> flat-ladder flag set
    # time-weighted avg: .03*5 + .05*3 + .02*2 = .15+.15+.04 = .34 over 10 -> .034
    assert abs(a["twa_net"] - 0.034) < 1e-9, a["twa_net"]
    assert len(allB) == 2
    b_r = [e for e in allB if e["censored"] == "restart"][0]
    b_c = [e for e in allB if e["censored"] == "none"][0]
    assert b_r["open_t"] == 0 and b_r["duration"] == 4, b_r          # cut at the t=4 restart
    assert b_c["open_t"] == 6 and b_c["duration"] == 6, b_c          # fresh episode after restart
    c = eps[C]
    assert c["censored"] == "eod" and c["open_t"] == 35 and c["duration"] == 10, c   # last_t = 45 (D's CLOSE)
    d = eps[D]
    assert d["censored"] == "none" and d["duration"] == 5 and d["peak_net"] == 0.02, d   # dup OPEN ignored (no peak bump)
    print("  OK - episodes: clean / restart-cut / eod-censored / dup-OPEN-coalesced; twa + peak + both-age + flat correct")

    # debounce flush-stamp lag correction (0013): pre-fix CLOSE stamps carry +~1.25s; default policy
    # shifts them back, clamped at the last continuation so segments never go negative.
    lr = [tr(0, A, "OPEN", 0.03), tr(10, A, "CLOSE", -0.01)]
    e_lag = build_episodes(lr, [])[0]
    assert abs(e_lag["duration"] - (10 - 1.25)) < 1e-9 and e_lag["lag_adj"], e_lag       # 8.75, flagged
    lr2 = [tr(0, A, "OPEN", 0.03), tr(9.9, A, "WIDEN", 0.05), tr(10, A, "CLOSE", -0.01)]
    e_clamp = build_episodes(lr2, [])[0]
    assert abs(e_clamp["duration"] - 9.9) < 1e-9, e_clamp            # clamped at the 9.9 WIDEN, not 8.75
    assert build_episodes(lr, [], close_lag=0)[0]["duration"] == 10  # explicit 0 = correction off
    print("  OK - pre-0013 CLOSE flush-lag corrected (default), clamped at last continuation, opt-out works")

    # summary runs end-to-end on the synthetic set
    txt = summarize(sorted(recs, key=lambda r: r["t"]), sessions, build_episodes(recs, sessions, close_lag=0), 0.01, 5)
    assert "CAPTURABLE" in txt and "PERSISTENCE" in txt
    print("  OK - summary renders")
    print("self-test passed.")


# ============================================================================================
if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="cross-arb persistence analysis")
    ap.add_argument("--selftest", action="store_true", help="run the offline synthetic verification")
    ap.add_argument("--data-dir", default=os.path.join(os.path.dirname(__file__), "..", "..", "data", "cross-arb"))
    ap.add_argument("--edge-min", type=float, default=0.0, help="min net_edge fraction for 'capturable' (default 0 = every positive arb)")
    ap.add_argument("--window-min", type=float, default=0, help="min episode duration s for 'capturable' (default 0 = any)")
    args = ap.parse_args()

    if args.selftest:
        _selftest(); sys.exit(0)

    data_dir = os.path.abspath(args.data_dir)
    if not os.path.isdir(data_dir) or not glob.glob(os.path.join(data_dir, "transitions-*.jsonl*")):
        print(f"no data at {data_dir} -run `pwsh deploy/pull-data.ps1` first, or `--selftest`.")
        sys.exit(0)
    recs, sessions = load(data_dir)
    print(f"loaded {len(recs)} transitions + {len(sessions)} restarts from {data_dir}\n")
    print(summarize(recs, sessions, build_episodes(recs, sessions), args.edge_min, args.window_min))
