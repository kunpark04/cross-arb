"""scripts/settle_recon.py - EMPIRICAL test of invariant #1: do BOTH venues actually grade a
co-listed market to the SAME outcome? Uses SETTLED markets only. READ-ONLY, no capital.

Invariant #1 ("settlement identity") is currently believed from RULES TEXT (both venues' rules say
they grade off the same NWS CLI number / the same final game score). This script replaces that
INFERENCE with a DIRECT empirical check: take markets that were genuinely co-listed on both venues,
wait for them to SETTLE, and read each venue's RESOLVED outcome from its own API. If invariant #1
holds, every joined pair agrees YES<->YES. A DISAGREEMENT is a real settlement-divergence finding
(a "locked" pair could then lose BOTH legs) and is reported loudly with the specific markets.

WEATHER = THREE-WAY reconciliation (2026-06-10 extension). For every settled co-listed
(city, event-date, bucket) we compare THREE independent reads:
    (a) Kalshi's graded result (result yes/no) + the settlement number Kalshi RECORDED
        (expiration_value, e.g. "91.00") + WHEN it settled (settlement_ts, ~12:01Z = 8:01am ET D+1),
    (b) pmus's graded outcome (outcomes/outcomePrices by exact slug),
    (c) the NWS CLI daily max itself - monitor-logged cli.jsonl first (final = last issuance per
        (station, date)), falling back to walking forecast.weather.gov CLI text versions (same
        parse_cli the monitor uses; newest-first, first hit per report_date = the final value).
Pairs are enumerated BEYOND the live transition archive: the pmus weather slug deterministically
encodes the canonical inclusive bucket (pm_bounds), so for every settled Kalshi bucket we can
CONSTRUCT the slug of its bounds-identical pmus twin (pm_slug_for - the same identity join
colisted_map enforces, run in reverse) and fetch it; absent slug = not co-listed that day.

WHY the sports join is still sourced from the live transition archive (not a fresh catalog scan):
  The monitor (bot/monitor.py) already paired markets that were SIMULTANEOUSLY live on both venues,
  using the validated identity join. Those pmus slugs are the ground-truth "was co-listed" set.
  A pmus market is fetched by its EXACT slug (`?slug=<slug>`) - the only reliable way to read a
  SETTLED climate market, since pmus's paginated `closed=true` feed does NOT surface settled climate
  markets (verified 2026-06-09: 2499 closed rows, all old `aec-` sports, zero `tc-temp`).

RESOLUTION PARSING (per the venue gateways; pmus convention CORRECTED 2026-06-10):
  * Kalshi  : GET /markets/{ticker} -> result in {"yes","no"}, status "finalized"/"settled";
              settled objects also carry settlement_ts + expiration_value (the number it graded on).
  * pmus    : ?slug=<slug> -> closed:true. The winner MUST be read from `marketSides` - each side
              object carries its OWN label (description / team.name) + its settled `price` ("1" wins,
              "0" loses), which is unambiguous. The flat `outcomes`/`outcomePrices` arrays are NOT
              index-aligned with each other: outcomePrices follows the marketSides order (Yes/long
              side first in every observed object), while the outcomes array's order varies
              cosmetically (["Yes","No"] or ["No","Yes"]). The 2026-06-09 convention documented here
              previously - "pair outcomes[i] with outcomePrices[i]" - is WRONG: it mis-grades every
              No-first market. Verified 2026-06-10 on 286 settled weather buckets: marketSides-paired
              winner == Kalshi result 286/286; label-paired was wrong on exactly the 141 No-first
              objects (and manufactured 44 phantom "inconsistent days" + 22 phantom weather + ~21
              phantom sports "divergences" in earlier runs, incl. the 2026-06-09 "MIA 4-YES day").
              On an OPEN market outcomePrices are live prices and can even be a RAGGED 1-element
              array - pm_winner requires closed + a unique side priced 1. pmus exposes NO settlement
              timestamp: updatedAt/ep3SyncedAt are touched by a periodic resync (~hourly, observed
              live), and ep3Status is EXPIRED for both interim sports and finalized weather - so
              finalization lag can only be BRACKETED by reads at known times.

DATA-INTEGRITY GUARD (kept as a tripwire): a single high temp lands in exactly one bucket, so a
(city,date) whose pmus YES-winner count != 1 is internally impossible -> flagged pmus_inconsistent
and EXCLUDED from the agree/disagree tally (it cannot be compared fairly) but reported loudly.
This gate is what exposed the array-order parse bug above (44/70 days tripped it); post-fix it
should fire ~never, and any future trip is a real pmus data-integrity event worth raw-dumping.

Usage:
  python scripts/settle_recon.py --selftest          # offline synthetic verification
  python scripts/settle_recon.py                     # live three-way weather recon + sports recon
  python scripts/settle_recon.py --no-sports         # weather only (faster)
  python scripts/settle_recon.py --days-back 8       # shrink the enumerated weather window
  python scripts/settle_recon.py --no-nws            # skip the NWS version-walk CLI fallback
  python scripts/settle_recon.py --max-pairs 40      # cap sports pmus fetches
"""
import os, sys, re, json, gzip, glob, time, argparse, collections, datetime, urllib.request, urllib.error
try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass

# --- reuse the validated, identity-correct join helpers (do NOT duplicate) ---
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bot"))
from colisted_map import (get, KAL, PM, pm_bounds, kbounds, pick_game, surname, ktok_iso, wcity,  # noqa: E402
                          ECON, econ_parse, econ_twin, _FEDLBL)                                   # noqa: E402
from monitor import parse_cli  # NWS CLI text-product parser (single source of truth)             # noqa: E402

ARCHIVE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "data", "cross-arb")
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_data")
WX = {"sfo": "KXHIGHTSFO", "lax": "KXHIGHLAX", "nyc": "KXHIGHNY", "mia": "KXHIGHMIA", "mdw": "KXHIGHCHI"}
LEAGUES = {"mlb": ("KXMLBGAME", "abbrev"), "wnba": ("KXWNBAGAME", "abbrev"), "nba": ("KXNBAGAME", "abbrev"),
           "nhl": ("KXNHLGAME", "abbrev"), "atp": ("KXATPMATCH", "surname"), "wta": ("KXWTAMATCH", "surname"),
           "itfm": ("KXITFMATCH", "surname"), "itfw": ("KXITFWMATCH", "surname"), "ufc": ("KXUFCFIGHT", "surname")}


# ============================================================================================
# PARSING (pure, self-tested)
# ============================================================================================
_WIN, _LOSE = ("1", "1.0", "1.00"), ("0", "0.0", "0.00")

def pm_winner(market):
    """pmus settled market -> ("Yes"/"No"/team-label-or-None, raw_outcomes, raw_prices).
    PRIMARY: marketSides - each side carries its OWN label + settled price, so the read is
    unambiguous (winner = the unique side priced 1, every other side priced 0). FALLBACK (sides
    absent): pair outcomes[i] with outcomePrices[i] - but note those arrays are NOT reliably
    index-aligned (see module docstring; the fallback is best-effort only)."""
    if not market or not market.get("closed"):
        return (None, None, None)
    try:
        oc = json.loads(market.get("outcomes") or "[]")
        op = json.loads(market.get("outcomePrices") or "[]")
    except Exception:
        oc = op = None
    sides = market.get("marketSides") or []
    if sides:
        w = [s for s in sides if str(s.get("price")) in _WIN]
        l = [s for s in sides if str(s.get("price")) in _LOSE]
        if len(w) == 1 and len(l) == len(sides) - 1:   # a clean settled 1-vs-0 grade
            s = w[0]
            team = s.get("team")
            lab = s.get("description") or (team.get("name") if isinstance(team, dict) else team)
            return (lab, oc, op)
        return (None, oc, op)                           # sides present but not cleanly graded
    if oc and op:
        win = [oc[i] for i in range(min(len(oc), len(op))) if str(op[i]) in _WIN]
        return (win[0] if len(win) == 1 else None, oc, op)
    return (None, oc, op)

def kal_result(market):
    """Kalshi settled market -> 'yes'/'no'/None (None unless finalized/settled with a result)."""
    if not market or market.get("status") not in ("finalized", "settled"):
        return None
    r = market.get("result")
    return r if r in ("yes", "no") else None

def parse_wx_slug(slug):
    """tc-temp-<city>high-<YYYY-MM-DD>-<bucket> -> (city, date, bucketsuffix) or None."""
    m = re.match(r"tc-temp-([a-z]+)high-(\d{4}-\d{2}-\d{2})-(.+)$", str(slug))
    return (m.group(1), m.group(2), m.group(3)) if m else None

def parse_sport_slug(slug):
    """aec-<league>-<a>-<b>-<YYYY-MM-DD> -> (league, a, b, date) or None."""
    m = re.match(r"aec-([a-z0-9]+)-(.+?)-(.+?)-(\d{4}-\d{2}-\d{2})$", str(slug))
    return (m.group(1), m.group(2), m.group(3), m.group(4)) if m else None

def pm_slug_for(city, date, bounds):
    """INVERSE of pm_bounds: canonical inclusive (lo,hi) -> the pmus slug encoding EXACTLY those
    bounds. This is colisted_map's identity join run in reverse - it lets the recon enumerate the
    bounds-identical pmus twin of every settled Kalshi bucket without needing the live archive.
    None for an unbounded (None, None)."""
    lo, hi = bounds
    if lo is None and hi is None:
        return None
    if lo is None:
        return f"tc-temp-{city}high-{date}-lt{hi + 1}f"      # (-inf, hi] == 'less than hi+1'
    if hi is None:
        return f"tc-temp-{city}high-{date}-gte{lo}f"         # [lo, inf)
    return f"tc-temp-{city}high-{date}-gte{lo}lt{hi}f"       # [lo, hi] (the 2-deg middle bucket)

def bucket_hit(bounds, mx):
    """Does daily max `mx` land in canonical inclusive (lo,hi)? None when unanswerable."""
    if mx is None:
        return None
    lo, hi = bounds
    if lo is None and hi is None:
        return None
    return (lo is None or mx >= lo) and (hi is None or mx <= hi)

def cli_finals(lines):
    """cli.jsonl lines -> {(station,date): {final, values, revised}}. `final` = the LAST-logged max
    (highest t; ties -> later line), i.e. the value the venues settle on; values keeps the distinct
    sequence so a REVISED day (e.g. MDW 2026-06-09 87->88) is visible."""
    best = {}
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        try:
            r = json.loads(ln)
        except Exception:
            continue
        k = (r.get("station"), r.get("report_date"))
        if None in k or not isinstance(r.get("max"), int):
            continue
        e = best.setdefault(k, {"_t": float("-inf"), "final": None, "values": []})
        if r["max"] not in e["values"]:
            e["values"].append(r["max"])
        if r.get("t", 0) >= e["_t"]:
            e["_t"], e["final"] = r.get("t", 0), r["max"]
    return {k: {"final": e["final"], "values": e["values"], "revised": len(e["values"]) > 1}
            for k, e in best.items()}

def load_cli(path):
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return cli_finals(f)

def kal_recorded_value(kmkts):
    """The settlement number Kalshi RECORDED for a day (expiration_value, consensus across the
    day's buckets) -> (value_or_None, all_distinct_values). >1 distinct = internal conflict."""
    vals = set()
    for m in kmkts:
        v = m.get("expiration_value")
        if v in (None, ""):
            continue
        try:
            vals.add(float(v))
        except (TypeError, ValueError):
            pass
    vals = sorted(vals)
    return (vals[0] if len(vals) == 1 else None), vals

def iso_lag_h(iso, now_ts):
    """Hours from an ISO-8601 UTC stamp ('...Z', optional fractional secs) to now_ts, or None."""
    if not iso:
        return None
    try:
        s = re.sub(r"\.(\d+)$", lambda m: "." + (m.group(1) + "000000")[:6], str(iso).rstrip("Z"))
        dt = datetime.datetime.fromisoformat(s).replace(tzinfo=datetime.timezone.utc)
        return round((now_ts - dt.timestamp()) / 3600.0, 2)
    except Exception:
        return None

def classify_day(buckets, truth):
    """PURE per-day three-way classification (mutates bucket dicts, returns day flags).
    buckets: [{"bounds": (lo,hi), "pm_out": Yes/No/None, "kal_result": yes/no/None}, ...] - one
    (city, event-date)'s joined pairs. truth: the independent CLI max or None.
      status  : agree | DIVERGE | pm_inconsistent (day multi-YES -> excluded from tally)
                | pm_unresolved (pmus open/interim) | kal_unsettled
      *_vs_cli: match | MISMATCH | None  (three-way legs, only when truth is known)
    The multi-YES exclusion is the data-integrity guard: >1 pmus YES on disjoint buckets is
    impossible, so that day's pmus data is interim/untrustworthy and CANNOT be fairly compared."""
    n_yes = sum(1 for b in buckets if b.get("pm_out") == "Yes")
    multi = n_yes > 1
    for b in buckets:
        hit = bucket_hit(b["bounds"], truth)
        b["cli_hit"] = hit
        kr, po = b.get("kal_result"), b.get("pm_out")
        b["kal_vs_cli"] = (None if (hit is None or kr not in ("yes", "no"))
                           else ("match" if (kr == "yes") == hit else "MISMATCH"))
        b["pm_vs_cli"] = (None if (hit is None or po not in ("Yes", "No") or multi)
                          else ("match" if (po == "Yes") == hit else "MISMATCH"))
        if po not in ("Yes", "No"):
            b["status"] = "pm_unresolved"
        elif kr not in ("yes", "no"):
            b["status"] = "kal_unsettled"
        elif multi:
            b["status"] = "pm_inconsistent"
        elif (po == "Yes") == (kr == "yes"):
            b["status"] = "agree"
        else:
            b["status"] = "DIVERGE"
    return {"pm_yes_count": n_yes, "pm_multi_yes": multi}


# ============================================================================================
# CO-LISTED SET from the live transition archive (ground-truth "was co-listed on both")
# ============================================================================================
def archive_slugs(archive=ARCHIVE):
    """Distinct pmus slugs the monitor actually paired (each was simultaneously live on both venues).
    AT-MOST-ONE file per event-date (raw .jsonl wins over same-date .gz - mirrors analyze_persistence)."""
    if not os.path.isdir(archive):
        return []
    chosen = {}
    for path in glob.glob(os.path.join(archive, "transitions-*.jsonl.gz")) + \
                glob.glob(os.path.join(archive, "transitions-*.jsonl")):
        m = re.search(r"transitions-(.+?)\.jsonl(?:\.gz)?$", os.path.basename(path))
        chosen[m.group(1) if m else path] = path
    seen = set()
    for path in sorted(chosen.values()):
        op = gzip.open if path.endswith(".gz") else open
        with op(path, "rt", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        s = json.loads(line).get("market")
                        if s:
                            seen.add(s)
                    except Exception:
                        pass
    return sorted(seen)


# ============================================================================================
# KALSHI settled-bucket index (per city) / settled-event index (per league)
# ============================================================================================
def kal_wx_settled(city, errs=None):
    """{date: {(lo,hi)-bounds: market}} for one WX city's SETTLED Kalshi buckets."""
    kser = WX.get(city)
    if not kser:
        return {}
    d = get(f"{KAL}?series_ticker={kser}&status=settled&limit=1000", errs=errs)
    out = collections.defaultdict(dict)
    for m in d.get("markets", []):
        dm = re.search(r"-(\d{2}[A-Z]{3}\d{2})", str(m.get("ticker")))
        if not dm:
            continue
        date = ktok_iso(dm.group(1))
        out[date][kbounds(m)] = m
    return out

def kal_league_settled(series, join):
    """kbydate structure pick_game() consumes, but over SETTLED Kalshi markets; plus {ticker: market}."""
    d = get(f"{KAL}?series_ticker={series}&status=settled&limit=1000")
    byev, evd, by_ticker = collections.defaultdict(dict), {}, {}
    for m in d.get("markets", []):
        ev = m.get("event_ticker"); tk = str(m.get("ticker", ""))
        by_ticker[tk] = m
        key = tk.split("-")[-1].lower() if join == "abbrev" else surname(m.get("yes_sub_title"))
        dm = re.search(r"-(\d{2}[A-Z]{3}\d{2})", tk); evd[ev] = ktok_iso(dm.group(1)) if dm else None
        if key:
            byev[ev][key] = tk
    kbydate = collections.defaultdict(list)
    for ev, pl in byev.items():
        kbydate[evd.get(ev)].append(pl)
    return kbydate, by_ticker


# ============================================================================================
# NWS CLI fallback (dates older than the monitor's cli.jsonl)
# ============================================================================================
def nws_cli_walk(station, need_dates, max_versions=60, log=print):
    """Walk forecast.weather.gov's retained CLI text versions NEWEST-first; the FIRST issuance seen
    for a report_date is that date's LATEST (= final) value. Returns {date: max}. Read-only public
    product; stops once every needed date is found, the walk passes the oldest needed date, or the
    server starts repeating its last retained version."""
    need = {d for d in need_dates}
    out, last_sig = {}, None
    if not need:
        return out
    oldest = min(need)
    for v in range(1, max_versions + 1):
        url = (f"https://forecast.weather.gov/product.php?site=NWS&issuedby={station}"
               f"&product=CLI&format=TXT&version={v}&glossary=0")
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "cross-arb/1.0"}),
                                        timeout=20) as r:
                html = r.read().decode("utf-8", "replace")
        except Exception as e:
            log(f"  [nws {station}] v{v} fetch failed ({e!r}) - stopping walk")
            break
        m = re.search(r"<pre[^>]*>(.*?)</pre>", html, re.S | re.I)
        rec = parse_cli(m.group(1) if m else html, station)
        time.sleep(0.15)
        if not rec:
            break
        sig = (rec.get("wmo"), rec.get("issued"))
        if sig == last_sig:                      # retention exhausted -> server repeats last product
            break
        last_sig = sig
        d = rec["report_date"]
        if d in need and d not in out:
            out[d] = rec["max"]
        if need <= set(out):
            break
        if d < oldest:                            # walked past the window; older needs not retained
            break
    return out


# ============================================================================================
# WEATHER three-way recon (Kalshi result  <->  pmus outcome  <->  NWS CLI max)
# ============================================================================================
def recon_weather(arch_slugs, archive=ARCHIVE, days_back=12, use_nws=True, log=print):
    """For every settled co-listed weather (city, event-date, bucket): three-way reconcile.
    Pairs = archive-tracked slugs UNION slugs constructed from the settled Kalshi ladder
    (pm_slug_for; bounds-identical by construction). Returns {"days": [...], "read_ts", ...}."""
    read_ts = time.time()
    today_iso = datetime.date.fromtimestamp(read_ts).isoformat()
    cutoff = (datetime.date.fromtimestamp(read_ts) - datetime.timedelta(days=days_back)).isoformat()
    cli = load_cli(os.path.join(archive, "cli.jsonl"))
    errs = []

    arch_by_day = collections.defaultdict(set)
    for s in arch_slugs:
        p = parse_wx_slug(s)
        if p and p[0] in WX:
            arch_by_day[(p[0], p[1])].add(s)

    days = []
    for city in sorted(WX):
        st = city.upper()                                    # cli.jsonl station id == upper(city)
        ksettled = kal_wx_settled(city, errs=errs); time.sleep(0.2)
        dates = ({d for d in ksettled if d and d >= cutoff} |
                 {d for (c, d) in arch_by_day if c == city})
        # NWS fallback only for PAST dates the monitor's cli.jsonl doesn't cover (a final CLI
        # can't exist yet for today/future event-dates)
        nws = {}
        if use_nws:
            need = sorted(d for d in dates if (st, d) not in cli and d < today_iso)
            if need:
                nws = nws_cli_walk(st, need, log=log)
        for date in sorted(dates):
            kbuckets = ksettled.get(date, {})                # {bounds: settled kalshi market}
            slugs = {pm_slug_for(city, date, b) for b in kbuckets} | arch_by_day.get((city, date), set())
            slugs.discard(None)
            buckets, not_listed, fetch_errors = [], [], []
            for slug in sorted(slugs):
                d = get(f"{PM}?slug={slug}", errs=errs); time.sleep(0.12)
                if "_err" in d:
                    fetch_errors.append(slug); continue      # fetch failure != not listed
                pmm = (d.get("markets") or [None])[0]
                if pmm is None:
                    not_listed.append(slug); continue        # pmus never listed this bucket/date
                b = pm_bounds(slug)
                kmkt = kbuckets.get(b)
                pm_out, pm_oc, pm_op = pm_winner(pmm)
                buckets.append({"slug": slug, "bounds": b, "pm_out": pm_out, "pm_oc": pm_oc,
                                "pm_op": pm_op, "pm_closed": bool(pmm.get("closed")),
                                "pm_end": pmm.get("endDate"), "pm_ep3": pmm.get("ep3Status"),
                                "kalshi": kmkt.get("ticker") if kmkt else None,
                                "kal_result": kal_result(kmkt),
                                "_pm_raw": pmm, "_k_raw": kmkt})
            if not buckets and not not_listed and not fetch_errors:
                continue                                     # nothing co-listable this date
            e = cli.get((st, date))
            truth, src = (e["final"], "cli.jsonl") if e else (nws.get(date), "nws" if date in nws else None)
            kval, kvals = kal_recorded_value(list(kbuckets.values()))
            flags = classify_day(buckets, truth)
            k_yes = [m.get("ticker") for m in kbuckets.values() if kal_result(m) == "yes"]
            sts = [m.get("settlement_ts") for m in kbuckets.values() if m.get("settlement_ts")]
            settle_iso = max(sts) if sts else None
            days.append({"city": city, "date": date, "truth": truth, "truth_src": src,
                         "cli_revised": bool(e and e["revised"]), "cli_values": (e or {}).get("values", []),
                         "kal_value": kval, "kal_values_all": kvals,
                         "kal_yes_tickers": k_yes, "n_kal_buckets": len(kbuckets),
                         "pm_not_listed": not_listed, "pm_fetch_errors": fetch_errors,
                         "buckets": buckets, "settlement_ts": settle_iso,
                         "read_lag_h": iso_lag_h(settle_iso, read_ts), **flags})
    return {"days": days, "read_ts": read_ts, "cutoff": cutoff, "fetch_errors": errs}


def summarize_weather(wx):
    """Tallies over recon_weather()'s day rows (pure; selftest-able via synthetic days)."""
    s = collections.Counter()
    div, mism, incon_days = [], [], []
    for day in wx["days"]:
        if day["pm_multi_yes"]:
            incon_days.append(day)
        if len(day.get("kal_yes_tickers", [])) > 1:
            s["kal_multi_yes_days"] += 1
        for b in day["buckets"]:
            s[b["status"]] += 1
            if b["status"] == "DIVERGE":
                div.append((day, b))
            for leg in ("kal_vs_cli", "pm_vs_cli"):
                if b.get(leg) == "match":
                    s[leg + "_match"] += 1
                elif b.get(leg) == "MISMATCH":
                    s[leg + "_MISMATCH"] += 1
                    mism.append((leg, day, b))
        s["pm_not_listed"] += len(day["pm_not_listed"])
        s["pm_fetch_errors"] += len(day["pm_fetch_errors"])
        if day["truth"] is not None and day["kal_value"] is not None:
            s["kalval_vs_cli_" + ("match" if float(day["kal_value"]) == float(day["truth"]) else "MISMATCH")] += 1
    s["compared"] = s["agree"] + s["DIVERGE"]
    s["pending"] = s["pm_unresolved"] + s["kal_unsettled"]
    return {"tally": dict(s), "divergences": div, "cli_mismatches": mism, "inconsistent_days": incon_days}


def day_state(day):
    if any(b["status"] == "DIVERGE" for b in day["buckets"]): return "DIVERGE"
    if day["pm_multi_yes"]: return "pm_inconsistent"
    st = {b["status"] for b in day["buckets"]}
    if st and st <= {"agree"}: return "clean"
    if "agree" in st: return "partial"
    return "pending"


def report_weather(wx):
    sm = summarize_weather(wx)
    t = sm["tally"]
    print(f"\n[WEATHER - THREE-WAY]  window since {wx['cutoff']}  "
          f"(days with co-listing: {len(wx['days'])})")
    print(f"  joined settled pairs compared    : {t['compared']}")
    print(f"  BOTH venues graded IDENTICALLY   : {t.get('agree', 0)}")
    print(f"  DIVERGENCES (pm vs kalshi)       : {t.get('DIVERGE', 0)}")
    print(f"  pending (pm interim/kal unsettled): {t['pending']} "
          f"(pm_unresolved={t.get('pm_unresolved', 0)}, kal_unsettled={t.get('kal_unsettled', 0)})")
    print(f"  pmus-inconsistent-day buckets    : {t.get('pm_inconsistent', 0)} "
          f"(days: {len(sm['inconsistent_days'])}; excluded from tally)")
    print(f"  pm bucket not listed (not co-listed): {t.get('pm_not_listed', 0)}   "
          f"pm fetch errors: {t.get('pm_fetch_errors', 0)}")
    print(f"  three-way vs NWS CLI: kalshi result {t.get('kal_vs_cli_match', 0)} match / "
          f"{t.get('kal_vs_cli_MISMATCH', 0)} MISMATCH; pmus outcome {t.get('pm_vs_cli_match', 0)} match / "
          f"{t.get('pm_vs_cli_MISMATCH', 0)} MISMATCH")
    print(f"  kalshi recorded value (expiration_value) vs independent CLI: "
          f"{t.get('kalval_vs_cli_match', 0)} match / {t.get('kalval_vs_cli_MISMATCH', 0)} MISMATCH")
    if t.get("kal_multi_yes_days"):
        print(f"  !!! KALSHI multi-YES days (impossible): {t['kal_multi_yes_days']}")
    if wx.get("fetch_errors"):
        print(f"  !! DEGRADED: {len(wx['fetch_errors'])} venue fetch errors this pass "
              f"(missing rows may be fetch failures, not absences)")

    for day in sorted(wx["days"], key=lambda d: (d["date"], d["city"])):
        rev = " CLI-REVISED:" + "->".join(map(str, day["cli_values"])) if day["cli_revised"] else ""
        tr = f"{day['truth']} ({day['truth_src']})" if day["truth"] is not None else "?"
        print(f"    {day['date']} {day['city']}: cli={tr}{rev} kal_val={day['kal_value']} "
              f"state={day_state(day)} joined={len(day['buckets'])} "
              f"agree={sum(1 for b in day['buckets'] if b['status'] == 'agree')} "
              f"lag_h={day['read_lag_h']}")
    if sm["inconsistent_days"]:
        print(f"\n  !!! pmus INTERNALLY-INCONSISTENT days (YES-winner count != 1; EXCLUDED from tally):")
        for day in sm["inconsistent_days"]:
            ys = [b["slug"] for b in day["buckets"] if b["pm_out"] == "Yes"]
            print(f"      {day['city']} {day['date']}: pmus YES on {len(ys)} disjoint buckets -> {ys}")
    for day, b in sm["divergences"]:
        print(f"\n  !!! DIVERGE {day['city']} {day['date']} {b['slug']} bounds={b['bounds']} cli={day['truth']}")
        print(f"      pmus={b['pm_out']} (oc={b['pm_oc']} op={b['pm_op']})  |  kalshi={b['kalshi']}={b['kal_result']}")
    for leg, day, b in sm["cli_mismatches"]:
        print(f"\n  !!! {leg} MISMATCH {day['city']} {day['date']} {b['slug']} bounds={b['bounds']} "
              f"cli={day['truth']} ({day['truth_src']}) pm={b['pm_out']} kal={b['kal_result']}")
    return sm


# ============================================================================================
# SPORTS recon (best-effort)
# ============================================================================================
def recon_sports(slugs, max_pairs, log=print):
    sp_slugs = [s for s in slugs if str(s).startswith("aec-")]
    by_league = collections.defaultdict(list)
    for s in sp_slugs:
        p = parse_sport_slug(s)
        if p and p[0] in LEAGUES:
            by_league[p[0]].append(s)

    rows, divergences = [], []
    matched = mism = compared = 0
    kcache = {}
    budget = max_pairs

    for league in sorted(by_league):
        series, join = LEAGUES[league]
        if league not in kcache:
            kcache[league] = kal_league_settled(series, join); time.sleep(0.25)
        kbydate, by_ticker = kcache[league]
        for slug in sorted(by_league[league]):
            if budget <= 0:
                break
            _, a, b, date = parse_sport_slug(slug)
            pm = get(f"{PM}?slug={slug}"); time.sleep(0.12)
            pmm = (pm.get("markets") or [None])[0] if isinstance(pm, dict) else None
            pm_out, pm_oc, pm_op = pm_winner(pmm)
            budget -= 1

            # map pmus winning label -> which side (A/B) via marketSides team metadata on the pmus object
            win_side = None
            kA = kB = None
            sides = (pmm or {}).get("marketSides") or []
            side_meta = []
            for s in sides:
                tm = (s.get("team") or {})
                side_meta.append({"name": tm.get("name"), "abbr": (tm.get("abbreviation") or "").lower(),
                                  "outcome": s.get("outcome") or s.get("name")})
            if join == "abbrev":
                kkeys = [sm["abbr"] for sm in side_meta if sm["abbr"]]
            else:
                kkeys = [surname(sm["name"]) for sm in side_meta if sm["name"]]
            kkeys = [k for k in kkeys if k]
            if len(kkeys) >= 2:
                kA, kB = kkeys[0], kkeys[1]

            # find the Kalshi settled event + its YES-resolved side
            kal_winner_key = None
            if kA and kB:
                found = pick_game(kbydate, kA, kB, join, date, slug_dated=True)
                if found:
                    pl, mA, mB = found
                    for key, tk in pl.items():
                        m = by_ticker.get(tk)
                        if kal_result(m) == "yes":
                            kal_winner_key = key
                            break

            # map pmus winner label back to a team key for comparison
            pm_winner_key = None
            if pm_out:
                for sm in side_meta:
                    label = sm["outcome"] or sm["name"]
                    if label and str(label).strip().lower() == str(pm_out).strip().lower():
                        pm_winner_key = (sm["abbr"] if join == "abbrev" else surname(sm["name"]))
                        break

            # pmus settlement-state signal: a closed market whose endDate is still in the FUTURE (well
            # after the game's calendar date) is a tell that pmus's outcomePrices are an interim/pipeline
            # state, not the FINAL graded result - it bears on whether a "divergence" is pmus being wrong.
            pm_end = (pmm or {}).get("endDate")
            pm_end_future = bool(pm_end and isinstance(pm_end, str) and pm_end[:10] > date)

            row = {"cat": "sports", "league": league, "date": date, "slug": slug, "pm_out": pm_out,
                   "pm_winner_key": pm_winner_key, "kal_winner_key": kal_winner_key,
                   "pm_end": pm_end, "pm_end_future": pm_end_future}
            rows.append(row)
            if pm_winner_key and kal_winner_key:
                compared += 1
                if pm_winner_key == kal_winner_key:
                    matched += 1; row["status"] = "agree"
                else:
                    mism += 1; row["status"] = "DISAGREE"; divergences.append(row)
            else:
                row["status"] = "unsettled_or_unjoined"
        if budget <= 0:
            break
    return {"rows": rows, "compared": compared, "matched": matched, "mismatched": mism,
            "divergences": divergences, "n_slugs": len(sp_slugs)}


# ============================================================================================
# ECON reconciliation (2026-06-11) — econ releases RECUR, so PAST settlements reconcile NOW
# (no need to wait for the next print). Three structures, discovered live on settled markets:
#   (1) urc/nfpc/gdpc = CUMULATIVE ">= T"  -> tradeable twin vs Kalshi "Above T-step" (econ_twin).
#   (2) cpic = EXACT-VALUE buckets ("CPI YoY = X.X%"), only one wins -> NOT a tradeable twin
#       (econ_colisted skips them, point_bucket); reconciled at the PRINT level instead: the pmus
#       winning bucket value must equal Kalshi's implied print = a settlement-SOURCE identity check.
#   (3) rdc/FOMC = CATEGORICAL -> tradeable twin via _FEDLBL (decision-bucket match).
# ============================================================================================
def kal_implied_print(kmkts):
    """Kalshi cumulative 'Above floor' settled ladder -> implied print. On a 1-grid-step ladder the
    print equals the lowest NO floor ('Above fn' NO => print<=fn; 'Above fn-step' YES => print>=fn).
    Returns (print_or_None, last_yes_floor, first_no_floor)."""
    yes = [round(float(m["floor_strike"]), 6) for m in kmkts
           if m.get("floor_strike") is not None and kal_result(m) == "yes"]
    no = [round(float(m["floor_strike"]), 6) for m in kmkts
          if m.get("floor_strike") is not None and kal_result(m) == "no"]
    fn = min(no) if no else None
    return (fn, max(yes) if yes else None, fn)

def econ_reconcile(pre, step, pm_mkts, kfloor_mkt, klabel_mkt):
    """PURE reconciliation of ONE settled econ event (selftest-able with synthetic dicts).
    kfloor_mkt = {rounded_floor: kalshi_market}; klabel_mkt = {lower_sub_title: kalshi_market}.
    Returns row dicts {kind, key, pm, kal, status in agree|DIVERGE|pm_pending|no_twin}."""
    rows = []
    if pre == "rdc":                                            # categorical
        kwin = next((lbl for lbl, m in klabel_mkt.items() if kal_result(m) == "yes"), None)
        for m in pm_mkts:
            p = econ_parse(m.get("slug"))
            if not p or p.get("ineq") != "cat": continue
            if str(pm_winner(m)[0]).lower() == "yes":
                klbl = _FEDLBL.get(p["label"])
                st = "agree" if (kwin and klbl == kwin) else ("DIVERGE" if kwin else "pm_pending")
                rows.append({"kind": "cat", "key": p["label"], "pm": p["label"], "kal": kwin, "status": st})
        return rows
    eq = []                                                     # threshold families
    for m in pm_mkts:
        p = econ_parse(m.get("slug"))
        if not p or p.get("thr") is None: continue
        pmw = pm_winner(m)[0]; pm_side = "yes" if str(pmw).lower() == "yes" else ("no" if pmw is not None else None)
        if p["ineq"] == ">=":                                   # tradeable twin: pmus >=T <-> Kalshi Above T-step
            km = kfloor_mkt.get(round(econ_twin(p["thr"], step), 6)); kres = kal_result(km) if km else None
            st = "no_twin" if not km else ("pm_pending" if pm_side is None else ("agree" if pm_side == kres else "DIVERGE"))
            rows.append({"kind": "ge_twin", "key": p["thr"], "pm": pm_side, "kal": kres, "status": st})
        elif p["ineq"] == "==":                                 # exact bucket -> print-identity, collapsed below
            eq.append((p["thr"], pm_side))
    if eq:                                                      # one print-match row per event
        imp = kal_implied_print(list(kfloor_mkt.values()))[0]
        pm_win_val = next((thr for thr, side in eq if side == "yes"), None)
        st = ("agree" if (imp is not None and pm_win_val is not None and round(imp, 6) == round(pm_win_val, 6))
              else ("DIVERGE" if (imp is not None and pm_win_val is not None) else "pm_pending"))
        rows.append({"kind": "eq_print", "key": "print", "pm": pm_win_val, "kal": imp, "status": st})
    return rows

def recon_econ(log=print):
    """Fetch settled econ on both venues and reconcile per event. Read-only. Returns {(fam,period): rows}."""
    macro, off = [], 0
    while True:
        d = get(f"{PM}?categories[]=macro&closed=true&limit=200&offset={off}")
        pg = d.get("markets", []); macro += pg
        if len(pg) < 200 or len(macro) > 4000: break
        off += 200
    bypre = collections.defaultdict(list)
    for m in macro: bypre[str(m.get("slug", "")).split("-")[0]].append(m)
    out = {}
    for pre, (kser, fam, step) in ECON.items():
        pml = bypre.get(pre) or []
        if not pml: continue
        byper = collections.defaultdict(list)
        for m in pml:
            p = econ_parse(m.get("slug"))
            if p and p.get("period"): byper[p["period"]].append(m)
        kd = get(f"{KAL}?series_ticker={kser}&status=settled&limit=400"); time.sleep(0.2)
        kfloor, klabel = collections.defaultdict(dict), collections.defaultdict(dict)
        for m in kd.get("markets", []):
            pm_ = re.search(r"-(\d{2}[A-Z]{3}\d{0,2})-", str(m.get("ticker", ""))); per = pm_.group(1) if pm_ else None
            if m.get("floor_strike") is not None: kfloor[per][round(float(m["floor_strike"]), 6)] = m
            klabel[per][str(m.get("yes_sub_title", "")).lower()] = m
        for per, pmkts in sorted(byper.items()):
            rows = econ_reconcile(pre, step, pmkts, kfloor.get(per, {}), klabel.get(per, {}))
            if rows: out[(fam, per)] = rows
    return out

def report_econ(ec):
    print("\n--- ECON (settled past releases; recurring -> reconcilable now) ---")
    if not ec:
        print("  no settled co-listed econ events found (pmus lists only current-cycle econ?)"); return {}
    agree = diverge = pend = 0
    for (fam, per), rows in sorted(ec.items()):
        for r in rows:
            mark = {"agree": "OK", "DIVERGE": "!!! DIVERGE", "no_twin": "no-twin", "pm_pending": "pending"}.get(r["status"], r["status"])
            kind = {"ge_twin": ">=twin", "eq_print": "print", "cat": "categ"}.get(r["kind"], r["kind"])
            print(f"  {fam:4} {per:6} {kind:7} key={str(r['key']):8} pm={str(r['pm']):9} kal={str(r['kal']):9} {mark}")
            agree += r["status"] == "agree"; diverge += r["status"] == "DIVERGE"; pend += r["status"] in ("no_twin", "pm_pending")
    print(f"  --> econ reconciled: agree={agree}  DIVERGE={diverge}  pending/no-twin={pend}")
    return {"agree": agree, "DIVERGE": diverge, "pending": pend}


# ============================================================================================
# REPORT
# ============================================================================================
def report(wx, sp):
    print("=" * 78)
    print("SETTLEMENT RECONCILIATION - empirical invariant-#1 check (settled markets, read-only)")
    print("=" * 78)

    wsm = report_weather(wx)
    wt = wsm["tally"]

    print(f"\n[SPORTS]   co-listed slugs in archive: {sp['n_slugs']}")
    print(f"  joined+settled pairs compared : {sp['compared']}")
    print(f"  BOTH venues graded IDENTICALLY: {sp['matched']}")
    print(f"  DIVERGENCES                   : {sp['mismatched']}")
    compared_rows = [r for r in sp["rows"] if r.get("status") in ("agree", "DISAGREE")]
    n_cmp_future = sum(1 for r in compared_rows if r.get("pm_end_future"))
    if compared_rows and n_cmp_future:
        print(f"  !!! CAVEAT - pmus endDate STILL IN THE FUTURE on {n_cmp_future}/{len(compared_rows)} "
              f"compared pairs: pmus is closed:true but NOT finalized, so its outcomePrices are an INTERIM")
        print(f"      pipeline value, not the final grade. Neither these agreements NOR divergences are a "
              f"true settlement-identity read yet - pmus must reach its endDate first.")
    n_future = sum(1 for d in sp["divergences"] if d.get("pm_end_future"))
    if sp["divergences"]:
        print(f"  of which DIVERGENCES with future pmus endDate (interim, not final): {n_future}/{sp['mismatched']}")
    for d in sp["divergences"]:
        tag = " [pmus endDate FUTURE: " + str(d.get("pm_end")) + "]" if d.get("pm_end_future") else ""
        print(f"      DIVERGE {d['league']} {d['date']} {d['slug']}: "
              f"pmus->{d['pm_winner_key']} kalshi->{d['kal_winner_key']}{tag}")

    print("\n" + "-" * 78)
    if wt["compared"]:
        pct = 100.0 * wt.get("agree", 0) / wt["compared"]
        print(f"WEATHER VERDICT: {wt.get('agree', 0)}/{wt['compared']} joined settled buckets graded "
              f"IDENTICALLY ({pct:.1f}%); {wt.get('DIVERGE', 0)} divergences; "
              f"{wt.get('pm_vs_cli_MISMATCH', 0) + wt.get('kal_vs_cli_MISMATCH', 0)} CLI mismatches. "
              f"This IS a valid empirical invariant-#1 read for weather (per-day consistency gate applied).")
    else:
        print("WEATHER VERDICT: no settled joined weather pairs comparable yet.")
    if sp["compared"]:
        n_fut = sum(1 for r in sp["rows"] if r.get("status") in ("agree", "DISAGREE") and r.get("pm_end_future"))
        print(f"SPORTS: {sp['matched']}/{sp['compared']} agree, but {n_fut}/{sp['compared']} carry a future "
              f"pmus endDate (interim) - sports remains INCONCLUSIVE until pmus finalizes (~2wk).")
    print("-" * 78)
    return {"weather": wsm, "sports": sp}


# ============================================================================================
# SELF-TEST (offline, synthetic)
# ============================================================================================
def _selftest():
    print("settle_recon self-test (offline)")
    # pm_winner PRIMARY: marketSides is authoritative (label+price per side). The regression case is
    # a No-first outcomes array whose outcomePrices do NOT follow it (lax 2026-06-04 lt70f, live):
    # label-pairing said "Yes"; sides say No=1 -> "No" (== Kalshi == CLI). 286/286 live-verified.
    assert pm_winner({"closed": True, "outcomes": '["No","Yes"]', "outcomePrices": '["0","1"]',
                      "marketSides": [{"description": "Yes", "price": "0"},
                                      {"description": "No", "price": "1"}]})[0] == "No"
    assert pm_winner({"closed": True, "outcomes": '["Yes","No"]', "outcomePrices": '["1","0"]',
                      "marketSides": [{"description": "Yes", "price": "1"},
                                      {"description": "No", "price": "0"}]})[0] == "Yes"
    # sports: side label = description/team name (live case: outcomes-order said Zandschulp, sides say Griekspoor)
    assert pm_winner({"closed": True, "outcomes": '["Botic van de Zandschulp","Tallon Griekspoor"]',
                      "outcomePrices": '["1","0"]',
                      "marketSides": [{"description": "Tallon Griekspoor", "price": "1",
                                       "team": {"name": "Tallon Griekspoor"}},
                                      {"description": "Botic van de Zandschulp", "price": "0",
                                       "team": {"name": "Botic van de Zandschulp"}}]})[0] == "Tallon Griekspoor"
    # sides present but NOT cleanly graded (live/interim prices, or two winners) -> None
    assert pm_winner({"closed": True, "marketSides": [{"description": "Yes", "price": "0.42"},
                                                      {"description": "No", "price": "0.58"}]})[0] is None
    assert pm_winner({"closed": True, "marketSides": [{"description": "Yes", "price": "1"},
                                                      {"description": "No", "price": "1"}]})[0] is None
    # FALLBACK (no marketSides): label-pairing, best-effort
    assert pm_winner({"closed": True, "outcomes": '["Yes","No"]', "outcomePrices": '["1","0"]'})[0] == "Yes"
    assert pm_winner({"closed": True, "outcomes": '["Phillies","Jays"]', "outcomePrices": '["0","1"]'})[0] == "Jays"
    assert pm_winner({"closed": False, "outcomes": '["Yes","No"]', "outcomePrices": '["1","0"]'})[0] is None  # not closed
    assert pm_winner({"closed": True, "outcomes": '["Yes","No"]', "outcomePrices": '["1","1"]'})[0] is None   # two winners -> None
    assert pm_winner({"closed": True, "outcomes": '["No","Yes"]', "outcomePrices": '["0.01"]'})[0] is None    # ragged live-px array
    # kal_result: only finalized/settled with a clean yes/no
    assert kal_result({"status": "finalized", "result": "yes"}) == "yes"
    assert kal_result({"status": "active", "result": "yes"}) is None
    assert kal_result({"status": "settled", "result": ""}) is None
    # slug parsers
    assert parse_wx_slug("tc-temp-miahigh-2026-06-08-gte90lt91f") == ("mia", "2026-06-08", "gte90lt91f")
    assert parse_wx_slug("aec-mlb-lad-pit-2026-06-09") is None
    assert parse_sport_slug("aec-mlb-lad-pit-2026-06-09") == ("mlb", "lad", "pit", "2026-06-09")
    assert parse_sport_slug("aec-atp-romsaf-gioper-2026-06-09") == ("atp", "romsaf", "gioper", "2026-06-09")
    # bound parity (reuses colisted_map's verified maps): pm slug bound == kalshi bound when aligned
    assert pm_bounds("tc-temp-miahigh-2026-06-08-gte90lt91f") == kbounds({"floor_strike": 90, "cap_strike": 91}) == (90, 91)

    # --- pm_slug_for: EXACT inverse of pm_bounds (the enumeration join is identity-by-construction) ---
    assert pm_slug_for("mia", "2026-06-08", (90, 91)) == "tc-temp-miahigh-2026-06-08-gte90lt91f"
    for b in [(90, 91), (None, 63), (72, None)]:
        assert pm_bounds(pm_slug_for("sfo", "2026-06-09", b)) == b, b      # round-trip
    # composed with kbounds: a settled Kalshi tail/middle constructs the bounds-identical pm slug
    assert pm_slug_for("sfo", "2026-06-09", kbounds({"floor_strike": None, "cap_strike": 64})).endswith("-lt64f")
    assert pm_slug_for("sfo", "2026-06-09", kbounds({"floor_strike": 71, "cap_strike": None})).endswith("-gte72f")
    assert pm_slug_for("sfo", "2026-06-09", (None, None)) is None
    # bucket_hit: inclusive on both edges; open tails; None-safe
    assert bucket_hit((90, 91), 91) and bucket_hit((90, 91), 90) and not bucket_hit((90, 91), 92)
    assert not bucket_hit((90, 91), 89) and bucket_hit((None, 63), 63) and not bucket_hit((None, 63), 64)
    assert bucket_hit((72, None), 72) and not bucket_hit((72, None), 71)
    assert bucket_hit((90, 91), None) is None and bucket_hit((None, None), 90) is None
    # cli_finals: final = LAST-by-t value; revisions kept + flagged (e.g. MDW 06-09 87->88)
    L = [json.dumps({"t": 10, "station": "MDW", "report_date": "2026-06-09", "max": 87}),
         json.dumps({"t": 20, "station": "MDW", "report_date": "2026-06-09", "max": 88}),
         json.dumps({"t": 5, "station": "SFO", "report_date": "2026-06-09", "max": 68})]
    cf = cli_finals(L)
    assert cf[("MDW", "2026-06-09")] == {"final": 88, "values": [87, 88], "revised": True}
    assert cf[("SFO", "2026-06-09")] == {"final": 68, "values": [68], "revised": False}
    cf2 = cli_finals(reversed(L))                       # line order must not matter; t decides
    assert cf2[("MDW", "2026-06-09")]["final"] == 88 and cf2[("MDW", "2026-06-09")]["revised"]
    # kal_recorded_value: consensus across the day's buckets; conflict -> None + all values
    assert kal_recorded_value([{"expiration_value": "91.00"}, {"expiration_value": "91.00"}]) == (91.0, [91.0])
    assert kal_recorded_value([{"expiration_value": "91.00"}, {"expiration_value": "92.00"}]) == (None, [91.0, 92.0])
    assert kal_recorded_value([{}]) == (None, [])
    # iso_lag_h: Z + fractional-second stamps -> hours
    now = datetime.datetime(2026, 6, 9, 13, 30, tzinfo=datetime.timezone.utc).timestamp()
    assert iso_lag_h("2026-06-09T12:00:00Z", now) == 1.5
    assert iso_lag_h("2026-06-09T12:01:54.518555Z", now) == round((88 * 60 + 5.481445) / 3600, 2)
    assert iso_lag_h(None, now) is None

    # --- classify_day: the three-way day logic (agree / DIVERGE / multi-YES exclusion / pending) ---
    def B(bounds, pm, kal): return {"bounds": bounds, "pm_out": pm, "kal_result": kal}
    # clean day: winner agrees on both venues and matches the CLI
    d = [B((90, 91), "Yes", "yes"), B((88, 89), "No", "no")]
    f = classify_day(d, 91)
    assert [b["status"] for b in d] == ["agree", "agree"] and not f["pm_multi_yes"]
    assert [b["kal_vs_cli"] for b in d] == ["match", "match"] == [b["pm_vs_cli"] for b in d]
    # divergence: venues disagree; the CLI says Kalshi is right -> pm_vs_cli MISMATCH
    d = [B((90, 91), "Yes", "no")]
    classify_day(d, 92)
    assert d[0]["status"] == "DIVERGE" and d[0]["pm_vs_cli"] == "MISMATCH" and d[0]["kal_vs_cli"] == "match"
    # zero-YES day where the CLI lands in a listed bucket pmus called No -> DIVERGE + MISMATCH
    d = [B((90, 91), "No", "yes")]
    classify_day(d, 91)
    assert d[0]["status"] == "DIVERGE" and d[0]["pm_vs_cli"] == "MISMATCH" and d[0]["kal_vs_cli"] == "match"
    # multi-YES day (impossible -> interim): excluded from tally, pm_vs_cli suppressed
    d = [B((90, 91), "Yes", "yes"), B((88, 89), "Yes", "no")]
    f = classify_day(d, 91)
    assert f["pm_multi_yes"] and [b["status"] for b in d] == ["pm_inconsistent", "pm_inconsistent"]
    assert [b["pm_vs_cli"] for b in d] == [None, None] and [b["kal_vs_cli"] for b in d] == ["match", "match"]
    # pending: pmus open/interim or Kalshi not yet finalized
    d = [B((90, 91), None, "yes"), B((88, 89), "No", None)]
    classify_day(d, None)
    assert [b["status"] for b in d] == ["pm_unresolved", "kal_unsettled"]
    assert [b["kal_vs_cli"] for b in d] == [None, None]                     # no truth -> no three-way
    # summarize_weather over a synthetic run: tallies + exclusions land in the right cells
    wx = {"days": [{"city": "mia", "date": "2026-06-08", "truth": 91, "truth_src": "cli.jsonl",
                    "cli_revised": False, "cli_values": [91], "kal_value": 91.0, "kal_values_all": [91.0],
                    "kal_yes_tickers": ["K1"], "n_kal_buckets": 2, "pm_not_listed": ["x"],
                    "pm_fetch_errors": [], "settlement_ts": "2026-06-09T12:01:54Z", "read_lag_h": 37.0,
                    "buckets": [], "pm_yes_count": 1, "pm_multi_yes": False}],
          "read_ts": 0, "cutoff": "x", "fetch_errors": []}
    wx["days"][0]["buckets"] = [
        {"bounds": (90, 91), "pm_out": "Yes", "kal_result": "yes", "slug": "s1", "pm_oc": None, "pm_op": None,
         "kalshi": "K1", "status": "agree", "kal_vs_cli": "match", "pm_vs_cli": "match", "cli_hit": True},
        {"bounds": (88, 89), "pm_out": "No", "kal_result": "yes", "slug": "s2", "pm_oc": None, "pm_op": None,
         "kalshi": "K2", "status": "DIVERGE", "kal_vs_cli": "MISMATCH", "pm_vs_cli": "match", "cli_hit": False}]
    sm = summarize_weather(wx)
    assert sm["tally"]["compared"] == 2 and sm["tally"]["agree"] == 1 and sm["tally"]["DIVERGE"] == 1
    assert sm["tally"]["pm_not_listed"] == 1 and sm["tally"]["kalval_vs_cli_match"] == 1
    assert len(sm["divergences"]) == 1 and len(sm["cli_mismatches"]) == 1
    assert day_state(wx["days"][0]) == "DIVERGE"
    # --- econ reconciliation (pure): cumulative >=twin, exact-bucket print-identity, FOMC categorical ---
    def _pm(slug, yes):  # synthetic settled pmus market (marketSides-graded)
        return {"slug": slug, "closed": True,
                "marketSides": [{"description": "Yes", "price": "1" if yes else "0"},
                                {"description": "No", "price": "0" if yes else "1"}]}
    def _k(floor=None, res="yes", sub=None):
        return {"status": "settled", "result": res, "floor_strike": floor, "yes_sub_title": sub}
    # (1) cumulative twin: pmus >=4.2 YES <-> Kalshi Above-4.1 (floor 4.1) — agree when both YES
    ge = [_pm("urc-us-seasonadj-gte-june-2026-07-02-atl4pt2", True)]
    r = econ_reconcile("urc", 0.1, ge, {4.1: _k(4.1, "yes")}, {})
    assert r == [{"kind": "ge_twin", "key": 4.2, "pm": "yes", "kal": "yes", "status": "agree"}], r
    assert econ_reconcile("urc", 0.1, ge, {4.1: _k(4.1, "no")}, {})[0]["status"] == "DIVERGE"   # twin disagrees
    assert econ_reconcile("urc", 0.1, ge, {}, {})[0]["status"] == "no_twin"                     # twin not listed
    # (2) exact-bucket CPI: print-identity only (pmus exact winner == Kalshi implied print)
    cp = [_pm("cpic-uscpi-apr2026yoy-2026-05-12-3pt8pct", True),
          _pm("cpic-uscpi-apr2026yoy-2026-05-12-3pt9pct", False)]
    kladder = {3.7: _k(3.7, "yes"), 3.8: _k(3.8, "no")}            # Above-3.7 YES, Above-3.8 NO -> print 3.8
    rr = econ_reconcile("cpic", 0.1, cp, kladder, {})
    assert rr == [{"kind": "eq_print", "key": "print", "pm": 3.8, "kal": 3.8, "status": "agree"}], rr
    assert kal_implied_print(list(kladder.values())) == (3.8, 3.7, 3.8)
    # (3) FOMC categorical: pmus 'maintains' YES <-> Kalshi 'Fed maintains rate' YES
    fo = [_pm("rdc-usfed-fomc-2026-04-29-maintains", True), _pm("rdc-usfed-fomc-2026-04-29-cut25bps", False)]
    rc = econ_reconcile("rdc", None, fo, {}, {"fed maintains rate": _k(None, "yes", "Fed maintains rate"),
                                              "cut 25bps": _k(None, "no", "Cut 25bps")})
    assert rc == [{"kind": "cat", "key": "maintains", "pm": "maintains", "kal": "fed maintains rate", "status": "agree"}], rc
    print("OK - pm_winner order-independence + ragged guard, kal_result gating, slug parse, bound parity,")
    print("     pm_slug_for inverse join, bucket_hit, cli_finals last-by-t, kal_recorded_value, iso_lag_h,")
    print("     classify_day three-way (agree/DIVERGE/multi-YES exclusion/pending), summarize_weather tallies,")
    print("     econ_reconcile (>=twin agree/DIVERGE/no_twin, exact-bucket print-identity, FOMC categorical)")


# ============================================================================================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--no-sports", action="store_true")
    ap.add_argument("--archive", default=ARCHIVE)
    ap.add_argument("--days-back", type=int, default=12, help="enumerate settled weather this many days back")
    ap.add_argument("--no-nws", action="store_true", help="skip the NWS CLI version-walk fallback")
    ap.add_argument("--max-pairs", type=int, default=60, help="cap sports pmus fetches (keeps live run fast)")
    ap.add_argument("--no-econ", action="store_true", help="skip the econ settled-release reconciliation")
    ap.add_argument("--econ-only", action="store_true", help="ONLY run the econ reconciliation (fast)")
    a = ap.parse_args()

    if a.selftest:
        _selftest(); return

    if a.econ_only:
        report_econ(recon_econ()); return

    slugs = archive_slugs(a.archive)
    print(f"co-listed slugs from live archive: {len(slugs)} "
          f"(weather={sum(s.startswith('tc-') for s in slugs)}, sports={sum(s.startswith('aec-') for s in slugs)})")
    print(f"weather: enumerating settled co-listings {a.days_back} days back "
          f"(constructed bounds-identical pmus twins of settled Kalshi buckets) + archive slugs\n")
    wx = recon_weather(slugs, archive=a.archive, days_back=a.days_back, use_nws=not a.no_nws)
    sp = (recon_sports(slugs, a.max_pairs) if not a.no_sports
          else {"rows": [], "compared": 0, "matched": 0, "mismatched": 0, "divergences": [],
                "n_slugs": sum(s.startswith('aec-') for s in slugs)})
    report(wx, sp)
    if not a.no_econ:
        report_econ(recon_econ())

    # evidence dump: full day rows; raw venue objects kept ONLY where something needs proving
    # (any non-agree status, any CLI mismatch, any inconsistent day) - evidence discipline
    os.makedirs(DATA_DIR, exist_ok=True)
    out = os.path.join(DATA_DIR, f"settle_recon_weather_{time.strftime('%Y%m%d-%H%M%S')}.json")
    dump_days = []
    for day in wx["days"]:
        dd = {k: v for k, v in day.items() if k != "buckets"}
        dd["buckets"] = []
        keep_raw_day = day["pm_multi_yes"]
        for b in day["buckets"]:
            bb = {k: v for k, v in b.items() if not k.startswith("_")}
            if keep_raw_day or b["status"] not in ("agree",) or "MISMATCH" in (b.get("kal_vs_cli") or "",
                                                                               b.get("pm_vs_cli") or ""):
                bb["_pm_raw"], bb["_k_raw"] = b.get("_pm_raw"), b.get("_k_raw")
            dd["buckets"].append(bb)
        dump_days.append(dd)
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"run": {"ts": wx["read_ts"], "days_back": a.days_back, "nws": not a.no_nws,
                           "archive": a.archive},
                   "weather_days": dump_days, "sports_rows": sp["rows"]}, f, indent=1, default=str)
    print(f"\nevidence dump -> {out}")


if __name__ == "__main__":
    main()
