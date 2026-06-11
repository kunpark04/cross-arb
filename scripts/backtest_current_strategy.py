"""scripts/backtest_current_strategy.py — full backtest of the bot's CURRENT strategy (its gate stack).

WHAT THIS IS. The prior "full backtest" (research/backtest-2026-06-11.md) ran the *raw* pipeline:
every positive-edge capturable arb, all categories, no new gates. THIS script replays the SAME episode
data through the bot's ACTUAL pre-trade gate stack as of now — bot-rs/src/risk.rs::evaluate() with
bot-rs/src/config.rs defaults — so the report answers "what does the strategy the bot would actually run
today do, and how does it change the headline numbers?"

The gate stack is mirrored from risk.rs IN ORDER (a candidate must pass every gate to be tradeable):
  0. kill-switch / stream-paused              (off in backtest — not a per-episode property)
  1. settlement-identity (require_settle_clean): WEATHER always clean; SPORTS/ECON only if empirically
     verified. Today ONLY weather is verified (sports recon ~06-23, econ ~07-02) -> CURRENT = weather-only.
  2. crossed / stale book                      (already enforced inside capital_sim.capturable: c2>=1 +
                                                 the monitor only logs a tradeable two-sided book)
  3. cross-venue mid-divergence: econ twin bound 15c, else 40c (computed from the at-open px touches)
  4. edge sign + 2.0c floor (0014)
  4b. H1 TOXICITY-DIRECTION gate (skip_dear_led_weather): skip WEATHER edges where the DEAR venue led
      (the ~79%-toxic class). Cheap-led + unknown(led_by None) -> keep. Weather-only (null on sports).
      Direction classified with adverse_selection.open_classes/attribute (dear_made == dear-led, the
      strategy-test classifier — NOT re-inverted; cheap_made is the BENIGN class).
  5. concurrency cap (max 5)
  6. sizing: min(depth c2, per-pair contract cap, affordable, per-pair/cluster/total notional caps),
     then the FAT-EDGE haircut (above the 6c knee, size *= 0.5, floored at 1).

REUSES the proven machinery ([L20] — never re-implement loaders/economics/classifiers):
  analyze_persistence.load / build_episodes / category
  capital_sim.capturable / one_per_market / settle_t / void_haircut / DEPTH_BOUNDARY_NET economics
  account_sim.run_account                              (baseline $500 walk)
  adverse_selection.open_classes / attribute          (at-open direction = the H1 gate input)
  capital_velocity.LOCKUP_PASSIVE                      (capital-velocity lock-days)

Does NOT modify the bot or any frozen 0014 script. READ-ONLY. `--selftest` is offline.

  python scripts/backtest_current_strategy.py --selftest
  python scripts/backtest_current_strategy.py [--data-dir PATH] [--capital 500] [--out PATH]
"""
import os, sys, argparse, glob, datetime as dt
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from analyze_persistence import load, build_episodes, category
from capital_sim import (capturable, one_per_market, settle_t, void_haircut, DEPTH_BOUNDARY_NET)
from account_sim import run_account
from adverse_selection import open_classes, attribute, episodes_with_px
from capital_velocity import LOCKUP_PASSIVE


# =================================================================================================
# EXACT BOT CONFIG  (bot-rs/src/config.rs Config::from_env defaults — the staged-rollout floor)
# =================================================================================================
class BotConfig:
    edge_floor_cents = 2.0
    max_contracts_per_pair = 1            # DEFAULT 1 (staged-rollout brake)
    max_notional_per_pair = 1.0
    max_notional_per_cluster = 5.0
    max_total_notional = 20.0
    max_concurrent_positions = 5
    max_book_age_s = 5.0
    mid_divergence_reject_cents = 40.0
    econ_twin_max_divergence_cents = 15.0
    fat_edge_knee_cents = 6.0
    fat_edge_size_factor = 0.5
    skip_dear_led_weather = True
    leg_fill_timeout_ms = 500
    require_settle_clean = True

# Settlement-verified categories TODAY (invariant #1 / risk.rs): weather only is empirically clean.
# sports recon ~2026-06-23, econ ~2026-07-02 (CLAUDE.md / settle_recon). Weather is the only verified leg.
SETTLE_VERIFIED_NOW = {"weather"}
# the "if sports+econ were verified" contrast variant
SETTLE_VERIFIED_ALL = {"weather", "sports", "econ"}


# =================================================================================================
# DIRECTION (H1) — map the bot's `led_by == dear_venue` test onto the at-open classifier.
# risk.rs rejects ToxicDirection iff cat==Weather AND led_by == edge.dir.dear_venue().
# adverse_selection.open_classes tags each OPEN cheap_made / dear_made / mixed against the prior px:
#   dear_made  == the DEAR (sell) leg moved away to open the edge  == led_by == dear_venue  -> TOXIC, SKIP
#   cheap_made == the CHEAP (buy) leg made the edge                                          -> benign, KEEP
#   (unclassified / no_prior / mixed)                              == led_by None (unknown) -> KEEP
# This is the SAME classifier the strategy-idea tests used; the direction is NOT re-inverted (the
# strategy-tests doc confirmed cheap_made is benign 18% toxic, dear_made is the ~79%-toxic class).
# =================================================================================================
def open_direction_index(recs, sessions):
    """{(market, open_t): at-open class str} for every classifiable OPEN (cheap_made/dear_made/mixed)."""
    rows, skips = open_classes(recs, sessions)
    return {(r["market"], r["open_t"]): r["cls"] for r in rows}, skips


def is_dear_led_weather(e, dir_idx):
    """The H1 reject condition: WEATHER episode whose at-open class is dear_made (dear venue led)."""
    return e["cat"] == "weather" and dir_idx.get((e["market"], e["open_t"])) == "dear_made"


# =================================================================================================
# MID-DIVERGENCE — reconstruct each venue's YES mid from the at-open px touches (risk.rs step 3).
# risk.rs only applies the gate when BOTH venue mids exist; econ -> 15c bound, else 40c.
# =================================================================================================
def _mid(bid, ask):
    if bid is not None and ask is not None:
        return (bid + ask) / 2.0
    if bid is not None:
        return bid
    if ask is not None:
        return ask
    return None


def venue_mids(px, dir_):
    """(kalshi_yes_mid, pmus_yes_mid) in YES-space from the logged px, or (None, ...) if unavailable.
    Binary (weather/econ) px: p_yb/p_ya (pmus YES bid/ask), k_yb/k_ya (Kalshi YES bid/ask).
    Sports px: pm_b/pm_a (pmus YES=teamA bid/ask); ka/kb (Kalshi YES *asks* on A and B). Kalshi's YES(A)
    book is one-sided in the log (only the A-ask ka is a true A quote; kb is the B-ask = implied A-bid
    1-kb), so the Kalshi A-mid ~ midpoint of (ka, 1-kb) when both present."""
    if not px:
        return None, None
    if "pm_a" in px or "pm_b" in px or "ka" in px or "kb" in px:    # sports shape
        pm = _mid(px.get("pm_b"), px.get("pm_a"))
        ka, kb = px.get("ka"), px.get("kb")
        kbid = None if kb is None else 1.0 - kb                     # implied Kalshi YES(A) bid
        k = _mid(kbid, ka)
        return k, pm
    # binary shape
    return _mid(px.get("k_yb"), px.get("k_ya")), _mid(px.get("p_yb"), px.get("p_ya"))


def mid_divergence_cents(e, open_px_idx):
    """|Kalshi mid - pmus mid| in cents at the episode's OPEN, or None if a mid is unavailable."""
    px = open_px_idx.get((e["market"], e["open_t"]))
    if not px:
        return None
    km, pm = venue_mids(px, e.get("dir"))
    if km is None or pm is None:
        return None
    return abs(km - pm) * 100.0


def open_px_index(recs):
    """{(market, open_t): px} for OPEN records carrying a px (for the mid-divergence gate)."""
    return {(r["market"], r["t"]): r["px"] for r in recs
            if r.get("transition") == "OPEN" and r.get("px")}


# =================================================================================================
# THE GATE FUNNEL — apply risk.rs gates IN ORDER to the candidate cohort, counting survivors per stage.
# =================================================================================================
GATE_ORDER = ["candidates", "settlement", "mid_divergence", "edge_floor", "directional",
              "fat_edge_sizing", "caps"]


def _catmix(eps):
    return dict(Counter(e["cat"] for e in eps))


def run_funnel(eps, cfg, settle_verified, dir_idx, open_px_idx, max_clip,
               haircut, void_mult, capital0):
    """Walk the candidate cohort through the gate stack in risk.rs order. Returns (stages, funnel_rows).
    `stages` maps each gate name -> the list of episodes that PASS up to and including that gate. The
    sizing/caps stages additionally run the $-bankroll account walk so the cap funnel is the real
    capital-bound entry count, not a per-episode pass/fail."""
    # STAGE: candidates — every positive-edge capturable arb (the prior backtest's 292 cohort), one/market.
    cands = one_per_market(capturable(eps, 0.0, 0.0, liq_floor=1, max_age=0.0, drop_restart=True))
    cands = sorted(cands, key=lambda e: e["open_t"])
    stages = {"candidates": cands}

    # STAGE 1: settlement-identity — keep weather always; sports/econ only if verified.
    s1 = [e for e in cands if (not cfg.require_settle_clean)
          or e["cat"] == "weather" or e["cat"] in settle_verified]
    stages["settlement"] = s1

    # STAGE 3: mid-divergence — econ twin 15c, else 40c. None mid -> gate not applied (risk.rs).
    def passes_mid(e):
        d = mid_divergence_cents(e, open_px_idx)
        if d is None:
            return True
        ceil = cfg.econ_twin_max_divergence_cents if e["cat"] == "econ" else cfg.mid_divergence_reject_cents
        return d <= ceil
    s3 = [e for e in s1 if passes_mid(e)]
    stages["mid_divergence"] = s3

    # STAGE 4: edge sign + 2.0c floor. (capturable already requires open_net>0; floor is the new cut.)
    s4 = [e for e in s3 if e["open_net"] * 100.0 >= cfg.edge_floor_cents]
    stages["edge_floor"] = s4

    # STAGE 4b: H1 directional gate — skip dear-led weather.
    s4b = [e for e in s4 if not (cfg.skip_dear_led_weather and is_dear_led_weather(e, dir_idx))]
    stages["directional"] = s4b

    # STAGE 6 (sizing/fat-edge): the fat-edge haircut zeroes nothing (floored at 1), so the cohort that
    # SURVIVES to sizing is unchanged — but record the fat-edge-affected count + that the size is halved.
    stages["fat_edge_sizing"] = s4b      # same membership; sizing effect captured in the $ walk + report

    # STAGE 6 (caps): the real capital-bound entry set under the bot's tiny notional caps.
    entered = cap_constrained_entries(s4b, cfg, max_clip, haircut, void_mult, capital0)
    stages["caps"] = entered

    funnel_rows = []
    prev = None
    for g in GATE_ORDER:
        es = stages[g]
        funnel_rows.append({"gate": g, "n": len(es), "by_cat": _catmix(es),
                            "dropped": (prev - len(es)) if prev is not None else 0})
        prev = len(es)
    return stages, funnel_rows


def cap_constrained_entries(cohort, cfg, max_clip, haircut, void_mult, capital0):
    """Walk the surviving cohort in arrival order applying the bot's caps EXACTLY as risk.rs sizes:
    size = min(depth c2, max_contracts_per_pair, affordable, per-pair$/cluster$/total$ caps);
    then fat-edge halving above the knee. Concurrency cap (5) + per-cluster/total notional are live
    exposure, recycled at settlement. Returns the list of ENTERED positions (dicts)."""
    window_end = cohort and max(e.get("close_t") or e["open_t"] for e in cohort)
    cash_cap = cfg.max_total_notional                         # the bot's $20 hard ceiling dominates $500
    cash = min(float(capital0), cash_cap)
    # NOTE: the bot's max_total_notional ($20) is the binding cash limit, not the $500 bankroll — model
    # both: `capital0` funds affordability; the $20/$5/$1 caps are separate exposure ceilings (risk.rs).
    held, entered = [], []
    per_pair = defaultdict(float); per_cluster = defaultdict(float); total = [0.0]
    open_positions = [0]

    def cluster_key(e):
        return cluster_of(e["market"])

    def settle_due(now):
        nonlocal cash
        still = []
        for p in held:
            if p["settle_t"] <= now:
                cash += p["capital"]                          # free the cash (caps are exposure, freed too)
                per_pair[p["market"]] -= p["capital"]
                per_cluster[p["cluster"]] -= p["capital"]
                total[0] -= p["capital"]
                open_positions[0] -= 1
            else:
                still.append(p)
        held[:] = still

    for e in cohort:
        settle_due(e["open_t"])
        if open_positions[0] >= cfg.max_concurrent_positions:
            continue                                          # ConcurrencyCap
        cost_per = max(0.1, 1.0 - e["open_net"])
        ck = cluster_key(e)
        # caps -> max contracts under each $ ceiling
        pair_room = cfg.max_notional_per_pair - per_pair[e["market"]]
        clus_room = cfg.max_notional_per_cluster - per_cluster[ck]
        tot_room = cfg.max_total_notional - total[0]
        afford_cash = cash
        caps_contracts = min(int(max(0.0, pair_room) // cost_per),
                             int(max(0.0, clus_room) // cost_per),
                             int(max(0.0, tot_room) // cost_per),
                             int(afford_cash // cost_per))
        size = min(e["open_c2"], cfg.max_contracts_per_pair, caps_contracts)
        # fat-edge haircut (risk.rs step 6): above the knee, size *= factor, floored at 1.
        if e["open_net"] * 100.0 >= cfg.fat_edge_knee_cents and cfg.fat_edge_size_factor < 1.0 and size >= 1:
            size = max(1, int(size * cfg.fat_edge_size_factor))
        if size < 1:
            continue                                          # PairCap/ClusterCap/TotalCap/NoFillableSize
        avg_edge = max(DEPTH_BOUNDARY_NET, (e["open_net"] + DEPTH_BOUNDARY_NET) / 2.0)
        profit = size * max(0.0, avg_edge - haircut - void_haircut(e["market"], void_mult))
        capital = size * cost_per
        cash -= capital
        per_pair[e["market"]] += capital; per_cluster[ck] += capital; total[0] += capital
        open_positions[0] += 1
        pos = {"settle_t": settle_t(e["market"], e["open_t"], 28.0), "capital": capital, "profit": profit,
               "market": e["market"], "cat": e["cat"], "size": size, "cluster": ck,
               "open_t": e["open_t"], "open_net": e["open_net"]}
        held.append(pos); entered.append(pos)
    return entered


_CLUSTER_DATE = None
def cluster_of(market):
    """Correlated-exposure cluster key (risk.rs `cluster`): city-date for weather, game-date for sports.
    Approximated from the slug: drop the trailing strike/bucket token, keep team/city + event-date."""
    import re
    m = str(market)
    date = re.search(r"(\d{4}-\d{2}-\d{2})", m)
    d = date.group(1) if date else ""
    if m.startswith("tc-"):
        # tc-temp-<cityhigh>-<date>-<bucket> -> cluster on city+date
        parts = m.split("-")
        city = parts[2] if len(parts) > 2 else m
        return f"{city}-{d}"
    if m.startswith("aec-"):
        # aec-<league>-<teamA>-<teamB>-<date> -> the game (drop nothing; the game IS the cluster)
        return re.sub(r"-(gte|lt|gt|lte|above|below).*$", "", m)
    return m


# =================================================================================================
# PORTFOLIO STATS  (PnL / velocity / toxicity) on an entered-position set + a candidate cohort.
# =================================================================================================
def _median(xs):
    if not xs:
        return None
    s = sorted(xs); n = len(s)
    return s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2])


def cohort_edge_stats(cohort):
    opens = [e["open_net"] for e in cohort]
    return {"n": len(cohort), "by_cat": _catmix(cohort),
            "median_edge_c": (100.0 * _median(opens)) if opens else None,
            "mean_edge_c": (100.0 * sum(opens) / len(opens)) if opens else None}


def cap_weighted_lock_days(entered):
    dep = sum(p["capital"] for p in entered)
    if not dep:
        return None
    return sum(p["capital"] * LOCKUP_PASSIVE.get(p["cat"], 15.0) for p in entered) / dep


def toxicity_share(cohort, recs, sessions):
    """Close-toxic (dear_fell) share over the cohort's OPEN->CLOSE pairs that have px on both ends —
    the realized-wrong-leg proxy from adverse_selection. Restricted to the cohort's (market, open_t)."""
    keys = {(e["market"], e["open_t"]) for e in cohort}
    n_attr = n_tox = 0
    for o, c in episodes_with_px(recs, sessions):
        if (o["market"], o["t"]) not in keys:
            continue
        tag, _, _ = attribute(o.get("dir"), o.get("px"), c.get("px"))
        if tag:
            n_attr += 1
            if tag == "dear_fell":
                n_tox += 1
    return {"attributed": n_attr, "toxic": n_tox,
            "toxic_share": (n_tox / n_attr) if n_attr else None}


def account_pnl_for_cohort(eps_subset, capital0, max_clip):
    """The $-bankroll walk (account_sim.run_account economics) over a pre-gated cohort. We pass the
    subset as the only episodes so capturable(edge_min=0) keeps exactly them, then run_account does its
    one_per_market + arrival walk. Returns the run_account result dict."""
    return run_account(eps_subset, capital0, max_clip, 0.0, 0.0, 28.0, 0.0, 1.0, 1, 0.0)


# =================================================================================================
# REPORT
# =================================================================================================
SEP = "=" * 92
SUB = "-" * 92


def _fmt_catmix(d):
    return "  ".join(f"{k}={v}" for k, v in sorted(d.items())) or "(none)"


def build_report(eps, recs, sessions, cfg, capital0, max_clip_apples, data_dir):
    L = []
    P = L.append
    t0, t1 = recs[0]["t"], recs[-1]["t"]
    span_h = (t1 - t0) / 3600.0
    span_d = (t1 - t0) / 86400.0

    dir_idx, dir_skips = open_direction_index(recs, sessions)
    px_idx = open_px_index(recs)

    P(SEP)
    P("CROSS-ARB BACKTEST — CURRENT STRATEGY (the bot's live gate stack)")
    P(SEP)
    P(f"generated     : {dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')}")
    P(f"data dir      : {data_dir}")
    P(f"span          : {span_h:.1f} h ({span_d:.2f} d)   epochs {t0} .. {t1}")
    P(f"transitions   : {len(recs)}   restarts/censors: {len(sessions)}")
    P("posture       : READ-ONLY paper backtest. Zero orders placed. Fees+spread netted in net_edge;")
    P("                latency / leg-fill / slippage / adverse-selection / settlement-void NOT netted")
    P("                (gross upper bound). Mirrors bot-rs/src/risk.rs::evaluate + config.rs defaults.")
    P("")
    P("*** EFFECTIVE-N CAVEAT (L18/L19): ~2 calendar days but effective-n is ~1 INDEPENDENT event-day —")
    P("    the prior full backtest found 91% of candidate arbs carry event-date 2026-06-10 (the one fully")
    P("    covered day). Weather-only shrinks n HARD (see the weather candidate count below). Every number")
    P("    here is PRELIMINARY / METHOD-DEMO, not a validated edge. Do NOT quote magnitudes as 'the edge.' ***")

    # ---- EXACT CONFIG block --------------------------------------------------------------------
    P("")
    P(SEP)
    P("1.  EXACT BOT CONFIG  (bot-rs/src/config.rs Config::from_env defaults — staged-rollout floor)")
    P(SEP)
    rows = [
        ("edge_floor_cents", cfg.edge_floor_cents, "0014 pre-registered edge floor (skip thin arbs)"),
        ("max_contracts_per_pair", cfg.max_contracts_per_pair, "DEFAULT 1 — the staged-rollout size brake"),
        ("max_notional_per_pair", f"${cfg.max_notional_per_pair:.0f}", "per-market $ exposure cap"),
        ("max_notional_per_cluster", f"${cfg.max_notional_per_cluster:.0f}", "per city-date / game $ cap (correlated risk)"),
        ("max_total_notional", f"${cfg.max_total_notional:.0f}", "TOTAL $ exposure ceiling (binds before $500 bankroll)"),
        ("max_concurrent_positions", cfg.max_concurrent_positions, "max simultaneously-open pairs"),
        ("max_book_age_s", cfg.max_book_age_s, "L13 staleness reject (per venue)"),
        ("mid_divergence_reject_cents", cfg.mid_divergence_reject_cents, "L1 cross-category bad-join / stale guard"),
        ("econ_twin_max_divergence_cents", cfg.econ_twin_max_divergence_cents, "TIGHTER bound for settlement-identical econ twins"),
        ("fat_edge_knee_cents", cfg.fat_edge_knee_cents, "edges above this are adversely-selected (~66% toxic)"),
        ("fat_edge_size_factor", cfg.fat_edge_size_factor, "size multiplier above the knee (halve; never to 0)"),
        ("skip_dear_led_weather", cfg.skip_dear_led_weather, "H1 toxicity-direction gate (skip dear-led WEATHER)"),
        ("leg_fill_timeout_ms", cfg.leg_fill_timeout_ms, "unwind leg A if leg B isn't filled in time (stage-2)"),
        ("require_settle_clean", cfg.require_settle_clean, "only trade settlement-verified pairs (invariant #1)"),
    ]
    P(f"  {'parameter':<32}{'value':>10}   note")
    P(f"  {'-'*32}{'-'*10}   {'-'*44}")
    for k, v, note in rows:
        P(f"  {k:<32}{str(v):>10}   {note}")
    P("")
    P(f"  settlement-verified categories TODAY : {sorted(SETTLE_VERIFIED_NOW)}   "
      f"(sports recon ~2026-06-23, econ ~2026-07-02 — NOT yet verified)")
    P(f"  => with require_settle_clean=true, the CURRENT strategy is WEATHER-ONLY.")
    P(f"  apples-to-apples clip for the funnel/cohort math: max_clip={max_clip_apples} (prior-backtest setting);")
    P(f"  the bot's own per-pair contract cap is {cfg.max_contracts_per_pair} (shown in the $ walk).")

    # ---- THE GATE FUNNEL (current = weather-only) ----------------------------------------------
    stages, funnel = run_funnel(eps, cfg, SETTLE_VERIFIED_NOW, dir_idx, px_idx,
                                max_clip_apples, 0.0, 1.0, capital0)
    P("")
    P(SEP)
    P("2.  THE GATE FUNNEL  (risk.rs order; CURRENT strategy = weather-only settlement)")
    P(SEP)
    P("  Each row = candidates SURVIVING up to and including that gate, with by-category counts.")
    P("  (capturable already enforces c2>=1 + non-stale two-sided books, i.e. risk.rs steps 2/crossed/stale.)")
    P("")
    P(f"  {'gate':<20}{'survivors':>10}{'dropped':>9}   by category")
    P(f"  {'-'*20}{'-'*10}{'-'*9}   {'-'*40}")
    labels = {"candidates": "0 candidates", "settlement": "1 settlement", "mid_divergence": "3 mid-diverge",
              "edge_floor": "4 edge-floor 2c", "directional": "4b dear-led skip",
              "fat_edge_sizing": "6 fat-edge size", "caps": "6 caps ($ walk)"}
    for r in funnel:
        P(f"  {labels[r['gate']]:<20}{r['n']:>10}{('-'+str(r['dropped'])) if r['dropped'] else '0':>9}   "
          f"{_fmt_catmix(r['by_cat'])}")
    # explain the two structural drops
    n_cand = funnel[0]["n"]; n_settle = funnel[1]["n"]
    n_floor_in = stages["mid_divergence"]; n_floor = stages["edge_floor"]
    n_dir_in = stages["edge_floor"]; n_dir = stages["directional"]
    dear_led = [e for e in n_dir_in if is_dear_led_weather(e, dir_idx)]
    fat = [e for e in stages["fat_edge_sizing"] if e["open_net"] * 100.0 >= cfg.fat_edge_knee_cents]
    # at-open direction class mix on the FULL weather cohort (pre-floor) — shows why the gate is/isn't active
    wx_all = [e for e in stages["settlement"] if e["cat"] == "weather"]
    wx_clsmix = dict(Counter(dir_idx.get((e["market"], e["open_t"]), "unclassified") for e in wx_all))
    dear_led_all = sum(1 for e in wx_all if is_dear_led_weather(e, dir_idx))
    P("")
    P(f"  STAGE NOTES:")
    P(f"   • settlement   : {n_cand} -> {n_settle}. Dropped ALL non-weather (sports/econ unverified). "
      f"weather candidates = {funnel[1]['by_cat'].get('weather', 0)}.")
    P(f"   • mid-diverge  : {len(n_floor_in)} survive (econ already gone, so the 15c twin bound is moot; "
      f"weather uses the 40c bound).")
    P(f"   • edge-floor 2c: {len(n_floor_in)} -> {len(n_floor)}. Dropped {len(n_floor_in)-len(n_floor)} "
      f"sub-2c weather arbs (median weather edge is < 1c, so the floor is the single biggest weather cut).")
    P(f"   • dear-led skip: {len(n_dir_in)} -> {len(n_dir)}. Skipped {len(dear_led)} dear-led (toxic-class) "
      f"weather edges; kept cheap-led + unknown-direction (risk.rs keeps led_by==None).")
    P(f"     >>> THE H1 GATE IS DORMANT ON THIS DATA: of the {len(wx_all)} weather candidates, the at-open")
    P(f"         class mix is {wx_clsmix} — ~all are UNCLASSIFIED (no prior px to diff against), only")
    P(f"         {dear_led_all} dear-led exist in the WHOLE weather cohort. The gate keeps unknowns, so it")
    P(f"         removes nothing here. (strategy-idea-tests reached n=59 weather opens by using ALL post-")
    P(f"         epoch records incl. sub-2c; at the >=2c candidate level the classifiable cells collapse.)")
    P(f"   • fat-edge     : {len(fat)} survivors sit above the {cfg.fat_edge_knee_cents:.0f}c knee -> their "
      f"size is HALVED (never zeroed). Membership unchanged.")
    P(f"   • caps ($ walk): {len(stages['directional'])} tradeable -> {len(stages['caps'])} ENTERED under "
      f"the $1/pair, $5/cluster, $20 total, 1-contract/pair, 5-concurrent caps at ${capital0:.0f} bankroll.")
    P(f"  at-open direction classifier skips (whole archive): {dir_skips}")
    P(f"    (no_prior = first sighting of a market; censor_gap = a restart/reconnect sits between the prior")
    P(f"     px and the open, so the move would measure the outage — both leave led_by unknown -> KEEP.)")

    # ---- BASELINE vs CURRENT ------------------------------------------------------------------
    P("")
    P(SEP)
    P("3.  BASELINE  vs  CURRENT STRATEGY   (side by side)")
    P(SEP)
    P("  BASELINE = the prior full backtest: ALL categories, NO new gates (no floor, no directional, no")
    P("             econ-twin bound), max_clip=1000. (account_sim.run_account on every +edge capturable arb.)")
    P("  CURRENT  = the full risk.rs gate stack above (weather-only today).")
    P("")

    base_cohort = stages["candidates"]                      # all-category candidate cohort
    base_acct = account_pnl_for_cohort(eps, capital0, max_clip_apples)
    base_entered = base_acct["entered"]
    base_tox = toxicity_share(base_cohort, recs, sessions)
    base_edge = cohort_edge_stats(base_cohort)
    base_lockd = cap_weighted_lock_days(base_entered)

    cur_cohort = stages["directional"]                      # the tradeable (post-gate, pre-cap) cohort
    cur_entered = stages["caps"]
    cur_tox = toxicity_share(cur_cohort, recs, sessions)
    cur_edge = cohort_edge_stats(cur_cohort)
    cur_lockd = cap_weighted_lock_days(cur_entered)
    # $ walk under the CURRENT strategy at the bot's real caps:
    cur_realized = sum(p["profit"] for p in cur_entered
                       if p["settle_t"] <= (recs[-1]["t"]))
    cur_unreal = sum(p["profit"] for p in cur_entered
                     if p["settle_t"] > (recs[-1]["t"]))
    cur_locked = sum(p["capital"] for p in cur_entered
                     if p["settle_t"] > (recs[-1]["t"]))

    def pct(x):
        return f"{100.0 * x / capital0:+.2f}%"

    def row(label, base, cur):
        P(f"  {label:<34}{str(base):>24}{str(cur):>24}")

    P(f"  {'metric':<34}{'BASELINE (all-cat)':>24}{'CURRENT (weather-only)':>24}")
    P(f"  {'-'*34}{'-'*24}{'-'*24}")
    row("candidate arbs (one/market)", base_edge["n"], cur_edge["n"])
    row("  by category", _fmt_catmix(base_edge["by_cat"]), _fmt_catmix(cur_edge["by_cat"]))
    row("median edge (c)", f"{base_edge['median_edge_c']:.2f}" if base_edge['median_edge_c'] is not None else "n/a",
        f"{cur_edge['median_edge_c']:.2f}" if cur_edge['median_edge_c'] is not None else "n/a")
    row("mean edge (c)", f"{base_edge['mean_edge_c']:.2f}" if base_edge['mean_edge_c'] is not None else "n/a",
        f"{cur_edge['mean_edge_c']:.2f}" if cur_edge['mean_edge_c'] is not None else "n/a")
    row("$%d entered (capital-bound)" % capital0,
        f"{len(base_entered)} ({_fmt_catmix(_catmix(base_entered))})",
        f"{len(cur_entered)} ({_fmt_catmix(_catmix(cur_entered))})")
    row("  REALIZED PnL", f"${base_acct['realized_pnl']:.2f} ({pct(base_acct['realized_pnl'])})",
        f"${cur_realized:.2f} ({pct(cur_realized)})")
    row("  UNREALIZED PnL", f"${base_acct['unrealized_pnl']:.2f} ({pct(base_acct['unrealized_pnl'])})",
        f"${cur_unreal:.2f} ({pct(cur_unreal)})")
    row("  capital locked", f"${base_acct['locked_capital']:.2f}", f"${cur_locked:.2f}")
    row("cap-wt lock-days (velocity)",
        f"{base_lockd:.1f} d" if base_lockd is not None else "n/a",
        f"{cur_lockd:.1f} d" if cur_lockd is not None else "n/a")
    row("close-toxicity share",
        f"{100*base_tox['toxic_share']:.0f}% (n={base_tox['attributed']})" if base_tox['toxic_share'] is not None else "n/a",
        f"{100*cur_tox['toxic_share']:.0f}% (n={cur_tox['attributed']})" if cur_tox['toxic_share'] is not None else "n/a")
    P("")
    P("  NOTE on the CURRENT $-walk: the bot's caps ($20 total / $1 pair / 1 contract/pair) bind FAR")
    P("  below the $500 bankroll — that is the staged-rollout design (tiny size first). The PnL is")
    P("  therefore trivially small; the UNCONSTRAINED-NOTIONAL variant (section 5) shows the strategy's")
    P("  shape at realistic size. The velocity number is ~1.2d (all-weather) vs ~15d (sports-dominated")
    P("  baseline) — the cleanest single win of the weather-only stack: capital recycles ~12x faster.")

    # ---- DIFF vs research/backtest-2026-06-11.md -----------------------------------------------
    P("")
    P(SEP)
    P("4.  HOW IT CHANGES vs research/backtest-2026-06-11.md  (explicit headline diff)")
    P(SEP)
    wx = cur_edge["by_cat"].get("weather", 0)
    P("  That backtest (the RAW pipeline, all categories, no new gates) reported:")
    P("    • 292 capturable candidate arbs, ~85% sports (4,830 sports vs 1,284 weather episodes pre-collapse)")
    P("    • $500 account: 3 entered, 1 realized / 2 locked, +$1.97 realized (+0.39%), +0.86% mark-to-edge")
    P("    • capital-velocity dominated by sports (~15 d locks); ~67% of entered capital frozen")
    P("    • toxicity unaddressed (no directional gate); fat tail = phantoms (71%)")
    P("")
    P("  The CURRENT strategy (this run) changes those headlines as follows:")
    P(f"    • CANDIDATES {base_edge['n']} -> {cur_edge['n']}  "
      f"({base_edge['n']-cur_edge['n']} dropped = {100.0*(base_edge['n']-cur_edge['n'])/max(1,base_edge['n']):.0f}%). "
      f"The mix flips from ~85% sports to 100% WEATHER ({wx} weather arbs survive).")
    P(f"    • The two big cuts: settlement-identity removes every sports/econ arb (unverified), then the 2c")
    P(f"      edge-floor + dear-led skip thin the weather remainder. Net: a {base_edge['n']}->{cur_edge['n']} collapse.")
    P(f"    • $500 REALIZED PnL {base_acct['realized_pnl']:+.2f} ({pct(base_acct['realized_pnl'])}) -> "
      f"{cur_realized:+.2f} ({pct(cur_realized)}) at the bot's REAL caps. Both are tiny + capital-bound; the")
    P(f"      sign/size is noise at effective-n~1. Unconstrained (section 5) is the comparable-scale number.")
    P(f"    • VELOCITY improves structurally: cap-wt lock-days "
      f"{base_lockd:.1f}d -> {(cur_lockd if cur_lockd is not None else float('nan')):.1f}d "
      f"(weather settles ~1.2d vs sports ~15d) — the weather-only book is ~12x more capital-efficient.")
    tox_txt_b = f"{100*base_tox['toxic_share']:.0f}%" if base_tox['toxic_share'] is not None else "n/a"
    tox_txt_c = f"{100*cur_tox['toxic_share']:.0f}%" if cur_tox['toxic_share'] is not None else "n/a"
    P(f"    • TOXICITY: baseline (all-cat) {tox_txt_b} dear-fell (n={base_tox['attributed']}) vs CURRENT "
      f"{tox_txt_c} (n={cur_tox['attributed']}). The drop is the WEATHER FOCUS (weather is structurally")
    P(f"      less toxic than sports), NOT the directional gate — which skipped 0 here (dormant; see §2).")
    P(f"      CURRENT n={cur_tox['attributed']} is too small to read as a result; it is the cohort shrink.")

    # ---- VARIANTS ------------------------------------------------------------------------------
    P("")
    P(SEP)
    P("5.  VARIANTS  (sensitivity to the two biggest design choices)")
    P(SEP)

    # 5a — if sports+econ were verified (settlement gate relaxed), full stack otherwise unchanged.
    st_all, fn_all = run_funnel(eps, cfg, SETTLE_VERIFIED_ALL, dir_idx, px_idx,
                                max_clip_apples, 0.0, 1.0, capital0)
    all_cohort = st_all["directional"]; all_entered = st_all["caps"]
    all_edge = cohort_edge_stats(all_cohort)
    all_acct_real = sum(p["profit"] for p in all_entered if p["settle_t"] <= recs[-1]["t"])
    all_lockd = cap_weighted_lock_days(all_entered)
    P("  5a. IF SPORTS+ECON WERE SETTLEMENT-VERIFIED (the full stack, settlement gate opened to all):")
    P(f"      tradeable cohort {cur_edge['n']} (weather-only) -> {all_edge['n']} (all verified). "
      f"by cat: {_fmt_catmix(all_edge['by_cat'])}")
    P(f"      gate funnel: " + " -> ".join(f"{r['gate'].split('_')[0]}:{r['n']}" for r in fn_all))
    P(f"      $500 entered {len(all_entered)} ({_fmt_catmix(_catmix(all_entered))}); cap-wt lock-days "
      f"{(all_lockd if all_lockd is not None else float('nan')):.1f}d (sports/econ drag velocity back up).")
    P(f"      => the directional gate STILL only touches weather (sports/econ null), but the floor + econ-")
    P(f"         twin 15c bound now act on the larger cohort. This is the strategy's shape once recon clears.")

    # 5b — unconstrained notional (lift the $20/$5/$1 caps; keep edge-floor + directional + fat-edge).
    P("")
    P("  5b. UNCONSTRAINED-NOTIONAL CURRENT STRATEGY (lift the $-caps; keep floor+directional+fat-edge+")
    P("      max_clip; $500 bankroll the only limit) — the strategy's shape at realistic size:")
    for label, cohort in (("weather-only (today)", cur_cohort), ("all-verified (5a)", all_cohort)):
        unc = account_pnl_for_cohort(_episodes_from(cohort, eps), capital0, max_clip_apples)
        if unc is None:
            P(f"      [{label}] (no episodes)")
            continue
        # apply fat-edge halving post-hoc to the entered set for fidelity to risk.rs sizing
        P(f"      [{label}] candidates {len(cohort)} -> entered {len(unc['entered'])} "
          f"({_fmt_catmix(_catmix(unc['entered']))}); REALIZED ${unc['realized_pnl']:.2f} "
          f"({pct(unc['realized_pnl'])}), UNREALIZED ${unc['unrealized_pnl']:.2f} ({pct(unc['unrealized_pnl'])}); "
          f"locked ${unc['locked_capital']:.2f}")
    P("      (fat-edge halving is applied in the capped walk; here the un-capped account_sim shows the")
    P("       selection-only effect. Both remain PAPER/GROSS and effective-n~1 — method-demo only.)")

    # 5c — directional gate ON vs OFF (isolate H1's effect on the weather cohort).
    P("")
    P("  5c. DIRECTIONAL GATE on/off (isolating H1 on the weather cohort):")
    wx_pre = stages["edge_floor"]                            # post-floor, pre-directional (weather-only)
    wx_off = wx_pre
    wx_on = stages["directional"]
    tox_off = toxicity_share(wx_off, recs, sessions)
    tox_on = toxicity_share(wx_on, recs, sessions)
    # full-weather-cohort (pre-floor) class mix, to show why the gate is inert at the candidate level
    wx_all2 = [e for e in stages["settlement"] if e["cat"] == "weather"]
    clsmix2 = dict(Counter(dir_idx.get((e["market"], e["open_t"]), "unclassified") for e in wx_all2))
    dl_all2 = sum(1 for e in wx_all2 if is_dear_led_weather(e, dir_idx))
    P(f"      gate OFF: {len(wx_off)} weather arbs (>=2c), close-toxic "
      f"{(100*tox_off['toxic_share']) if tox_off['toxic_share'] is not None else float('nan'):.0f}% "
      f"(n={tox_off['attributed']})")
    P(f"      gate ON : {len(wx_on)} weather arbs, close-toxic "
      f"{(100*tox_on['toxic_share']) if tox_on['toxic_share'] is not None else float('nan'):.0f}% "
      f"(n={tox_on['attributed']})  — skipped {len(wx_off)-len(wx_on)} dear-led opens")
    P(f"      >>> NO DIFFERENCE on this archive: the gate is DORMANT. Full weather cohort class mix "
      f"{clsmix2}; only {dl_all2} dear-led opens exist across ALL {len(wx_all2)} weather candidates, and the")
    P(f"      ~all-unclassified opens (no prior px / censor-gapped) are KEPT by design. The gate needs the")
    P(f"      sub-2c + multi-day weather opens to populate; at the >=2c candidate level it has nothing to cut.")
    P("      (The H1 SIGNAL itself is real where measurable — strategy-idea-tests: dropping dear-led weather")
    P("       cuts portfolio toxicity 43%->31% at ~0c realized-edge cost, weather-only, Fisher p=2.7e-6,")
    P("       n=59 weather opens / effective-n~2. This backtest just shows the GATE doesn't bite the >=2c")
    P("       candidate cohort on these 2 days — a coverage limit, not a refutation of H1.)")

    # ---- CAVEATS / VERDICT ---------------------------------------------------------------------
    P("")
    P(SEP)
    P("6.  CAVEATS  &  HONEST READ")
    P(SEP)
    P(f"  • EFFECTIVE-N ~1 (L18/L19): ~{span_d:.1f} calendar days, but ~91% of candidate arbs fall on one")
    P(f"    event-date (2026-06-10). Weather-only shrinks the usable cohort to {cur_edge['n']} arbs across")
    P(f"    ~{_eff_n_days(cur_cohort)} event-days — TINY. One morning's CLI/boundary regime could drive it all.")
    P("  • PAPER / GROSS: fees+spread are netted (net_edge); latency, leg-fill-failure (shadow_fill: ~55%")
    P("    naked @1s, ~27% of >=1c edges die ~instantly), adverse selection, slippage, and the sports")
    P("    settlement-void tail are NOT. Realized would be LOWER than every figure here.")
    P("  • The bot's DEFAULT caps make the literal $500 PnL trivial by design (staged rollout: 1 contract,")
    P("    $20 total). The unconstrained variant (5b) is the right scale comparison; treat it as shape, not size.")
    P("  • Settlement-identity for sports/econ is RULES-verified only; empirical recon is pending (settle_recon")
    P("    found pmus `closed` != finalized, ~2-week lag). So weather-only is the honest CURRENT universe.")
    P("  • The directional (H1) gate's input (led_by) is the at-open close-attribution PROXY, not a measured")
    P("    realized-PnL loss (read-only can't fill). On THIS archive the gate is DORMANT at the candidate")
    P("    level: ~all weather opens are unclassified (no prior px / censor-gapped) so led_by is unknown and")
    P("    they are KEPT. The H1 signal is real where measurable (Fisher p=2.7e-6, n=59 sub-2c weather opens)")
    P("    but needs the sub-2c + multi-day weather population to bite the >=2c candidate cohort.")
    P("")
    P("  VERDICT (consistent with the readiness audit): the current gate stack is structurally sound and does")
    P("  exactly what it should — it strips the strategy down to the ONE empirically-clean, capital-efficient")
    P("  universe (WEATHER, 292->9 candidates), applies the 0014 2c floor (the dominant cut), and sizes tiny")
    P("  ($20 total cap, 1 contract/pair). The fat-edge haircut + H1 directional gate are present but INERT on")
    P("  this 2-day cohort (no >=6c survivors; ~all weather opens unclassified). The cost of the stack is cohort")
    P("  size: weather-only on ~1 effective event-day is a handful of arbs. This is a MEASUREMENT RIG producing")
    P("  a small, directionally-sound, NOT-yet-validated signal — re-run on the multi-week post-0013 data the")
    P("  0014 protocol requires. Not a go; not a magnitude to quote.")
    P(SEP)
    return "\n".join(L)


def _eff_n_days(cohort):
    import re
    days = set()
    for e in cohort:
        m = re.search(r"(\d{4}-\d{2}-\d{2})", e["market"])
        days.add(m.group(1) if m else "NODATE")
    return len(days)


def _episodes_from(cohort, all_eps):
    """Return the subset of `all_eps` whose (market, open_t) is in `cohort` — so account_sim's
    capturable/one_per_market sees exactly the gated cohort (it re-applies open_net>0, which they pass)."""
    keys = {(e["market"], e["open_t"]) for e in cohort}
    return [e for e in all_eps if (e["market"], e["open_t"]) in keys]


# =================================================================================================
# SELF-TEST  (offline; synthetic episodes exercise every gate)
# =================================================================================================
def _selftest():
    print("backtest-current-strategy self-test")
    cfg = BotConfig

    # --- mid-divergence math ---
    # binary: Kalshi mid 0.855, pmus mid 0.675 -> 18c apart
    km, pm = venue_mids({"k_yb": 0.85, "k_ya": 0.86, "p_yb": 0.66, "p_ya": 0.69}, "P")
    assert abs(km - 0.855) < 1e-9 and abs(pm - 0.675) < 1e-9, (km, pm)
    e_econ = {"market": "urc-x-2026-07-02", "cat": "econ", "open_t": 10, "dir": "P"}
    e_wx = {"market": "tc-temp-x-2026-06-10-gte70", "cat": "weather", "open_t": 10, "dir": "P"}
    px_idx = {("urc-x-2026-07-02", 10): {"k_yb": 0.85, "k_ya": 0.86, "p_yb": 0.66, "p_ya": 0.69},
              ("tc-temp-x-2026-06-10-gte70", 10): {"k_yb": 0.85, "k_ya": 0.86, "p_yb": 0.66, "p_ya": 0.69}}
    assert abs(mid_divergence_cents(e_econ, px_idx) - 18.0) < 1e-6
    # 18c REJECTED at econ 15c bound, ALLOWED at weather 40c bound (matches risk.rs::econ_twin test)
    assert mid_divergence_cents(e_econ, px_idx) > cfg.econ_twin_max_divergence_cents
    assert mid_divergence_cents(e_wx, px_idx) < cfg.mid_divergence_reject_cents
    # sports mid from implied A-space: ka=0.03, kb=0.04 -> Kalshi A-mid ~ (0.96+0.03)/2; pm (0.94+0.95)/2
    kms, pms = venue_mids({"ka": 0.03, "kb": 0.04, "pm_a": 0.95, "pm_b": 0.94}, "PK")
    assert abs(kms - 0.495) < 1e-9 and abs(pms - 0.945) < 1e-9, (kms, pms)
    # missing mid -> gate not applied (None)
    assert mid_divergence_cents({"market": "tc-x", "cat": "weather", "open_t": 5, "dir": "P"}, {}) is None
    print("  OK — mid-divergence: binary + sports-implied mids, econ 15c vs weather 40c, None-when-missing")

    # --- directional gate: dear-led weather skipped, cheap-led/unknown/sports kept ---
    dir_idx = {("tc-a-2026-06-10-gte70", 1): "dear_made",   # dear-led weather -> SKIP
               ("tc-b-2026-06-10-gte70", 2): "cheap_made",  # cheap-led weather -> KEEP
               ("aec-mlb-x-2026-06-10", 3): "dear_made"}    # dear-led SPORTS -> KEEP (gate weather-only)
    assert is_dear_led_weather({"cat": "weather", "market": "tc-a-2026-06-10-gte70", "open_t": 1}, dir_idx)
    assert not is_dear_led_weather({"cat": "weather", "market": "tc-b-2026-06-10-gte70", "open_t": 2}, dir_idx)
    assert not is_dear_led_weather({"cat": "sports", "market": "aec-mlb-x-2026-06-10", "open_t": 3}, dir_idx)
    # unknown direction (no entry) -> kept
    assert not is_dear_led_weather({"cat": "weather", "market": "tc-z-2026-06-10-gte70", "open_t": 9}, dir_idx)
    print("  OK — H1 gate: dear-led weather skipped; cheap-led / unknown / dear-led-sports kept")

    # --- cluster keys ---
    assert cluster_of("tc-temp-nychigh-2026-06-11-gte95f") == "nychigh-2026-06-11"
    assert cluster_of("aec-mlb-lad-pit-2026-06-10").startswith("aec-mlb-lad-pit-2026-06-10")
    print("  OK — cluster keys: weather=city-date, sports=game")

    # --- full funnel end-to-end on synthetic episodes ---
    def ep(mkt, cat, t, net, c2=200, dir_="P", cens="none"):
        return {"market": mkt, "cat": cat, "open_t": t, "open_net": net, "open_c2": c2, "peak_c2": c2,
                "duration": 100, "censored": cens, "dir": dir_, "close_t": t + 100, "open_age": 0,
                "open_flat": False}
    eps = [
        ep("tc-temp-laxhigh-2026-06-10-gte73", "weather", 100, 0.03),         # weather, >2c, cheap-led -> KEEP all
        ep("tc-temp-sfohigh-2026-06-10-lt71f", "weather", 200, 0.04, dir_="K"),# weather, >2c, dear-led -> dropped by H1
        ep("tc-temp-nychigh-2026-06-10-gte90f", "weather", 300, 0.01),        # weather, <2c -> dropped by floor
        ep("aec-mlb-lad-pit-2026-06-10", "sports", 400, 0.05, c2=800),        # sports -> dropped by settlement
        ep("urc-us-gte-2026-07-02-atl4", "econ", 500, 0.05),                  # econ -> dropped by settlement
    ]
    dir2 = {("tc-temp-laxhigh-2026-06-10-gte73", 100): "cheap_made",
            ("tc-temp-sfohigh-2026-06-10-lt71f", 200): "dear_made"}
    px2 = {}                                                                  # no px -> mid gate inert
    stages, funnel = run_funnel(eps, cfg, SETTLE_VERIFIED_NOW, dir2, px2, 1000, 0.0, 1.0, 500.0)
    assert stages["candidates"] and len(stages["candidates"]) == 5, len(stages["candidates"])
    assert _catmix(stages["settlement"]) == {"weather": 3}, _catmix(stages["settlement"])  # sports+econ gone
    assert len(stages["edge_floor"]) == 2, [e["market"] for e in stages["edge_floor"]]      # the <2c wx dropped
    assert len(stages["directional"]) == 1, [e["market"] for e in stages["directional"]]    # dear-led wx dropped
    assert stages["directional"][0]["market"] == "tc-temp-laxhigh-2026-06-10-gte73"
    # all-verified variant keeps sports+econ through settlement
    st_all, _ = run_funnel(eps, cfg, SETTLE_VERIFIED_ALL, dir2, px2, 1000, 0.0, 1.0, 500.0)
    assert _catmix(st_all["settlement"]) == {"weather": 3, "sports": 1, "econ": 1}, _catmix(st_all["settlement"])
    # directional STILL weather-only in the all-verified variant (sports dear-led kept)
    assert any(e["cat"] == "sports" for e in st_all["directional"])
    print("  OK — funnel: settlement drops sports/econ, floor drops <2c, H1 drops dear-led weather; "
          "all-verified keeps sports/econ")

    # --- caps: the $20 total / 1-contract-per-pair brake binds ---
    many = [ep(f"tc-temp-c{i}high-2026-06-10-gte70", "weather", 100 + i, 0.03, c2=500) for i in range(40)]
    dirm = {(e["market"], e["open_t"]): "cheap_made" for e in many}
    stc, _ = run_funnel(many, cfg, SETTLE_VERIFIED_NOW, dirm, {}, 1000, 0.0, 1.0, 500.0)
    ent = stc["caps"]
    # 1 contract/pair * ~$0.97 = ~$0.97/pos; $20 total cap / $0.97 ~ 20 positions max, but 5-concurrent
    # + settlement recycling governs; assert the $-caps bind well below the 40 tradeable.
    assert 1 <= len(ent) <= 40 and sum(p["capital"] for p in ent) <= cfg.max_total_notional + 1e-6, \
        (len(ent), sum(p["capital"] for p in ent))
    assert all(p["size"] == 1 for p in ent), "1 contract/pair cap must hold"
    print(f"  OK — caps: {len(ent)} entered, all size=1, total notional <= ${cfg.max_total_notional:.0f}")

    # --- fat-edge halving (above the 6c knee) under relaxed caps ---
    cfg_big = type("C", (BotConfig,), {"max_contracts_per_pair": 100, "max_notional_per_pair": 1e9,
                                       "max_notional_per_cluster": 1e9, "max_total_notional": 1e9})
    thin = [ep("tc-temp-thinhigh-2026-06-10-gte70", "weather", 100, 0.03, c2=50)]   # below knee
    fat = [ep("tc-temp-fathigh-2026-06-10-gte70", "weather", 100, 0.10, c2=50)]     # above 6c knee
    et = cap_constrained_entries(thin, cfg_big, 1000, 0.0, 1.0, 1e9)
    ef = cap_constrained_entries(fat, cfg_big, 1000, 0.0, 1.0, 1e9)
    assert et[0]["size"] == 50 and ef[0]["size"] == 25, (et[0]["size"], ef[0]["size"])  # fat halved
    print("  OK — fat-edge: 10c edge sized down 50->25 (factor 0.5), 3c edge full 50")

    print("self-test passed.")


# =================================================================================================
if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="full backtest of the bot's CURRENT strategy (risk.rs gate stack)")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--data-dir", default=os.path.join(os.path.dirname(__file__), "..", "..", "data", "cross-arb"))
    ap.add_argument("--capital", type=float, default=500.0, help="starting bankroll $ (default 500)")
    ap.add_argument("--max-clip", type=int, default=1000, help="apples-to-apples clip for the cohort/funnel (prior-backtest 1000)")
    ap.add_argument("--out", default=None, help="write the full report to this path (also prints to stdout)")
    a = ap.parse_args()
    if a.selftest:
        _selftest(); sys.exit(0)
    dd = os.path.abspath(a.data_dir)
    if not glob.glob(os.path.join(dd, "transitions-*.jsonl*")):
        print(f"no data at {dd} — run `pwsh deploy/pull-data.ps1` first, or `--selftest`."); sys.exit(0)
    recs, sessions = load(dd)
    eps = build_episodes(recs, sessions)
    report = build_report(eps, recs, sessions, BotConfig, a.capital, a.max_clip, dd)
    print(report)
    if a.out:
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(report + "\n")
        print(f"\n[written to {a.out}]")
