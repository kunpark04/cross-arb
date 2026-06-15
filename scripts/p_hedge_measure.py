"""scripts/p_hedge_measure.py — MEASURE p_hedge, the maker-mode go/no-go number, from the WAVE-2 logs.

  p_hedge = P( a profitable + available pmus taker hedge exists | a resting Kalshi-weather MAKER just filled )

This converts maker_feasibility.py's transition-SAMPLED bounds into a MEASUREMENT using the higher-fidelity
WAVE-2 logs the monitor has collected since 2026-06-11:
  - trades-<date>.jsonl : REAL Kalshi trade prints — true fill time `vt`, `yes_c`, `qty`, `taker` aggressor.
  - ladders-<date>.jsonl: both-venue top-5 depth `{pb,pa,kb,ka}` (k:'tr' transitions + 'hb' 300 s heartbeats),
                          pre-joined by the same colisted `market` key.

WHY it matters (the 2026-06-15 risk verdict, NOT_VIABLE): the maker mode rests the Kalshi leg ($0 maker fee)
so the Kalshi leg fills FIRST, then reaches for the pmus taker hedge. The advertised +0.14–0.44c/attempt
priced only the ~2c hedge SLIPPAGE on fills that DID hedge — it never priced the hedge-MISS branch. The real
maker EV is
  EV/fill = p_hedge * E[net | hedged]  +  (1 - p_hedge) * E[recovery_cost | naked]
and the whole decision turns on p_hedge. The skeptic argued p_hedge is ~25–40% (so EV ~ -0.95c, wrong sign);
this script measures it from the logs instead of assuming it. The fill detection uses REAL trades (not the
transition-sampled crossing bound) and the hedge is priced from the REAL pmus ladder AT the fill time `vt`
(so adverse selection — the pmus book moving away on the same flow that filled the maker — is captured).

METHOD (per weather market):
  1. PLACEMENT (anchor): walk the ladder time series; a +EV Kalshi-rest maker OPENS when, resting the Kalshi
     leg (dir P = YES ASK @ k_ya / the NO leg; dir K = YES BID @ k_yb / the YES leg) and taker-hedging on
     pmus, the net clears the maker floor. Anchor at the OPEN of each episode (first snapshot the +EV rest
     appears for that dir), recording the rest level, side, dir, queue_ahead (Kalshi ladder vol at the level),
     and the placement-time hedge state.
  2. FILL (real, queue-aware): scan trades AFTER placement for Kalshi prints on our side at our level; our
     1-contract order fills when cumulative same-side taker qty at the level >= queue_ahead + clip ('join'),
     or on the first qualifying print ('improve' = jump the queue, queue_ahead=0); a print THROUGH the level
     is a definite fill. The print's `vt` is the true fill time.
  3. HEDGE-AT-FILL: look up the ladder nearest `vt`; price the pmus taker hedge there (reusing the VERIFIED
     maker_feasibility.hedge_eval). HEDGED iff net > floor AND the pmus hedge level has >= clip volume; else NAKED.
  4. p_hedge = hedged / fills; EV/fill = mean[ hedged ? net : recovery_cost ]; recovery = the adverse Kalshi
     re-cross to flatten the now-naked filled leg, priced from the Kalshi ladder at `vt`.

LIMITATIONS (binding, reported): ladders are ~300 s between transitions, so the hedge-at-fill lookup is within
that window (the pmus book can move between the nearest ladder and the true `vt`); the fill model is a queue
approximation (real queue position is not in the data); ~5 days, weather-only, ATM-dominated. This is the
right MEASUREMENT rig but a PRELIMINARY read until ~weeks accumulate. It is read-only (no orders, no deploys).

  python scripts/p_hedge_measure.py --selftest
  python scripts/p_hedge_measure.py [--data-dir PATH] [--floor-c 0.0] [--clip 1] [--no-dump]
"""
import os, sys, re, gzip, json, glob, bisect, argparse, statistics, collections, datetime, random

try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
# REUSE the verified fee + hedge math (single source of truth; do not re-derive — L15).
from maker_feasibility import pmus_fee_c, kalshi_fee_c, hedge_eval, cents

DATA_DIR = os.path.join(HERE, "..", "..", "data", "cross-arb")
WEATHER_PFX = "tc-"
TICK_C = 1


# ============================================================================================
# LOAD
# ============================================================================================
def _open_any(path):
    return gzip.open(path, "rt", encoding="utf-8") if path.endswith(".gz") else open(path, encoding="utf-8")

def _chosen(data_dir, prefix):
    """At-most-one file per event date (raw beats .gz — same rule as analyze_persistence.load)."""
    chosen = {}
    for path in glob.glob(os.path.join(data_dir, f"{prefix}-*.jsonl.gz")) + \
                glob.glob(os.path.join(data_dir, f"{prefix}-*.jsonl")):
        m = re.search(rf"{prefix}-(.+?)\.jsonl(?:\.gz)?$", os.path.basename(path))
        chosen[m.group(1) if m else path] = path
    return sorted(chosen.values())

def parse_vt(vt):
    """ISO8601 (e.g. '2026-06-14T14:00:29.199635Z') -> epoch seconds (UTC). Robust to 3.10 (no 'Z' support)."""
    s = vt.replace("Z", "+00:00")
    try:
        return datetime.datetime.fromisoformat(s).timestamp()
    except ValueError:
        s2 = re.sub(r"\.\d+", "", vt).replace("Z", "")
        return datetime.datetime.strptime(s2, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=datetime.timezone.utc).timestamp()

def load_ladders(data_dir):
    """{market -> [(t, rec), ...] sorted by t} for weather markets; rec carries pb/pa/kb/ka."""
    by_mkt = collections.defaultdict(list)
    for path in _chosen(data_dir, "ladders"):
        with _open_any(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                r = json.loads(line)
                m = r.get("market", "")
                if m.startswith(WEATHER_PFX):
                    by_mkt[m].append((r["t"], r))
    for m in by_mkt:
        by_mkt[m].sort(key=lambda x: x[0])
    return by_mkt

def load_trades(data_dir):
    """{market -> [(vt_epoch, yes_c, qty, taker), ...] sorted by vt} (weather Kalshi prints)."""
    by_mkt = collections.defaultdict(list)
    for path in _chosen(data_dir, "trades"):
        with _open_any(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                r = json.loads(line)
                m = r.get("market", "")
                if not m.startswith(WEATHER_PFX) or r.get("venue") != "k":
                    continue
                try:
                    vt = parse_vt(r["vt"])
                except Exception:
                    continue
                by_mkt[m].append((r["t"], vt, int(r["yes_c"]), float(r.get("qty", 0.0)), r.get("taker")))
    for m in by_mkt:
        by_mkt[m].sort(key=lambda x: (x[0], x[1]))   # CLOCK FIX: order on monitor-recv `t` (the ladders' clock), tiebreak vt
    return by_mkt


# ============================================================================================
# px / depth helpers
# ============================================================================================
def best(levels):
    """(best_price_c, vol_at_best) from a ladder side list [[p,q],...], or (None, 0.0). pmus bids/asks and
    Kalshi bids/asks are each stored best-first by the monitor, but we take the true extreme to be safe:
    a BID side's best = max price, an ASK side's best = min price."""
    if not levels:
        return None, 0.0
    return levels[0][0], float(levels[0][1])

def px_of(rec):
    """A ladder rec -> the {p_yb,p_ya,k_yb,k_ya} touch dict hedge_eval consumes (dollars), + depths (cents->$)."""
    pb_p, _ = best(rec.get("pb"));  pa_p, _ = best(rec.get("pa"))
    kb_p, _ = best(rec.get("kb"));  ka_p, _ = best(rec.get("ka"))
    return {"p_yb": None if pb_p is None else pb_p / 100, "p_ya": None if pa_p is None else pa_p / 100,
            "k_yb": None if kb_p is None else kb_p / 100, "k_ya": None if ka_p is None else ka_p / 100}

def hedge_vol(rec, direction):
    """Volume available to the pmus taker hedge: dir P hedges by BUYING pmus YES -> pmus ask vol; dir K hedges
    by buying pmus NO = SELLING pmus YES -> pmus bid vol."""
    _, v = best(rec.get("pa") if direction == "P" else rec.get("pb"))
    return v


# ============================================================================================
# PLACEMENT — +EV Kalshi-rest maker OPENs
# ============================================================================================
def maker_leg(direction):
    """The Kalshi resting leg of `direction`: dir P -> YES ASK (the NO leg, level k_ya); dir K -> YES BID."""
    return ("ask", "k_ya") if direction == "P" else ("bid", "k_yb")

def placement_net_c(direction, px, floor_c):
    """Net cents/contract-pair of resting the Kalshi leg at its touch + taker-hedging pmus AT THIS snapshot
    (no slip — the placement-time edge). None if either touch is unquoted. Reuses the verified hedge_eval."""
    side, lvl_key = maker_leg(direction)
    level = cents(px.get(lvl_key))
    if level is None or px.get("p_yb") is None or px.get("p_ya") is None:
        return None
    h = hedge_eval(direction, "K", side, level, px, px)
    return None if h is None or h.get("net_c") is None else h["net_c"]

def placements(ladders, floor_c):
    """Per market, anchor at each dir's OPEN (first ladder snapshot its +EV Kalshi-rest maker appears).
    Yields dicts: market, t0, dir, side, level_c, queue_ahead, place_net_c, px0 (the placement touch)."""
    out = []
    for m, obs in ladders.items():
        in_ep = {"P": False, "K": False}
        for t0, rec in obs:
            px = px_of(rec)
            for d in ("P", "K"):
                net = placement_net_c(d, px, floor_c)
                live = net is not None and net > floor_c
                if live and not in_ep[d]:
                    side, lvl_key = maker_leg(d)
                    level = cents(px.get(lvl_key))
                    qa_levels = rec.get("ka") if side == "ask" else rec.get("kb")
                    queue_ahead = next((float(q) for p, q in (qa_levels or []) if p == level), 0.0)
                    out.append({"market": m, "t0": t0, "dir": d, "side": side, "level_c": level,
                                "queue_ahead": queue_ahead, "place_net_c": round(net, 3), "px0": px})
                in_ep[d] = live
    return out


# ============================================================================================
# FILL — queue-aware, from REAL trades
# ============================================================================================
def detect_fill(anchor, trades_m, mode, clip):
    """Scan trades after the placement for a fill of the resting Kalshi maker. Our side's takers:
      dir P (rest YES ASK @ L): a YES taker (taker=='yes') at yes_c==L consumes the ask queue; yes_c>L = the
                                 book traded THROUGH L -> definite fill.
      dir K (rest YES BID @ L): a NO taker (taker=='no') at yes_c==L consumes the bid queue; yes_c<L = through.
    'join' fills when cumulative same-level qty >= queue_ahead + clip; 'improve' (jump to front) when >= clip.
    A through-print is an immediate definite fill. Time ordering + the window are on the monitor-recv clock `t`
    (consistent with the anchor t0 and the ladders' clock — the CLOCK FIX). Returns (fill_t, fill_vt) or None."""
    L, d = anchor["level_c"], anchor["dir"]
    want = "yes" if d == "P" else "no"
    need = (anchor["queue_ahead"] if mode == "join" else 0.0) + clip
    cum = 0.0
    i = bisect.bisect_right([t for t, *_ in trades_m], anchor["t0"])
    for t, vt, yes_c, qty, taker in trades_m[i:]:
        if taker != want:
            continue
        through = (yes_c > L) if d == "P" else (yes_c < L)
        if through:
            return t, vt                                # book traded beyond our level -> we filled
        if yes_c == L:
            cum += qty
            if cum >= need:
                return t, vt
    return None


# ============================================================================================
# HEDGE-AT-FILL + recovery
# ============================================================================================
def ladder_at(obs, t, max_gap_s):
    """The ladder rec nearest to time t with |t - t_rec| <= max_gap_s, else None (no fresh book at the fill)."""
    ts = [x[0] for x in obs]
    j = bisect.bisect_left(ts, t)
    best_rec, best_dt = None, None
    for k in (j - 1, j):
        if 0 <= k < len(obs):
            dt = abs(obs[k][0] - t)
            if best_dt is None or dt < best_dt:
                best_dt, best_rec = dt, obs[k][1]
    return best_rec if best_dt is not None and best_dt <= max_gap_s else None

def evaluate_fill(anchor, rec_fill, floor_c, clip):
    """At the fill ladder, is the pmus hedge profitable+available? Returns dict(hedged, net_c, recovery_c)."""
    px_f = px_of(rec_fill)
    d, side, L = anchor["dir"], anchor["side"], anchor["level_c"]
    h = hedge_eval(d, "K", side, L, anchor["px0"], px_f)
    net = None if h is None else h.get("net_c")
    vol = hedge_vol(rec_fill, d)
    hedged = net is not None and net > floor_c and vol >= clip
    # NAKED recovery: flatten the filled Kalshi leg by re-crossing at the fill ladder + a Kalshi taker fee.
    # dir P: we sold YES @ L (hold NO); buy YES back at the Kalshi ask k_ya. dir K: we bought YES @ L; sell at k_yb.
    rec_c = None
    if d == "P":
        ka = cents(px_f.get("k_ya"))
        if ka is not None:
            rec_c = -((ka - L) + kalshi_fee_c(ka / 100, taker=True))
    else:
        kb = cents(px_f.get("k_yb"))
        if kb is not None:
            rec_c = -((L - kb) + kalshi_fee_c(kb / 100, taker=True))
    return {"hedged": hedged, "net_c": net, "recovery_c": None if rec_c is None else round(rec_c, 3)}


# ============================================================================================
# MEASURE
# ============================================================================================
MAX_GAP_S = 300                   # hedge-at-fill ladder must be within this of the true vt (heartbeat cadence)

def measure(ladders, trades, floor_c, clip, mode):
    fills, p_hedge_rows = [], []
    anchors = placements(ladders, floor_c)
    unfilled = 0
    no_ladder = 0
    for a in anchors:
        tr = trades.get(a["market"])
        if not tr:
            continue
        res = detect_fill(a, tr, mode, clip)
        if res is None:
            unfilled += 1
            continue
        fill_t, fill_vt = res
        # CLOCK FIX (stats review 2026-06-15): look the hedge ladder up on the monitor-recv `t` (the ladders'
        # clock), NOT the venue `vt` — `t`-`vt` is a structured ~150s skew that priced the hedge against the
        # PRE-fill pmus book (look-ahead inflating p_hedge). `vt` is kept for latency reporting only.
        rec_fill = ladder_at(ladders[a["market"]], fill_t, MAX_GAP_S)
        if rec_fill is None:
            no_ladder += 1
            continue
        ev = evaluate_fill(a, rec_fill, floor_c, clip)
        row = {**a, "t_fill": fill_t, "vt": fill_vt, "dt_s": round(fill_t - a["t0"], 1), **ev}
        fills.append(row)
        p_hedge_rows.append(row)
    return {"n_anchors": len(anchors), "n_unfilled": unfilled, "n_no_ladder": no_ladder,
            "fills": fills}


def _pct(xs, q):
    if not xs:
        return float("nan")
    xs = sorted(xs); k = (len(xs) - 1) * q / 100.0; f = int(k)
    return xs[f] if f == len(xs) - 1 else xs[f] + (xs[f + 1] - xs[f]) * (k - f)

def _ev_term(f):
    return f["net_c"] if (f["hedged"] and f["net_c"] is not None) else (f["recovery_c"] or 0.0)

def breakeven_p_hedge(fills):
    """The p_hedge above which EV/fill > 0, given THIS sample's E[net|hedged] & E[cost|naked] (means):
    p* = -E[cost|naked] / (E[net|hedged] - E[cost|naked]). None if a side is empty."""
    net = [f["net_c"] for f in fills if f["hedged"] and f["net_c"] is not None]
    cost = [f["recovery_c"] for f in fills if not f["hedged"] and f["recovery_c"] is not None]
    if not net or not cost:
        return None
    en, ec = statistics.mean(net), statistics.mean(cost)
    return None if en == ec else -ec / (en - ec)

def cluster_key(market):
    m = re.match(r"tc-temp-(.+?)-(\d{4}-\d{2}-\d{2})", market)
    return (m.group(1), m.group(2)) if m else (market, "")

def cluster_bootstrap(fills, n_boot=2000, seed=12345):
    """(city,date)-clustered bootstrap CI for p_hedge + EV/fill — resample CLUSTERS with replacement so the CI
    reflects the ~handful of independent market-days, not the autocorrelated within-day anchors (the honest
    denominator). Returns the 5-95% CI on each + P(EV>0)."""
    by_cl = collections.defaultdict(list)
    for f in fills:
        by_cl[cluster_key(f["market"])].append(f)
    clusters = list(by_cl.values())
    nc = len(clusters)
    if nc < 2:
        return None
    rng = random.Random(seed)
    phs, evs = [], []
    for _ in range(n_boot):
        samp = [f for _ in range(nc) for f in clusters[rng.randrange(nc)]]
        if not samp:
            continue
        phs.append(sum(1 for f in samp if f["hedged"]) / len(samp))
        evs.append(statistics.mean([_ev_term(f) for f in samp]))
    return {"n_clusters": nc, "p_hedge_ci": (round(_pct(phs, 5), 3), round(_pct(phs, 95), 3)),
            "ev_ci_c": (round(_pct(evs, 5), 2), round(_pct(evs, 95), 2)),
            "p_pos_ev": round(sum(1 for e in evs if e > 0) / len(evs), 3) if evs else None}

def report(args):
    P = print
    ladders = load_ladders(args.data_dir)
    trades = load_trades(args.data_dir)
    P("=" * 96)
    P("p_hedge MEASUREMENT — maker-mode go/no-go (rest Kalshi weather, taker-hedge pmus)")
    P("=" * 96)
    P(f"weather markets: ladders={len(ladders)} trades={len(trades)} | floor={args.floor_c}c clip={args.clip}")
    P("p_hedge = P(profitable+available pmus hedge | Kalshi maker filled); EV/fill = p_h*net + (1-p_h)*recovery.\n")
    out = {}
    for mode in ("join", "improve"):
        r = measure(ladders, trades, args.floor_c, args.clip, mode)
        fills = r["fills"]
        n = len(fills)
        hedged = [f for f in fills if f["hedged"]]
        p_hedge = len(hedged) / n if n else float("nan")
        net_h = [f["net_c"] for f in hedged if f["net_c"] is not None]
        rec_n = [f["recovery_c"] for f in fills if not f["hedged"] and f["recovery_c"] is not None]
        ev = None
        if n:
            terms = [(f["net_c"] if f["hedged"] and f["net_c"] is not None else (f["recovery_c"] or 0.0)) for f in fills]
            ev = statistics.mean(terms)
        dts = [f["dt_s"] for f in fills]
        P("-" * 96)
        P(f"MODE={mode}  anchors={r['n_anchors']}  filled={n}  unfilled={r['n_unfilled']}  no-ladder-at-fill={r['n_no_ladder']}")
        P(f"  p_hedge = {100*p_hedge:.1f}%  ({len(hedged)}/{n} fills had a profitable+available pmus hedge)")
        if net_h:
            P(f"  E[net | hedged]   = med {_pct(net_h,50):.2f}c  mean {statistics.mean(net_h):.2f}c  (n={len(net_h)})")
        if rec_n:
            P(f"  E[cost | naked]   = med {_pct(rec_n,50):.2f}c  mean {statistics.mean(rec_n):.2f}c  (n={len(rec_n)})")
        if ev is not None:
            P(f"  EV / FILL         = {ev:+.2f}c  <- {'+EV' if ev > 0 else 'NEGATIVE'} (the decision number)")
        be = breakeven_p_hedge(fills)
        boot = cluster_bootstrap(fills)
        if be is not None:
            verdict = "VIABLE" if (boot and boot["p_hedge_ci"][0] > be) else "NOT VIABLE"
            P(f"  breakeven p_hedge = {100*be:.1f}%  -> measured {100*p_hedge:.1f}% is "
              f"{'ABOVE' if p_hedge > be else 'BELOW'} breakeven  [{verdict}]")
        if boot:
            P(f"  CI (city,date-clustered boot, {boot['n_clusters']} clusters): "
              f"p_hedge [{100*boot['p_hedge_ci'][0]:.0f}%, {100*boot['p_hedge_ci'][1]:.0f}%]  "
              f"EV/fill [{boot['ev_ci_c'][0]:+.2f}c, {boot['ev_ci_c'][1]:+.2f}c]  P(EV>0)={100*boot['p_pos_ev']:.0f}%")
        if dts:
            P(f"  time-to-fill: med {_pct(dts,50):.0f}s  p90 {_pct(dts,90):.0f}s")
        out[mode] = {"anchors": r["n_anchors"], "filled": n, "unfilled": r["n_unfilled"],
                     "no_ladder": r["n_no_ladder"], "p_hedge": round(p_hedge, 4) if n else None,
                     "breakeven_p_hedge": round(be, 4) if be is not None else None,
                     "net_med_c": round(_pct(net_h, 50), 3) if net_h else None,
                     "recovery_med_c": round(_pct(rec_n, 50), 3) if rec_n else None,
                     "ev_per_fill_c": round(ev, 3) if ev is not None else None,
                     "bootstrap": boot}
    P("\n" + "-" * 96)
    P("READ: a HIGH p_hedge (hedge usually available at the fill) -> maker is viable; a LOW p_hedge confirms")
    P("the skeptic — the Kalshi maker fills on the same flow that empties pmus, so it nakeds into a recovery loss.")
    P("Caveats: ~5 days, weather-only, ATM-dominated; hedge-at-fill within the <=300s ladder cadence; the fill")
    P("model is a queue approximation. PRELIMINARY — re-run as weeks accumulate; pre-register the threshold.")
    if not args.no_dump:
        os.makedirs(os.path.join(HERE, "_data"), exist_ok=True)
        dp = os.path.join(HERE, "_data", "p_hedge_measure.json")
        json.dump({"generated_from": "scripts/p_hedge_measure.py", "floor_c": args.floor_c, "clip": args.clip,
                   "n_markets_ladders": len(ladders), "n_markets_trades": len(trades), "by_mode": out},
                  open(dp, "w", encoding="utf-8"), indent=1)
        P(f"\nnumeric dump -> {dp}")


# ============================================================================================
# SELF-TEST (offline; no data dir)
# ============================================================================================
def _selftest():
    print("p_hedge_measure self-test")
    assert abs(parse_vt("2026-06-14T14:00:29.199635Z") - parse_vt("2026-06-14T14:00:29Z")) < 1.0
    # px_of + best
    rec = {"pb": [[40, 12.0]], "pa": [[48, 78.0]], "kb": [[53, 19.0]], "ka": [[54, 1.0]]}
    px = px_of(rec)
    assert px["p_yb"] == 0.40 and px["p_ya"] == 0.48 and px["k_ya"] == 0.54
    assert hedge_vol(rec, "P") == 78.0 and hedge_vol(rec, "K") == 12.0
    # placement: dir P rests Kalshi YES ask @54, hedge buy pmus YES @48 -> gross 6c, minus pmus taker fee ~1.2c
    net = placement_net_c("P", px, 0.0)
    assert net is not None and 4.0 < net < 6.0, net
    # placement anchor: one OPEN per dir-episode
    obs = [(100.0, rec), (160.0, rec)]                      # same +EV rest two snapshots -> ONE anchor (open)
    anc = placements({"m": obs}, 0.0)
    assert sum(1 for a in anc if a["dir"] == "P") == 1, "dir P opens exactly once across a steady episode"
    aP = next(a for a in anc if a["dir"] == "P")
    assert aP["side"] == "ask" and aP["level_c"] == 54 and aP["queue_ahead"] == 1.0
    # FILL: dir P rest YES ask @54. trade tuple = (recv_t, vt, yes_c, qty, taker); detect_fill returns
    # (recv_t, vt) of the fill (recv_t is the ladder/window clock — the CLOCK FIX), or None.
    assert detect_fill(aP, [(150.0, 149.0, 54, 5.0, "yes")], "join", 1) == (150.0, 149.0)  # 5 >= queue(1)+clip(1)
    assert detect_fill(aP, [(150.0, 149.0, 54, 0.5, "yes")], "join", 1) is None    # 0.5 < 1+1 -> unfilled
    assert detect_fill(aP, [(150.0, 149.0, 54, 0.5, "yes")], "improve", 1) is None # improve needs >=clip(1)
    assert detect_fill(aP, [(150.0, 149.0, 55, 1.0, "yes")], "join", 1) == (150.0, 149.0)  # 55>54 = through
    assert detect_fill(aP, [(150.0, 149.0, 53, 9.0, "yes")], "join", 1) is None    # 53<54 not our ask level
    assert detect_fill(aP, [(150.0, 149.0, 54, 9.0, "no")], "join", 1) is None     # wrong taker side
    # dir K rests YES bid; a NO taker at/through the bid fills
    aK = next(a for a in anc if a["dir"] == "K") if any(a["dir"] == "K" for a in anc) else None
    # HEDGE-AT-FILL: hedge still good (pmus ask 48 unchanged) -> hedged
    ev = evaluate_fill(aP, rec, 0.0, 1)
    assert ev["hedged"] and ev["net_c"] > 0, ev
    # adverse: pmus ask jumped to 55 (> our rest 54) -> gross 54-55 <0 -> NOT hedged -> recovery priced
    rec_adv = {"pb": [[40, 12.0]], "pa": [[55, 78.0]], "kb": [[53, 19.0]], "ka": [[58, 9.0]]}
    ev2 = evaluate_fill(aP, rec_adv, 0.0, 1)
    assert not ev2["hedged"] and ev2["recovery_c"] is not None and ev2["recovery_c"] < 0, ev2
    assert abs(ev2["recovery_c"] - round(-((58 - 54) + kalshi_fee_c(0.58, taker=True)), 3)) < 1e-9
    # ladder_at: within gap vs outside
    assert ladder_at(obs, 130.0, 300) is not None and ladder_at(obs, 1000.0, 300) is None
    # thin pmus hedge: vol < clip -> not hedged even if priced +EV
    rec_thin = {"pb": [[40, 12.0]], "pa": [[48, 0.0]], "kb": [[53, 19.0]], "ka": [[54, 1.0]]}
    assert not evaluate_fill(aP, rec_thin, 0.0, 1)["hedged"]
    print("  OK px/placement/fill(join+improve+through)/hedge-at-fill/adverse-recovery/gap/thin-vol")
    print("all self-tests passed")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--data-dir", default=DATA_DIR)
    ap.add_argument("--floor-c", type=float, default=0.0, help="maker net floor cents (default 0 = require >0)")
    ap.add_argument("--clip", type=int, default=1, help="contracts (staged-rollout 1)")
    ap.add_argument("--no-dump", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        _selftest()
    else:
        report(args)
