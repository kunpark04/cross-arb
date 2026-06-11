"""scripts/early_exit_ev.py — quantify the weather EARLY-EXIT option (probe #6, read-only).

QUESTION: a won weather pair can be exited in the evening liquidity window (high locked ~6 PM ET,
winning bucket bids 0.98-0.99 with depth — measured, scripts/exit_liquidity.py) instead of held to
settlement (~8-10 AM ET next day). Exiting costs the bid discount + taker fees but (i) frees capital
~13 h sooner and (ii) dodges the 8-11 AM *downward* CLI-correction tail on boundary days. Is it +EV?

LOSS STRUCTURE (the mechanics; research/settlement-verification.md):
  pmus locks at 8 AM on the FIRST CLI (its 11 AM delay triggers on CLI-vs-METAR inconsistency, NOT
  on a downward CLI correction per se); Kalshi waits for the FINAL (<= 11 AM).
  So a downward 8-11 AM correction crossing a bucket boundary grades the venues into DIFFERENT
  buckets — and the DIVERGING LEG IS ALWAYS THE KALSHI LEG (pm's outcome = the evening-known high).
  Per held pair on bucket X (dir 'P' = YES@pm + NO@K ; 'K' = YES@K + NO@pm), evening-known high m:
    X = winner bucket,  dir=K : flip -> pair pays 0  (lose BOTH legs)      EXPOSED
    X = winner bucket,  dir=P : flip -> pair pays 2  (double windfall)     FAVORABLE
    X below winner,     dir=P : flip INTO X -> 0                           EXPOSED
    X below winner,     dir=K : flip INTO X -> 2                           FAVORABLE
    X above winner, or winner is the low tail: downward can't touch it     SAFE (pays 1 regardless)
  The winning (~$1) leg of an EXPOSED pair is always the Kalshi leg, so selling it removes the whole
  exposure; a FAVORABLE pair's winning leg is the pm leg, and selling it KEEPS the Kalshi lottery
  ticket for free (the ~$0 Kalshi leg pays 1 on a flip). d_req = degF of downward correction needed
  to cross the relevant boundary; BOUNDARY pair = d_req == 1 (print on its bucket floor / adjacent).

EV per contract pair (hold baseline pays exactly 1.0 at settlement — NO settlement fee either venue:
Kalshi verbatim in the CFTC-filed schedule; pmus none mentioned; research/fee-pin-2026-06-10.md):
  EV(hold)        = 1 - P_eff (exposed) / 1 + P_eff (favorable) / 1 (safe)
  EV(exit winner) = b_win - fee(b_win) + r_lose + ctv  [+ P_eff if favorable: lottery retained]
  where P_eff = P(downward CLI correction >= d_req lands in the 8-11 AM window), b_win = evening bid
  on the winning leg's venue, r_lose = optional losing-leg recovery (sell the ~0 leg at its bid),
  ctv = value of the capital freed ~13 h sooner. Fees from bot/ledger.py (single source, L15) at the
  MARGINAL rate (per-$ EV; booking would ceil per order, L10). Kalshi WEATHER maker fee is $0 and
  pmus rebates makers 0.0125*p*(1-p) (fee-pin 2026-06-10) -> the maker-exit variant rests at the ask.

MEASURED inputs (computed live from the archive when present; defaults = the 2026-06-10/11 pull):
  * evening touches, winner bucket (n=10 px records, 1 bucket-evening, NYC 06-10 19:00-19:52 ET):
    pm YES bid 0.99 ; Kalshi YES bid 0.96-0.97 (mode 0.97), Kalshi ask 0.98.  exit_liquidity.py adds
    pm bids 0.98/0.99 on 2 more bucket-evenings. -> B_PM=0.99, B_K=0.965, ASK_K=0.98 + sensitivity.
  * capital rate: capital_sim cohort (>=1c, >=30s, clip 1000) profit/day over avg concurrent capital
    + the share of that edge arriving in the freed window (00Z-12Z = 8 PM-9 AM ET). PRELIMINARY.
  * downward-revision rate: cli_revisions chains — 0 downward in N station-days -> Jeffreys upper
    bound, plus the task's sensitivity grid P in {0.1%, 0.5%, 1%, 2%}.
  * boundary-day rate + held-pair exposure mix: cli.jsonl prints joined to the day's bucket tiling
    (recovered from archive slugs) x capturable weather pairs (analyze_persistence + capital_sim).

READ-ONLY. Pure stdlib. `--selftest` runs the offline checks (no data needed).

  python scripts/early_exit_ev.py --selftest
  python scripts/early_exit_ev.py [--data-dir PATH] [--cli PATH] [--b-pm 0.99] [--b-k 0.965]
"""
import os, sys, re, json, glob, gzip, math, argparse, collections, datetime

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bot"))
from ledger import kfee, pfee                                    # fee single-source (L15)
from colisted_map import pm_bounds, WX                            # verified bucket decoding (L17)
from analyze_persistence import load, build_episodes
from capital_sim import capturable, one_per_market, simulate
import cli_revisions

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "data", "cross-arb")
OUT_TXT = os.path.join(os.path.dirname(__file__), "_data", "early_exit_ev.txt")
OUT_JSON = os.path.join(os.path.dirname(__file__), "_data", "early_exit_ev.json")

# measured evening-window defaults (see module docstring; all overridable by CLI flags)
B_PM, B_K, ASK_K = 0.99, 0.965, 0.98     # pm winner bid / Kalshi winner bid / Kalshi winner ask
B_LOSE_PM, B_LOSE_K = 0.01, 0.02         # losing-leg bids: pm loser YES ~0.01; K loser NO = 1-ask ~0.02
PM_TOP = 0.99                            # price cap (no resting above 0.99 on either venue)
FRAC2 = 0.3                              # prior: P(correction >= 2F | a downward correction) — UNMEASURED
WINDOW_H = 13                            # capital freed ~8 PM ET (exit) -> ~9 AM ET (settlement payout)
P_GRID = (0.001, 0.005, 0.01, 0.02)      # task sensitivity grid for the flip probability

STATION = {c: s for c, s in [("sfo", "SFO"), ("lax", "LAX"), ("nyc", "NYC"), ("mia", "MIA"), ("mdw", "MDW")]}
assert set(STATION) == set(WX), "city keys must track colisted_map.WX"


# ============================================================================================
# Beta quantile (Jeffreys interval for the unobserved revision rate) — pure stdlib
# ============================================================================================
def _betacf(a, b, x, itmax=200, eps=3e-12):
    """Continued fraction for the incomplete beta (Numerical Recipes betacf)."""
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    if abs(d) < 1e-30: d = 1e-30
    d = 1.0 / d; h = d
    for m in range(1, itmax + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d; d = 1e-30 if abs(d) < 1e-30 else d
        c = 1.0 + aa / c; c = 1e-30 if abs(c) < 1e-30 else c
        d = 1.0 / d; h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d; d = 1e-30 if abs(d) < 1e-30 else d
        c = 1.0 + aa / c; c = 1e-30 if abs(c) < 1e-30 else c
        d = 1.0 / d; de = d * c; h *= de
        if abs(de - 1.0) < eps: break
    return h

def betainc(a, b, x):
    """Regularized incomplete beta I_x(a,b)."""
    if x <= 0: return 0.0
    if x >= 1: return 1.0
    ln = (math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log(1.0 - x))
    if x < (a + 1.0) / (a + b + 2.0):
        return math.exp(ln) * _betacf(a, b, x) / a
    return 1.0 - math.exp(ln) * _betacf(b, a, 1.0 - x) / b

def beta_ppf(q, a, b, tol=1e-10):
    """Quantile of Beta(a,b) by bisection."""
    lo, hi = 0.0, 1.0
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if betainc(a, b, mid) < q: lo = mid
        else: hi = mid
        if hi - lo < tol: break
    return (lo + hi) / 2.0

def jeffreys_upper(k, n, conf=0.95):
    """One-sided upper bound for a binomial rate with k/n observed (Jeffreys prior Beta(.5,.5))."""
    return beta_ppf(conf, k + 0.5, n - k + 0.5) if n > 0 else 1.0


# ============================================================================================
# Pair classification vs the evening-known high (the divergence-exposure taxonomy)
# ============================================================================================
def classify_pair(bounds, dir_, m):
    """-> {status: exposed|favorable|safe, d_req, win_venue} for a held pair on bucket `bounds`
    ((lo,hi) inclusive, None = open tail), direction dir_ ('P'|'K'), evening-known high m (degF).
    d_req = degF of downward correction needed for the venue-divergence; win_venue = the venue of the
    ~$1 winning leg you would sell to exit. See module docstring for the payoff table."""
    lo, hi = bounds
    in_bucket = (lo is None or m >= lo) and (hi is None or m <= hi)
    if in_bucket:                                       # holder of the presumptive WINNER bucket
        if lo is None:                                  # low tail: a downward move stays inside -> safe
            return {"status": "safe", "d_req": None, "win_venue": "P" if dir_ == "P" else "K"}
        d = m - lo + 1                                  # correction crossing below the floor
        return {"status": ("exposed" if dir_ == "K" else "favorable"), "d_req": d,
                "win_venue": "P" if dir_ == "P" else "K"}   # winning leg = the YES leg
    if hi is not None and hi < m:                       # holder of a bucket BELOW the winner
        d = m - hi                                      # correction landing inside [lo,hi]
        return {"status": ("exposed" if dir_ == "P" else "favorable"), "d_req": d,
                "win_venue": "K" if dir_ == "P" else "P"}   # winning leg = the NO leg (other venue)
    # above the winner: a downward correction moves further away -> loser on both venues regardless
    return {"status": "safe", "d_req": None, "win_venue": "K" if dir_ == "P" else "P"}


def p_eff(p1, d_req, frac2=FRAC2):
    """Effective divergence probability for a pair needing a d_req-degF downward correction.
    p1 = P(a >=1F downward correction lands in the 8-11 AM window). Corrections >= 2F are a FRAC2
    (prior, unmeasured) fraction of corrections; >= 3F treated as ~0."""
    if d_req is None: return 0.0
    if d_req <= 1: return p1
    if d_req == 2: return p1 * frac2
    return 0.0


# ============================================================================================
# EV per contract pair (cents). Hold baseline = 1.0 at settlement (no settlement fees).
# ============================================================================================
def exit_fee(venue, p):
    """Taker fee per contract for selling a leg at price p (marginal rates — per-$ EV, L10/L15)."""
    return kfee(p, marginal=True) if venue == "K" else pfee(p)

def exit_proceeds(win_venue, b_pm, b_k, taker=True, sell_lose=False, b_lose_pm=B_LOSE_PM, b_lose_k=B_LOSE_K):
    """Evening exit proceeds per pair: sell the winning (~$1) leg; optionally also the ~$0 leg.
    taker=False = MAKER variant: rest at the ask (Kalshi weather maker fee = $0; pm maker REBATES
    0.0125*p*(1-p)) — price improvement applies on Kalshi (bid 0.965 -> rest 0.98); pm winner bid is
    already at the 0.99 cap so maker adds only the rebate. Conditional on the resting order filling."""
    if win_venue == "K":
        b = b_k if taker else min(ASK_K, PM_TOP)
        fee = kfee(b, marginal=True) if taker else 0.0          # weather series fee_type=quadratic: maker $0
        lose_v, b_l = "P", b_lose_pm
    else:
        b = b_pm if taker else min(b_pm, PM_TOP)                # pm winner bid already ~at cap
        fee = pfee(b) if taker else -0.0125 * b * (1 - b)       # pm maker rebate (credit)
        lose_v, b_l = "K", b_lose_k
    out = b - fee
    if sell_lose and b_l > 0:
        out += b_l - (exit_fee(lose_v, b_l) if taker else 0.0)
    return out

def delta_exit_minus_hold(status, win_venue, p1, d_req, ctv, b_pm=B_PM, b_k=B_K, taker=True,
                          sell_lose=False, frac2=FRAC2):
    """EV(exit in the evening) - EV(hold to settlement), $ per contract pair.
    exposed  : exit removes the Kalshi-leg divergence -> + p_eff vs hold's expected loss.
    favorable: winner-leg-only exit KEEPS the Kalshi lottery (cancels); a FULL unwind sells it.
    safe     : pure cost-of-exit vs 1.0."""
    pe = p_eff(p1, d_req, frac2)
    proceeds = exit_proceeds(win_venue, b_pm, b_k, taker=taker, sell_lose=sell_lose)
    if status == "exposed":
        return (proceeds + ctv) - (1.0 - pe)
    if status == "favorable":
        lottery = 0.0 if sell_lose else pe              # full unwind forfeits the flip windfall
        return (proceeds + ctv + lottery) - (1.0 + pe)
    return (proceeds + ctv) - 1.0

def breakeven_p(win_venue, ctv, b_pm=B_PM, b_k=B_K, taker=True):
    """Flip probability at which exiting an EXPOSED boundary pair (d_req=1) breaks even with holding."""
    return 1.0 - exit_proceeds(win_venue, b_pm, b_k, taker=taker) - ctv


# ============================================================================================
# Measured inputs from the archive
# ============================================================================================
def cli_prints(path):
    """(station, report_date) -> first-issued daily max (the evening-known high; downward revisions
    observed: 0 so far). Reuses cli_revisions' chain reconstruction."""
    ch = cli_revisions.chains(cli_revisions.load(path))
    return {k: v[0][1] for k, v in ch.items() if v}

def weather_tilings(data_dir):
    """(city, date) -> sorted list of inclusive (lo,hi) bucket bounds recovered from archive slugs."""
    tiles = collections.defaultdict(set)
    for p in glob.glob(os.path.join(data_dir, "transitions-*.jsonl*")):
        op = gzip.open(p, "rt", encoding="utf-8") if p.endswith(".gz") else open(p, encoding="utf-8")
        for ln in op:
            try: r = json.loads(ln)
            except Exception: continue
            mk = r.get("market", "")
            c = re.search(r"tc-temp-([a-z]+?)high-(\d{4}-\d{2}-\d{2})", mk)
            if c:
                b = pm_bounds(mk)
                if b != (None, None): tiles[(c.group(1), c.group(2))].add(b)
        op.close()
    return {k: sorted(v, key=lambda b: (b[0] if b[0] is not None else -999)) for k, v in tiles.items()}

def held_weather_pairs(episodes):
    """The held-position population: capturable weather episodes (net>0, depth>=1, restart-censored
    dropped — capital_sim.capturable), ONE per market (first entry = the held position)."""
    wx = [e for e in episodes if e["cat"] == "weather"]
    return one_per_market(capturable(wx, 1e-9, 0))

def classify_population(pairs, prints):
    """Join held pairs to their station-day print -> classified list + the no-print remainder count."""
    out, no_print = [], 0
    for e in pairs:
        mk = e["market"]
        c = re.search(r"tc-temp-([a-z]+?)high-(\d{4}-\d{2}-\d{2})", mk)
        if not c: continue
        city, date = c.group(1), c.group(2)
        m = prints.get((STATION.get(city, "?"), date))
        if m is None:
            no_print += 1; continue
        cl = classify_pair(pm_bounds(mk), e["dir"], m)
        out.append({"market": mk, "city": city, "date": date, "dir": e["dir"], "print": m,
                    "bounds": pm_bounds(mk), "size_c2": e["open_c2"], **cl})
    return out, no_print

def boundary_day_rate(prints, tilings):
    """Across station-days with BOTH a print and a recovered tiling: was the print on its winning
    bucket's floor (a -1F correction crosses a boundary)? -> (n_boundary, n_joinable, details)."""
    nb, nj, det = 0, 0, []
    for (st, date), m in sorted(prints.items()):
        city = next((c for c, s in STATION.items() if s == st), None)
        tl = tilings.get((city, date))
        if not tl: continue
        win = next((b for b in tl if (b[0] is None or m >= b[0]) and (b[1] is None or m <= b[1])), None)
        if win is None: continue                      # print fell in an unlisted gap — skip
        nj += 1
        boundary = win[0] is not None and m == win[0]  # on the floor: -1F exits the bucket
        nb += boundary
        det.append({"station": st, "date": date, "print": m, "winner": win, "boundary": boundary})
    return nb, nj, det

def capital_rate(episodes, recs):
    """(rho_day = daily gross profit / avg concurrent capital, overnight edge share s_w, n) from the
    capital_sim cohort (>=1c, >=30s, clip 1000). PRELIMINARY (~days of data, walk-the-book model)."""
    cap = capturable(episodes, 0.01, 30)
    if not cap or not recs: return 0.0, 0.0, 0
    span_d = max((recs[-1]["t"] - recs[0]["t"]) / 86400.0, 1e-9)
    _, _, avg, profit = simulate(cap, 1000, 0.0, 28)
    rho = (profit / span_d) / avg if avg > 0 else 0.0
    win_h = set(range(0, WINDOW_H))                   # 00Z..12Z = 8 PM..9 AM ET (capital-freed window)
    tot = sw = 0.0
    for e in one_per_market(cap):                     # edge-weighted arrival share (capital_sim's profit weights)
        w = min(e["open_c2"], 1000) * max(0.0, (e["open_net"] + 0.005) / 2.0)
        tot += w
        if int((e["open_t"] // 3600) % 24) in win_h: sw += w
    return rho, (sw / tot if tot > 0 else 0.0), len(cap)


# ============================================================================================
# Report
# ============================================================================================
def fmt_c(x): return f"{x*100:+.2f}c"

def report(data_dir, cli_path, b_pm, b_k, frac2):
    L = []; P = L.append
    recs, sess = load(data_dir)
    eps = build_episodes(recs, sess)
    prints = cli_prints(cli_path)
    tilings = weather_tilings(data_dir)
    pairs = held_weather_pairs([e for e in eps if e["censored"] in ("none", "eod")])
    cls, no_print = classify_population(pairs, prints)
    nb, nj, bdet = boundary_day_rate(prints, tilings)
    rho, s_w, n_cap = capital_rate(eps, recs)
    cli_stats = cli_revisions.analyze(cli_revisions.load(cli_path))
    n_sd, n_down = cli_stats["n_station_days"], cli_stats["n_downward"]
    jup = jeffreys_upper(n_down, n_sd)
    ctv_hi = (b_pm) * rho * s_w                       # freed ~b for the 00Z-12Z window; marginal<=avg rate
    ctv_grid = [0.0, ctv_hi / 2.0, ctv_hi]

    P("=" * 96)
    P("WEATHER EARLY-EXIT EV  —  hold a WON pair to settlement vs exit in the evening window")
    P("=" * 96)
    P("MEASURED INPUTS (n per cell; everything from the live archive unless marked PRIOR)")
    P(f"  evening winner bids       : pm {b_pm:.3f} / Kalshi {b_k:.3f} (ask {ASK_K:.2f})   "
      f"[n=10 px records, 1 bucket-evening + 2 exit_liquidity probes — THIN]")
    P(f"  capital rate rho          : {rho*100:.1f}%/day on avg concurrent capital   "
      f"[n={n_cap} capturable arbs, PRELIMINARY]")
    P(f"  overnight edge share s_w  : {s_w*100:.0f}% of capturable edge arrives 8 PM-9 AM ET")
    P(f"  -> capital-freed value ctv: 0 .. {ctv_hi*100:.2f}c per pair (={b_pm:.2f} freed x rho x s_w; "
      f"UPPER bound — assumes the bankroll binds and marginal=average)")
    P(f"  downward CLI revisions    : {n_down}/{n_sd} station-days  ->  Jeffreys 95% upper "
      f"{jup*100:.1f}%/station-day (UPPER bound on P1; the in-window boundary-crossing rate is a "
      f"fraction of it). P1 sensitivity grid {[f'{p*100:.1f}%' for p in P_GRID]}")
    P(f"  boundary-day rate         : {nb}/{nj} joinable station-days had the print ON its bucket floor"
      f"   (a-priori ~50% for a 2F bucket; n TINY)")
    for d in bdet:
        P(f"      {d['station']} {d['date']}: print {d['print']} in {d['winner']}"
          + ("   <- BOUNDARY DAY (a -1F correction flips the Kalshi side)" if d["boundary"] else ""))
    P(f"  P(corr >= 2F | corr)      : {frac2:.1f}  [PRIOR — no measured correction magnitudes yet]")
    P("")

    # ---- held-pair population ----
    def _dlab(c):
        if c["d_req"] is None: return "-"
        return str(c["d_req"]) if c["d_req"] <= 2 else ">=3 (unreachable)"
    mix = collections.Counter((c["status"], _dlab(c)) for c in cls)
    P(f"HELD-PAIR POPULATION  (capturable weather pairs, one per market: {len(pairs)} held; "
      f"{len(cls)} classified vs a print, {no_print} await prints)")
    for (st, d), n in sorted(mix.items(), key=lambda kv: str(kv)):
        P(f"  {st:9} d_req={d:18} : {n}")
    expo1 = [c for c in cls if c["status"] == "exposed" and c["d_req"] == 1]
    expo2 = [c for c in cls if c["status"] == "exposed" and c["d_req"] == 2]
    fav = [c for c in cls if c["status"] == "favorable"]
    for c in cls:
        if c["status"] != "safe":
            P(f"    {c['market'][-34:]:36} dir={c['dir']} print={c['print']} {str(c['bounds']):12} "
              f"{c['status'].upper():9} d_req={c['d_req']} exit-leg on {c['win_venue']}")
    P("")

    # ---- per-pair EV table ----
    P("PER-PAIR EV: exit minus hold, cents per contract pair  (rows x P1; ctv columns)")
    P(f"  pair class                         exit-leg   " +
      "".join(f"  ctv={c*100:.1f}c".rjust(24) for c in ctv_grid))
    P(f"  {'':45}" + ("  " + "  ".join(f"P1={p*100:>4.1f}%" for p in P_GRID)) * 1)
    rows = [
        ("SAFE/FAVORABLE (taker, pm leg)", "safe", "P", True),
        ("SAFE (taker, Kalshi leg)", "safe", "K", True),
        ("EXPOSED boundary d=1 (taker, K leg)", "exposed", "K", True),
        ("EXPOSED boundary d=1 (MAKER@ask, K leg)", "exposed", "K", False),
        ("EXPOSED d=2 (taker, K leg)", "exposed2", "K", True),
    ]
    for label, st, wv, taker in rows:
        status = "exposed" if st.startswith("exposed") else st
        d_req = 2 if st == "exposed2" else (1 if st == "exposed" else None)
        for ctv in ctv_grid:
            cells = [delta_exit_minus_hold(status, wv, p1, d_req, ctv, b_pm, b_k, taker=taker, frac2=frac2)
                     for p1 in P_GRID]
            tag = label if ctv == ctv_grid[0] else ""
            P(f"  {tag:45}ctv={ctv*100:4.2f}c  " + "  ".join(f"{fmt_c(c):>8}" for c in cells))
        P("")

    # ---- breakeven ----
    P("BREAKEVEN flip probability P* for an EXPOSED boundary pair (exit pays iff P1 > P*)")
    for ctv in ctv_grid:
        bt = breakeven_p("K", ctv, b_pm, b_k, taker=True)
        bm = breakeven_p("K", ctv, b_pm, b_k, taker=False)
        P(f"  ctv={ctv*100:4.2f}c :  taker-exit P* = {bt*100:5.2f}%    maker-exit P* = {bm*100:5.2f}%"
          f"    (vs measured 0/{n_sd} down-revisions; Jeffreys ceiling {jup*100:.1f}%)")
    P("")

    # ---- policy table ----
    P("POLICY EV vs HOLD-ALL  ($ per evening on the measured classified population, per contract;")
    P("                        x size = $: median held size c2 "
      f"{sorted(c['size_c2'] for c in cls)[len(cls)//2] if cls else 0:.0f} contracts)")
    P("   policy                                P1=0.1%   P1=0.5%   P1=1.0%   P1=2.0%   (ctv mid; taker unless noted)")
    ctv_mid = ctv_grid[1]
    def policy_ev(p1, which, taker_exposed=True):
        tot = 0.0
        for c in cls:
            d = delta_exit_minus_hold(c["status"], c["win_venue"], p1, c["d_req"], ctv_mid,
                                      b_pm, b_k, taker=(taker_exposed if c["status"] == "exposed" else True),
                                      frac2=frac2)
            if which == "all": tot += d
            elif which == "boundary" and c["status"] == "exposed" and c["d_req"] == 1: tot += d
        return tot
    for name, which, tk in [("EXIT-ALL every evening (taker)", "all", True),
                            ("EXIT exposed-boundary only (taker)", "boundary", True),
                            ("EXIT exposed-boundary only (MAKER on K)", "boundary", False)]:
        cells = [policy_ev(p1, which, tk) for p1 in P_GRID]
        P(f"   {name:38}" + "  ".join(f"{fmt_c(c):>8}" for c in cells))
    P(f"   (HOLD-ALL baseline = 0; classified population n={len(cls)}, exposed-boundary n={len(expo1)}, "
      f"exposed d=2 n={len(expo2)}, favorable n={len(fav)})")
    P("")
    P("NOTES: a favorable pair's winner-leg exit keeps the Kalshi flip-lottery for free, so its delta")
    P("equals the safe pair's. A FULL unwind also sells the ~0 leg (pm ~0.01 / K NO ~0.02 = the market's")
    P("own price of the tail) — selling the K lottery at ~2c beats holding it whenever P1 < ~2%.")
    P("Cheaper tail-trim than exiting: BUY Kalshi YES on the flip-destination bucket at its ~1-2c ask")
    P("(pays 1 exactly when the exposed pair pays 0) — same protection, no 3.5c bid-discount; but it")
    P("ADDS capital instead of freeing it. Worth a measured ask before adopting any exit policy.")
    P("=" * 96)
    summary = {
        "n_station_days": n_sd, "n_downward": n_down, "jeffreys_upper": jup,
        "boundary_days": [nb, nj], "rho_day": rho, "s_w": s_w, "ctv_hi": ctv_hi,
        "b_pm": b_pm, "b_k": b_k, "pairs_held": len(pairs), "pairs_classified": len(cls),
        "exposed_boundary": len(expo1), "exposed_d2": len(expo2), "favorable": len(fav),
        "breakeven_taker_ctv0": breakeven_p("K", 0.0, b_pm, b_k, True),
        "breakeven_maker_ctv0": breakeven_p("K", 0.0, b_pm, b_k, False),
        "delta_safe_taker_pm_ctv0": delta_exit_minus_hold("safe", "P", 0.0, None, 0.0, b_pm, b_k),
        "classified": [{k: v for k, v in c.items() if k != "size_c2"} for c in cls],
    }
    return "\n".join(L), summary


# ============================================================================================
# SELF-TEST (offline; no data files)
# ============================================================================================
def _selftest():
    print("early-exit EV self-test")
    # beta machinery vs known values
    assert abs(betainc(0.5, 0.5, 0.5) - 0.5) < 1e-9                  # arcsine symmetry
    assert abs(beta_ppf(0.5, 2, 2) - 0.5) < 1e-6                     # symmetric median
    ju = jeffreys_upper(0, 14)                                       # 0/14 downward revisions
    assert 0.08 < ju < 0.20, ju                                      # ~13% — tighter than rule-of-3 (3/14=21%)
    assert jeffreys_upper(0, 100) < ju < jeffreys_upper(0, 5)        # monotone in n
    print(f"  OK - Jeffreys upper(0/14) = {ju*100:.1f}% (rule-of-three 21.4%)")

    # classification: the full divergence taxonomy (winner/below/above x dir x tails)
    assert classify_pair((90, 91), "K", 91) == {"status": "exposed", "d_req": 2, "win_venue": "K"}
    assert classify_pair((90, 91), "P", 91) == {"status": "favorable", "d_req": 2, "win_venue": "P"}
    assert classify_pair((79, None), "K", 79) == {"status": "exposed", "d_req": 1, "win_venue": "K"}   # tail floor print
    assert classify_pair((77, 78), "P", 79) == {"status": "exposed", "d_req": 1, "win_venue": "K"}     # adjacent below
    assert classify_pair((77, 78), "K", 79) == {"status": "favorable", "d_req": 1, "win_venue": "P"}
    assert classify_pair((None, 89), "K", 91)["status"] == "favorable"                                 # low tail below, d=2
    assert classify_pair((None, 89), "K", 91)["d_req"] == 2
    assert classify_pair((92, 93), "P", 91)["status"] == "safe"                                        # above winner
    assert classify_pair((None, 76), "K", 75)["status"] == "safe"                                      # low-tail WINNER: down can't exit
    # the exposed pair's winning leg is ALWAYS the Kalshi leg (both exposure shapes)
    for b, d, m in [((90, 91), "K", 91), ((77, 78), "P", 79)]:
        assert classify_pair(b, d, m)["win_venue"] == "K"
    print("  OK - exposure taxonomy (winner/below/above, tails, win-leg venue)")

    # EV arithmetic vs hand-computed cases (per contract pair, ctv=0)
    d_safe = delta_exit_minus_hold("safe", "P", 0.0, None, 0.0, 0.99, 0.965)
    assert abs(d_safe - (0.99 - pfee(0.99) - 1.0)) < 1e-12           # -(1c) - 0.05c fee ~ -1.05c
    assert -0.0106 < d_safe < -0.0104, d_safe
    d_exp = delta_exit_minus_hold("exposed", "K", 0.02, 1, 0.0, 0.99, 0.965)
    hand = (0.965 - kfee(0.965, marginal=True)) - (1.0 - 0.02)       # 2% flip vs 3.5c+0.24c exit cost
    assert abs(d_exp - hand) < 1e-12 and d_exp < 0                   # still negative at P1=2% taker
    d_mk = delta_exit_minus_hold("exposed", "K", 0.02, 1, 0.0, 0.99, 0.965, taker=False)
    assert abs(d_mk - (0.98 - (1.0 - 0.02))) < 1e-12                 # maker at 0.98, $0 fee -> breakeven at 2%
    assert d_mk > d_exp                                              # maker dominates taker on Kalshi weather
    # favorable: winner-leg exit keeps the lottery -> same delta as safe (P_eff cancels)
    d_fav = delta_exit_minus_hold("favorable", "P", 0.02, 1, 0.0, 0.99, 0.965)
    assert abs(d_fav - d_safe) < 1e-12
    # full unwind of a favorable pair forfeits the lottery but harvests the ~2c K NO bid
    d_full = delta_exit_minus_hold("favorable", "P", 0.02, 1, 0.0, 0.99, 0.965, sell_lose=True)
    assert d_full < d_fav + 0.02                                     # gains the 2c leg, loses the 2% lottery
    # d_req=2 scales by frac2; d_req>=3 ~ 0
    assert abs(p_eff(0.01, 2, 0.3) - 0.003) < 1e-12 and p_eff(0.01, 3) == 0.0
    print("  OK - EV deltas: safe -1.05c, exposed taker/maker, favorable lottery, full-unwind, p_eff")

    # breakevens: maker < taker; ctv shifts both down linearly
    bt0, bm0 = breakeven_p("K", 0.0, 0.99, 0.965, True), breakeven_p("K", 0.0, 0.99, 0.965, False)
    assert abs(bt0 - (1 - 0.965 + kfee(0.965, marginal=True))) < 1e-12     # ~3.74%
    assert abs(bm0 - 0.02) < 1e-12                                          # rest at 0.98, $0 fee -> 2.0%
    assert abs(breakeven_p("K", 0.005, 0.99, 0.965, True) - (bt0 - 0.005)) < 1e-12
    print(f"  OK - breakeven P*: taker {bt0*100:.2f}%, maker {bm0*100:.2f}% (ctv=0)")

    # boundary-day join on synthetic prints + tilings
    pr = {("SFO", "2026-06-10"): 79, ("MDW", "2026-06-10"): 91, ("NYC", "2026-06-10"): 82}
    tl = {("sfo", "2026-06-10"): [(None, 70), (77, 78), (79, None)],
          ("mdw", "2026-06-10"): [(90, 91), (92, 93)],
          ("nyc", "2026-06-10"): [(81, 82)]}
    nb, nj, det = boundary_day_rate(pr, tl)
    assert (nb, nj) == (1, 3), (nb, nj)                              # SFO tail-floor print = the boundary day
    assert [d["boundary"] for d in sorted(det, key=lambda x: x["station"])] == [False, False, True]
    print("  OK - boundary-day join (print on floor vs cap; tail handling)")
    print("self-test passed.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="weather early-exit EV (hold vs evening exit)")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--data-dir", default=DATA_DIR)
    ap.add_argument("--cli", default=os.path.join(DATA_DIR, "cli.jsonl"))
    ap.add_argument("--b-pm", type=float, default=B_PM, help="evening pm winner-leg bid (measured 0.98-0.99)")
    ap.add_argument("--b-k", type=float, default=B_K, help="evening Kalshi winner-leg bid (measured 0.96-0.97)")
    ap.add_argument("--frac2", type=float, default=FRAC2, help="PRIOR P(correction >= 2F | correction)")
    a = ap.parse_args()
    if a.selftest:
        _selftest(); sys.exit(0)
    dd = os.path.abspath(a.data_dir)
    if not glob.glob(os.path.join(dd, "transitions-*.jsonl*")):
        print(f"no data at {dd} — pull first, or --selftest."); sys.exit(0)
    txt, summary = report(dd, a.cli, a.b_pm, a.b_k, a.frac2)
    print(txt)
    os.makedirs(os.path.dirname(OUT_TXT), exist_ok=True)
    with open(OUT_TXT, "w", encoding="utf-8") as f: f.write(txt + "\n")
    with open(OUT_JSON, "w", encoding="utf-8") as f: json.dump(summary, f, indent=1, default=str)
    print(f"\nwrote {OUT_TXT}\n      {OUT_JSON}")
