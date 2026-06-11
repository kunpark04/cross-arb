"""scripts/probe_mlb_postpone.py - todo #7 probe: can a trade layer DETECT an MLB postponement fast
enough to unwind a cross-venue pair BEFORE Kalshi's 2-day reschedule window voids the Kalshi leg?
(research/sports-settlement-verification.md: Kalshi settles a replay only if rescheduled <=2 days, else
voids to "a fair price"; pmus waits <=2 weeks -> a makeup in the 2d-2wk gap = both-legs loss.) READ-ONLY.

MLB statsapi (statsapi.mlb.com) is public, keyless. Modes:
  --selftest        offline: the unwind-trigger classifier on synthetic schedule snapshots
  --scan [--days N] scan schedules back N days for Postponed/Suspended/Cancelled games: which fields
                    flip, reschedule pointers, multi-gamePk poll form. Saves raw samples to _data/.
  --feed PK         one gamePk's live-feed status + /timestamps (is a status-change time exposed?)
  --rules           pull a live KXMLBGAME market's verbatim rules text (the exact "2 days" language)
  --archive         per-venue spread stats from the archived MLB transition records (px epoch),
                    pre-game vs in-play (game times from statsapi)
  --books           LIVE exit-depth spot check: current co-listed MLB pairs' pmus book + both Kalshi
                    orderbooks (bid-side depth = what an unwind sells into) + any lingering past-date
                    pm MLB market (a postponed game's book state, if one exists right now)
"""
import os, sys, re, json, time, glob, gzip, argparse, collections, datetime as dt

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bot"))
from colisted_map import get, KAL                      # proven GET (UA + retries) + Kalshi base

STATS   = "https://statsapi.mlb.com/api/v1"
STATS11 = "https://statsapi.mlb.com/api/v1.1"
PM      = "https://gateway.polymarket.us/v1/markets"
DATA    = os.path.join(os.path.dirname(__file__), "_data")
ARCHIVE = os.path.join(os.path.dirname(__file__), "..", "..", "data", "cross-arb")
POSTPONE_STATES = ("Postponed", "Suspended", "Cancelled")


def _dump(name, obj):
    os.makedirs(DATA, exist_ok=True)
    p = os.path.join(DATA, name)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1, default=str)
    print(f"  [saved scripts/_data/{name}]")


def _fl(v):
    try: return float(v)
    except (TypeError, ValueError): return None


def _days(d0, d1):
    """whole days from ISO date d0 -> d1 (None if unparseable)."""
    try:
        a = dt.date.fromisoformat(str(d0)[:10]); b = dt.date.fromisoformat(str(d1)[:10])
        return (b - a).days
    except Exception:
        return None


# ---------------------------------------------------------------------------------------------
# The DETECTOR core (offline-testable): schedule entry -> compact snapshot -> trigger decision.
# ---------------------------------------------------------------------------------------------
def snap(g):
    """One statsapi schedule game entry -> the compact status snapshot the detector compares."""
    st = g.get("status", {}) or {}
    return {"gamePk": g.get("gamePk"),
            "detailedState": st.get("detailedState"),
            "codedGameState": st.get("codedGameState"),
            "statusCode": st.get("statusCode"),
            "reason": st.get("reason"),
            "officialDate": g.get("officialDate"),
            "gameDate": g.get("gameDate"),
            "rescheduleDate": g.get("rescheduleDate") or g.get("rescheduleGameDate"),
            "resumeDate": g.get("resumeDate") or g.get("resumeGameDate"),
            "rescheduledFrom": g.get("rescheduledFrom") or g.get("rescheduledFromDate")}


def unwind_trigger(prev, cur, window_days=2, event_date=None):
    """(prev, cur) snapshots for ONE tracked gamePk (prev may be None on first sight) ->
    (action, reason); action in {'UNWIND','WATCH',None}.
      UNWIND - the settlement-divergence tail is LIVE: status in {Postponed,Cancelled,Suspended} AND
               the makeup/resume date is unknown or > window_days after the ORIGINAL date (Kalshi
               voids to fair price while pmus settles the replay / last-traded -> legs stop offsetting).
      WATCH  - same status flip but the makeup is INSIDE the window: both venues settle the replay
               (settlement still identical) -> hold, confirm next poll + doubleheader-rebind check.
    FIELD SEMANTICS (verified live 2026-06-10, _data/mlb_postpone_samples.json): on a postponement the
    entry's officialDate MOVES to the makeup date while gameDate KEEPS the original datetime, and
    rescheduleDate appears (all 5 live examples carried it immediately). So the gap must be measured
    from the pair's BOUND event date (pm slug date == Kalshi ticker date == original gameDate) - never
    from officialDate (using officialDate computes makeup-minus-makeup = 0d and misses every UNWIND).
    Idempotent on the current state (a postponed game stays UNWIND until acted on)."""
    cs = (cur or {}).get("detailedState") or ""
    orig = (event_date or str((prev or {}).get("gameDate") or "")[:10]
            or str((cur or {}).get("gameDate") or "")[:10]) or None
    if not any(s in cs for s in POSTPONE_STATES):
        if prev and cur and prev.get("officialDate") and cur.get("officialDate") \
                and prev["officialDate"] != cur["officialDate"]:        # date moved w/o a status flip
            d = _days(orig or prev["officialDate"], cur["officialDate"])
            act = "UNWIND" if d is None or d > window_days else "WATCH"
            return act, f"officialDate moved {prev['officialDate']} -> {cur['officialDate']} ({d}d from original)"
        return None, ""
    if "Cancelled" in cs:
        return "UNWIND", "Cancelled: Kalshi fair-price void vs pmus last-traded (divergent void bases)"
    target = cur.get("resumeDate") if "Suspended" in cs else cur.get("rescheduleDate")
    if not target:
        return "UNWIND", f"{cs}: makeup/resume date UNKNOWN -> assume outside the {window_days}d window"
    d = _days(orig, target)
    if d is None or d > window_days:
        return "UNWIND", f"{cs}: makeup {str(target)[:10]} is {d}d after original (> {window_days}d Kalshi window)"
    return "WATCH", f"{cs}: makeup {str(target)[:10]} within {window_days}d -> both venues settle the replay"


def _selftest():
    # REAL field semantics (live-verified): gameDate keeps the original datetime; officialDate MOVES to
    # the makeup date on a postponement; rescheduleDate appears alongside the status flip.
    base = {"gamePk": 1, "detailedState": "Scheduled",
            "officialDate": "2026-06-10", "gameDate": "2026-06-10T22:40:00Z"}
    def s(**kw): return {**base, **kw}
    # postponed, makeup 5 days out (the 2d-2wk both-legs-loss gap) -> UNWIND
    a, r = unwind_trigger(s(), s(detailedState="Postponed", reason="Rain",
                                 rescheduleDate="2026-06-15T17:10:00Z", officialDate="2026-06-15"))
    assert a == "UNWIND" and "5d" in r, (a, r)
    # the LIVE TB@NYY shape: makeup months out, officialDate already moved -> must be UNWIND
    # (an officialDate-based gap computes makeup-minus-makeup = 0d and would say WATCH - the L3 trap)
    a, r = unwind_trigger(s(), s(detailedState="Postponed", reason="Rain",
                                 rescheduleDate="2026-09-22T17:05:00Z", officialDate="2026-09-22"))
    assert a == "UNWIND" and "104d" in r, (a, r)
    # postponed, makeup next day (classic split-doubleheader makeup) -> inside window -> WATCH
    a, _ = unwind_trigger(s(), s(detailedState="Postponed", rescheduleDate="2026-06-11T17:10:00Z",
                                 officialDate="2026-06-11"))
    assert a == "WATCH"
    # postponed, NO makeup date yet -> unknown -> conservative UNWIND
    a, _ = unwind_trigger(s(), s(detailedState="Postponed"))
    assert a == "UNWIND"
    # suspended, resumes next day -> both venues settle the completed game -> WATCH
    a, _ = unwind_trigger(s(), s(detailedState="Suspended", resumeDate="2026-06-11T17:00:00Z"))
    assert a == "WATCH"
    # cancelled outright -> divergent void bases -> UNWIND
    a, _ = unwind_trigger(s(), s(detailedState="Cancelled: Rain"))
    assert a == "UNWIND"
    # normal life-cycle -> no trigger
    for st in ("Scheduled", "Pre-Game", "Warmup", "In Progress", "Final"):
        assert unwind_trigger(s(), s(detailedState=st))[0] is None, st
    # first sighting ALREADY postponed (prev=None): event_date comes from the BOUND pair (pm slug date)
    a, _ = unwind_trigger(None, s(detailedState="Postponed", rescheduleDate="2026-06-20T17:00:00Z",
                                  officialDate="2026-06-20"), event_date="2026-06-10")
    assert a == "UNWIND"
    # ... and without event_date the postponed entry's own gameDate (original) still catches it
    a, _ = unwind_trigger(None, s(detailedState="Postponed", rescheduleDate="2026-06-20T17:00:00Z",
                                  officialDate="2026-06-20"))
    assert a == "UNWIND"
    # date slid one day without a status flip (statsapi quirk) -> inside window -> WATCH
    a, _ = unwind_trigger(s(), s(officialDate="2026-06-11"))
    assert a == "WATCH"
    # date slid a week -> UNWIND
    a, _ = unwind_trigger(s(), s(officialDate="2026-06-17"))
    assert a == "UNWIND"
    print("OK - unwind_trigger: UNWIND on out-of-window/unknown/cancelled (incl. the live "
          "officialDate-moves shape), WATCH inside window, None on the normal life-cycle")


# ---------------------------------------------------------------------------------------------
# --scan : how do postponements SURFACE in the public schedule API?
# ---------------------------------------------------------------------------------------------
def scan(days):
    today = dt.date.today()
    found, normal_keys, per_date = [], None, collections.Counter()
    print(f"scanning statsapi schedules {today - dt.timedelta(days=days)} .. {today} for "
          f"{'/'.join(POSTPONE_STATES)} ...")
    for i in range(days, -1, -1):
        d = (today - dt.timedelta(days=i)).isoformat()
        sched = get(f"{STATS}/schedule?sportId=1&date={d}")
        games = [g for dd in (sched.get("dates") or []) for g in dd.get("games", [])]
        per_date[d] = len(games)
        for g in games:
            st = (g.get("status", {}) or {}).get("detailedState", "")
            if normal_keys is None and "Final" in st:
                normal_keys = set(g.keys())
            if any(s in st for s in POSTPONE_STATES):
                found.append({"date": d, "raw": g})
        time.sleep(0.15)
    print(f"  {sum(per_date.values())} games scanned across {len(per_date)} dates; "
          f"{len(found)} Postponed/Suspended/Cancelled entries found")
    samples = {"scanned": dict(per_date), "normal_final_keys": sorted(normal_keys or []),
               "postponed_entries": [f["raw"] for f in found], "found_dates": [f["date"] for f in found]}
    for f in found:
        g = f["raw"]; sn = snap(g)
        extra = sorted(set(g.keys()) - (normal_keys or set()))
        act, why = unwind_trigger(None, sn, event_date=f["date"])   # the queried date = the bound event date
        away = (((g.get("teams") or {}).get("away") or {}).get("team") or {}).get("name")
        home = (((g.get("teams") or {}).get("home") or {}).get("team") or {}).get("name")
        print(f"\n  {f['date']}  gamePk={sn['gamePk']}  {away} @ {home}")
        print(f"    status: detailedState={sn['detailedState']!r} codedGameState={sn['codedGameState']!r} "
              f"statusCode={sn['statusCode']!r} reason={sn['reason']!r}")
        print(f"    pointers: rescheduleDate={sn['rescheduleDate']!r} resumeDate={sn['resumeDate']!r} "
              f"rescheduledFrom={sn['rescheduledFrom']!r}")
        print(f"    extra keys vs a normal Final entry: {extra}")
        print(f"    -> trigger: {act}  ({why})")
    # the multi-gamePk poll form (ONE request for every tracked game) + the same pk on its makeup date
    if found:
        pk = snap(found[0]["raw"])["gamePk"]
        multi = get(f"{STATS}/schedule?sportId=1&gamePks={pk}")
        n_entries = sum(len(dd.get("games", [])) for dd in (multi.get("dates") or []))
        dates = [dd.get("date") for dd in (multi.get("dates") or [])]
        print(f"\n  multi-pk poll check: schedule?gamePks={pk} -> {n_entries} entries on dates {dates}")
        print("    (the SAME gamePk appearing on original + makeup dates = the reschedule pointer is "
              "bidirectional; one gamePks=... request polls every tracked game)")
        samples["gamepks_query"] = multi
    else:
        print("\n  (no postponements in range -- rerun with a larger --days)")
    _dump("mlb_postpone_samples.json", samples)
    return found


def feed(pk):
    """Live-feed status + update timecodes for one gamePk: is a status-CHANGE timestamp exposed?"""
    f = get(f"{STATS11}/game/{pk}/feed/live")
    gd, md = f.get("gameData", {}) or {}, f.get("metaData", {}) or {}
    out = {"gamePk": pk, "metaData": md, "status": gd.get("status"), "datetime": gd.get("datetime"),
           "gameInfo": {k: gd.get(k) for k in ("game",) if gd.get(k)}}
    print(f"\n  feed/live gamePk={pk}")
    print(f"    gameData.status   : {json.dumps(gd.get('status'))}")
    print(f"    gameData.datetime : {json.dumps(gd.get('datetime'))}")
    print(f"    metaData.timeStamp: {md.get('timeStamp')!r}  (LAST update timecode)")
    ts = get(f"{STATS11}/game/{pk}/feed/live/timestamps")
    if isinstance(ts, list) and ts:
        out["timestamps"] = {"n": len(ts), "first": ts[0], "last": ts[-1], "tail": ts[-5:]}
        print(f"    /timestamps: {len(ts)} update timecodes, first={ts[0]} last={ts[-1]}")
        print("    -> NO per-field 'status changed at' exists; the LAST timecode of a postponed game's "
              "feed bounds the flip time retrospectively. LIVE detection latency = poll cadence.")
    else:
        out["timestamps"] = ts
        print(f"    /timestamps: {ts!r}")
    _dump(f"mlb_feed_{pk}.json", out)


def rules():
    """Verbatim KXMLBGAME rules text: the exact reschedule-window language the unwind deadline keys on."""
    kd = get(f"{KAL}?series_ticker=KXMLBGAME&status=open&limit=3")
    mkts = kd.get("markets", [])
    if not mkts:
        kd = get(f"{KAL}?series_ticker=KXMLBGAME&limit=3"); mkts = kd.get("markets", [])
    if not mkts:
        print("  no KXMLBGAME market readable right now"); return
    tk = mkts[0].get("ticker")
    full = get(f"{KAL}/{tk}").get("market", {}) or mkts[0]
    txt = {k: full.get(k) for k in ("ticker", "title", "rules_primary", "rules_secondary",
                                    "expiration_time", "settlement_timer_seconds")}
    print(f"\n  KXMLBGAME rules text ({tk}):")
    for k in ("rules_primary", "rules_secondary"):
        if txt.get(k): print(f"    {k}: {txt[k]}")
    for sent in re.split(r"(?<=\.)\s+", str(txt.get("rules_primary", "")) + " " + str(txt.get("rules_secondary", ""))):
        if re.search(r"\b(2|two)\b.{0,20}(day|calendar)|resched|postpon|cancel", sent, re.I):
            print(f"    >> {sent.strip()}")
    _dump("mlb_kxmlbgame_rules.json", txt)


# ---------------------------------------------------------------------------------------------
# --archive : spread context from the logged MLB transitions (px epoch), pre-game vs in-play.
# ---------------------------------------------------------------------------------------------
def _pctl(xs, q):
    if not xs: return None
    xs = sorted(xs); i = (len(xs) - 1) * q
    lo, hi = int(i), min(int(i) + 1, len(xs) - 1)
    return round(xs[lo] + (xs[hi] - xs[lo]) * (i - lo), 4)


def archive():
    files = sorted(glob.glob(os.path.join(ARCHIVE, "transitions-*.jsonl"))
                   + glob.glob(os.path.join(ARCHIVE, "transitions-*.jsonl.gz")))
    teams = {t["id"]: str(t.get("abbreviation", "")).lower()
             for t in get(f"{STATS}/teams?sportId=1").get("teams", [])}
    sched_cache, recs = {}, []
    for p in files:
        op = gzip.open(p, "rt", encoding="utf-8") if p.endswith(".gz") else open(p, encoding="utf-8")
        for ln in op:
            try: r = json.loads(ln)
            except Exception: continue
            if str(r.get("market", "")).startswith("aec-mlb-"): recs.append(r)
        op.close()
    print(f"  {len(recs)} archived MLB transition records "
          f"({sum(1 for r in recs if r.get('px'))} carry px; post-0013 records only carry px)")

    def start_epoch(market):
        m = re.match(r"aec-mlb-([a-z]+)-([a-z]+)-(\d{4}-\d{2}-\d{2})", market)
        if not m: return None
        a, b, d = m.group(1), m.group(2), m.group(3)
        if d not in sched_cache:
            sc = get(f"{STATS}/schedule?sportId=1&date={d}"); time.sleep(0.15)
            byteam = {}
            for dd in sc.get("dates") or []:
                for g in dd.get("games", []):
                    ids = ((g.get("teams") or {}).get("away", {}).get("team", {}).get("id"),
                           (g.get("teams") or {}).get("home", {}).get("team", {}).get("id"))
                    key = frozenset(teams.get(i, "?") for i in ids)
                    ep = None
                    try: ep = dt.datetime.fromisoformat(str(g.get("gameDate")).replace("Z", "+00:00")).timestamp()
                    except Exception: pass
                    byteam[key] = ep
            sched_cache[d] = byteam
        return sched_cache[d].get(frozenset((a, b)))

    cells = collections.defaultdict(lambda: {"pm_spread": [], "k_over": [], "c0": [], "c2": [], "n": 0})
    unmatched = set()
    for r in recs:
        px = r.get("px")
        if not px: continue
        ep = start_epoch(r["market"])
        if ep is None:
            unmatched.add(r["market"]); continue
        phase = "pre-game" if r["t"] < ep else ("in-play(<5h)" if r["t"] < ep + 5 * 3600 else "post(>5h)")
        c = cells[phase]; c["n"] += 1
        if px.get("pm_a") is not None and px.get("pm_b") is not None:
            c["pm_spread"].append(round(px["pm_a"] - px["pm_b"], 4))
        if px.get("ka") is not None and px.get("kb") is not None:
            c["k_over"].append(round(px["ka"] + px["kb"] - 1, 4))     # sum of the two YES asks - 1
        d = r.get("depth") or {}
        if d.get("c0") is not None: c["c0"].append(d["c0"])
        if d.get("c2") is not None: c["c2"].append(d["c2"])
    if unmatched:
        print(f"  ({len(unmatched)} markets had no statsapi schedule match: {sorted(unmatched)[:4]} ...)")
    print("\n  CAVEAT (L18): these px snapshots exist only AT arb transitions (OPEN/CLOSE/FLIP moments),")
    print("  not a continuous book sample - spread CONTEXT, not the unwind quote. Depth c0/c2 is the")
    print("  ENTRY-direction crossable depth, NOT the bid-side exit depth (that is --books, live).")
    for phase, c in sorted(cells.items()):
        print(f"\n  [{phase}]  n={c['n']} px-records")
        for k, lab in (("pm_spread", "pmus YES spread (ask-bid)"), ("k_over", "Kalshi overround (kaskA+kaskB-1)")):
            xs = c[k]
            print(f"    {lab:34}: n={len(xs):4}  p25={_pctl(xs, .25)}  med={_pctl(xs, .5)}  p75={_pctl(xs, .75)}")
        print(f"    entry-dir crossable depth (pairs): c0 med={_pctl(c['c0'], .5)}  c2 med={_pctl(c['c2'], .5)}")


# ---------------------------------------------------------------------------------------------
# --books : LIVE bid-side exit depth on current MLB pairs (+ any lingering past-date pm market).
# ---------------------------------------------------------------------------------------------
def pm_book(slug):
    md = (get(f"{PM}/{slug}/book") or {}).get("marketData", {})
    bids = sorted([(_fl(x["px"]["value"]), _fl(x["qty"])) for x in md.get("bids", []) if x.get("px")],
                  reverse=True)
    offs = sorted([(_fl(x["px"]["value"]), _fl(x["qty"])) for x in md.get("offers", []) if x.get("px")])
    return [b for b in bids if b[0] is not None], [o for o in offs if o[0] is not None], md.get("state")


def k_orderbook(ticker):
    ob = get(f"{KAL}/{ticker}/orderbook").get("orderbook_fp", {})
    yb = sorted([(_fl(p), _fl(s)) for p, s in ob.get("yes_dollars", [])], reverse=True)   # YES bids
    nb = sorted([(_fl(p), _fl(s)) for p, s in ob.get("no_dollars", [])], reverse=True)    # NO bids
    return yb, nb


def _cum(levels, within):
    """(touch_px, touch_qty, cum qty within `within` of touch) for a bid ladder sorted best-first."""
    if not levels: return None, 0, 0
    best = levels[0][0]
    return best, levels[0][1], round(sum(q for p, q in levels if p >= best - within), 1)


def books(max_pairs=3):
    from colisted_map import build_colisted_map
    print("  discovering current co-listed MLB pairs (full catalog pull, ~30s)...")
    colisted, rep = build_colisted_map()
    mlb = [e for e in colisted["sports"] if e["league"] == "mlb"]
    print(f"  {len(mlb)} co-listed MLB pairs live right now")
    out = {"t_utc": dt.datetime.now(dt.timezone.utc).isoformat(), "pairs": []}
    for e in mlb[:max_pairs]:
        print(f"\n  {e['slug']}   ({e['teamA']} vs {e['teamB']})")
        bids, offs, state = pm_book(e["slug"])
        pb, pbq, pcum = _cum(bids, 0.03)
        pa = offs[0][0] if offs else None
        print(f"    pmus  state={state!r}  bid={pb} ask={pa} spread={round(pa - pb, 3) if pb is not None and pa is not None else None}"
              f"  bid@touch={pbq:.0f}  cum_bid<=3c={pcum:.0f}")
        rec = {"pair": e, "pm": {"state": state, "bid": pb, "ask": pa, "bid_touch_qty": pbq,
                                 "bid_cum_3c": pcum, "bids": bids[:8], "offers": offs[:8]}, "kalshi": {}}
        for lab, tk in (("A", e["kalshi_a"]), ("B", e["kalshi_b"])):
            yb, nb = k_orderbook(tk); time.sleep(0.2)
            kb_, kbq, kcum = _cum(yb, 0.03)
            ka_ = round(1 - nb[0][0], 4) if nb else None             # YES ask = 1 - best NO bid
            print(f"    K[{lab}] {tk}: yes_bid={kb_} yes_ask={ka_} "
                  f"spread={round(ka_ - kb_, 3) if kb_ is not None and ka_ is not None else None}"
                  f"  yes_bid@touch={kbq:.0f}  cum_yes_bid<=3c={kcum:.0f}")
            rec["kalshi"][lab] = {"ticker": tk, "yes_bid": kb_, "yes_ask": ka_, "yes_bid_touch_qty": kbq,
                                  "yes_bid_cum_3c": kcum, "yes_bids": yb[:8], "no_bids": nb[:8]}
        out["pairs"].append(rec)
    # any pm MLB market with a PAST event date still open = a postponed/suspended game's market lingering
    from colisted_map import pm_catalog, pmlg
    today = dt.date.today().isoformat()
    allm = pm_catalog()
    stale = [m for m in allm
             if m.get("category") == "sports" and pmlg(m.get("slug")) == "mlb"
             and (re.search(r"(\d{4}-\d{2}-\d{2})", str(m.get("slug"))) or [None])
             and (re.search(r"(\d{4}-\d{2}-\d{2})", str(m.get("slug"))).group(1) < today
                  if re.search(r"(\d{4}-\d{2}-\d{2})", str(m.get("slug"))) else False)]
    print(f"\n  lingering PAST-DATE pm MLB markets (a postponed game's market would sit here): {len(stale)}")
    out["stale_pm_mlb"] = []
    for m in stale[:5]:
        bids, offs, state = pm_book(str(m.get("slug")))
        print(f"    {m.get('slug')}  closed={m.get('closed')} state={state!r} "
              f"bids={len(bids)} offers={len(offs)} best_bid={bids[0] if bids else None}")
        out["stale_pm_mlb"].append({"slug": m.get("slug"), "closed": m.get("closed"), "state": state,
                                    "bids": bids[:5], "offers": offs[:5]})
    _dump("mlb_unwind_books.json", out)


# ---------------------------------------------------------------------------------------------
# --postmortem : what did EACH VENUE actually do with the recently postponed games (--scan output)?
# This is the first empirical read of the void path itself: pmus market state/endDate vs Kalshi
# result/settlement_value/close_time. Needs scripts/_data/mlb_postpone_samples.json (run --scan first).
# ---------------------------------------------------------------------------------------------
def postmortem():
    src = os.path.join(DATA, "mlb_postpone_samples.json")
    if not os.path.exists(src):
        print("  run --scan first (needs _data/mlb_postpone_samples.json)"); return
    with open(src, encoding="utf-8") as f:
        d = json.load(f)
    teams = {t["id"]: str(t.get("abbreviation", "")).lower()
             for t in get(f"{STATS}/teams?sportId=1").get("teams", [])}
    out = []
    for g, date in zip(d["postponed_entries"], d["found_dates"]):
        away = teams.get(((g.get("teams") or {}).get("away", {}).get("team") or {}).get("id"), "?")
        home = teams.get(((g.get("teams") or {}).get("home", {}).get("team") or {}).get("id"), "?")
        sn = snap(g)
        slug = f"aec-mlb-{away}-{home}-{date}"                       # pm slug = away-home-(original date)
        try:                                                          # Kalshi event = date+HHMM(ET)+AWAY+HOME
            gd = dt.datetime.fromisoformat(str(g.get("gameDate")).replace("Z", "+00:00"))
            et = gd - dt.timedelta(hours=4)                           # EDT in season
            ev = f"KXMLBGAME-{et.strftime('%y%b%d').upper()}{et.strftime('%H%M')}{away.upper()}{home.upper()}"
        except Exception:
            ev = None
        rec = {"game": f"{away}@{home} {date}", "gamePk": sn["gamePk"],
               "rescheduleDate": sn["rescheduleDate"], "pm_slug": slug, "k_event": ev}
        pmq = get(f"{PM}?slug={slug}"); time.sleep(0.2)
        pm = (pmq.get("markets") or [{}])[0]
        bk = (get(f"{PM}/{slug}/book") or {}).get("marketData", {}); time.sleep(0.2)
        rec["pm"] = {k: pm.get(k) for k in ("closed", "endDate", "gameStartTime", "outcome",
                                            "resolvedOutcome", "lastTradePrice")}
        rec["pm"]["book_state"] = bk.get("state")
        rec["pm"]["book_levels"] = (len(bk.get("bids") or []), len(bk.get("offers") or []))
        kd = get(f"{KAL.rsplit('/', 1)[0]}/events/{ev}") if ev else {}
        rec["kalshi"] = []
        for m in kd.get("markets") or []:
            tk = m.get("ticker")
            full = get(f"{KAL}/{tk}").get("market", {}); time.sleep(0.2)
            rec["kalshi"].append({k: full.get(k) for k in
                                  ("ticker", "status", "result", "settlement_value_dollars",
                                   "last_price_dollars", "close_time")})
        print(f"\n  {rec['game']}  makeup={str(sn['rescheduleDate'])[:10]}")
        print(f"    pmus  {slug}: closed={rec['pm']['closed']} state={rec['pm']['book_state']!r} "
              f"endDate={str(rec['pm']['endDate'])[:16]} (start+14d) outcome={rec['pm']['outcome']!r} "
              f"book={rec['pm']['book_levels']}")
        for k in rec["kalshi"]:
            print(f"    kalshi {k['ticker']}: status={k['status']} result={k['result']!r} "
                  f"settle=${k['settlement_value_dollars']} last=${k['last_price_dollars']} "
                  f"closed={str(k['close_time'])[:16]}")
        out.append(rec)
    _dump("mlb_postmortem.json", out)
    print("\n  READ: result='scalar' + settlement≈last price (pair normalized to $1.00) = the Kalshi")
    print("  fair-price void; close_time vs the original start = how fast the unwind window shuts.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="MLB postponement detection + unwind-liquidity probe (read-only)")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--scan", action="store_true"); ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--feed", type=int, metavar="GAMEPK")
    ap.add_argument("--rules", action="store_true")
    ap.add_argument("--archive", action="store_true")
    ap.add_argument("--books", action="store_true"); ap.add_argument("--max-pairs", type=int, default=3)
    ap.add_argument("--postmortem", action="store_true")
    a = ap.parse_args()
    if a.selftest: _selftest()
    if a.scan: scan(a.days)
    if a.feed: feed(a.feed)
    if a.rules: rules()
    if a.archive: archive()
    if a.books: books(a.max_pairs)
    if a.postmortem: postmortem()
    if not any((a.selftest, a.scan, a.feed, a.rules, a.archive, a.books, a.postmortem)):
        ap.print_help()
