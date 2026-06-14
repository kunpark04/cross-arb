"""scripts/settlement_identity.py - REUSABLE settlement-identity GATE (invariant #1, programmatic).

Where verify_settlement.py / verify_sports_settlement.py / verify_econ_settlement.py PRINT both venues'
rules side-by-side for a HUMAN to eyeball, this module returns a STRUCTURED VERDICT per co-listed pair:
`settlement_identity(pm_market, kalshi, cat) -> {status, dims, reasons}` with status in
IDENTICAL | DIVERGENT | NEEDS_MANUAL. It is the "every discovered market gets gated" capability the live
bot needs before sizing into a "locked" pair.

THE KEY DESIGN PRINCIPLE (owner's refinement of decision 0001, 2026-06-13)
-------------------------------------------------------------------------
A cross-venue arb is settlement-clean iff both venues grade the same event off the **IDENTICAL OUTCOME**,
NOT the identical source STRING. Two different *reporters* of the same deterministic event are
settlement-identical: a World Cup match graded via ESPN (Kalshi) vs via FIFA's official result (pmus) is
the SAME winner regardless, so a source-STRING difference there is NOT a divergence. Source-string identity
is required ONLY where different sources yield different underlying *numbers* (WEATHER: NWS CLI vs
METAR/Wunderground genuinely read different temperatures; ECON: the settling government AGENCY). For SPORTS
the divergence lives in the **outcome RULES** — the result-timing basis (soccer: 90-min/regulation vs incl.
extra-time/penalties), the void/postpone handling (the reschedule WINDOW + the fallback price), and the
outcome COUNT (2-way vs 3-way) — never the reporter's name. This REFINES 0001 ("same named number"): 0001's
rule is exactly right for weather/econ and exactly wrong for sports source strings.

CONSERVATIVE DISCIPLINE (load-bearing — the no-false-positive invariant, lesson L1)
-----------------------------------------------------------------------------------
Default is NEEDS_MANUAL. Return IDENTICAL only when EVERY outcome-determining dimension for the category
PROVABLY matches; return DIVERGENT only when a dimension PROVABLY differs; anything unextractable/uncertain
stays NEEDS_MANUAL (NEVER silently IDENTICAL). A false IDENTICAL is a both-legs-loss (L1/L2). DIVERGENT
beats IDENTICAL when both fire (a proven conflict is decisive); NEEDS_MANUAL is the floor, never overriding
a proven IDENTICAL/DIVERGENT but catching every gap. This mirrors L17: the underlying boundary/inequality
conventions this leans on were live-reverified (colisted_map), but the rules-text extractors here are
HEURISTIC — when they can't prove a dimension, they must abstain, not guess.

Per-category outcome-determining dimensions (checked EXACTLY these):
  weather: STATION (a SPECIFIC id — ICAO code/landmark, never the generic word "airport") + SOURCE is CLI
           (not METAR/ASOS) + BOUNDARY (colisted_map pm_bounds/kbounds inclusive-[lo,hi] equality). The
           8AM-vs-11AM CLI read-timing residual -> a NEEDS_MANUAL note, never auto-DIVERGENT (a downward-
           correction tail, not a source split).
  sports : EVENT (teams+date sanity, already joined) + RESULT-TIMING basis + VOID/POSTPONE (reschedule
           window + fallback) + OUTCOME-COUNT (2 vs 3-way). SOURCE STRING IS IGNORED (owner's refinement).
  econ   : RELEASE (colisted_map econ_parse family+period) + AGENCY (BLS/BEA/Fed) + BOUNDARY/INEQUALITY
           (colisted_map econ_twin, the 0013 grid-step-twin logic).

  python scripts/settlement_identity.py --selftest   # offline synthetic-dict proofs
  python scripts/settlement_identity.py --audit       # run the gate over the live co-listed universe

READ-ONLY, public APIs. A verdict is a GATE input, not a trade decision — the bot still decides size.
"""
import os, sys, re, argparse, collections
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bot"))
from colisted_map import (get, build_colisted_map, pm_catalog, econ_parse, econ_twin, ECON, _FEDLBL,  # noqa: E402
                          pm_bounds, kbounds, KAL)
from verify_settlement import fuzzy_rule_fields, kalshi_detail           # weather rule-field extractor + detail pull
from verify_sports_settlement import k_text                              # joins Kalshi rules fields -> one str

IDENTICAL, DIVERGENT, NEEDS_MANUAL = "IDENTICAL", "DIVERGENT", "NEEDS_MANUAL"

# A weather settlement SOURCE is one of these. CLI (the NWS Climatological Report) is the clean basis both
# venues must name; METAR/ASOS/Wunderground are DIFFERENT thermometers (a real number divergence, L2).
_CLI = re.compile(r"climatolog\w*|\bCLI\b", re.I)
_NONCLI_WX = re.compile(r"\bMETAR\b|\bASOS\b|wunderground|weather underground", re.I)
# A SPECIFIC station identifier — an ICAO airport code (KNYC/KSFO/...) or a named landmark (central park,
# laguardia, o'hare, midway). NOT the generic words "airport"/"NWS"/"national weather service": two
# DIFFERENT cities both say "airport", so matching on the generic word is a false station-identity (L1).
# pmus reliably names the code in its description; Kalshi names it only sometimes (e.g. NYC/MDW) — when a
# venue names no specific identifier the station dimension abstains (NEEDS_MANUAL), never asserts a match.
_STATION_SPECIFIC = re.compile(r"\bK[A-Z]{3}\b|central park|laguardia|o'?hare|\bmidway\b", re.I)

# Sports RESULT-TIMING basis. The settlement-relevant split: does the result include extra time / penalties,
# or stop at regulation (90 min / full time)? Both venues phrase regulation as "90 minutes (plus stoppage)"
# / "full time" / "regulation"; the EXTENDED basis as "extra time" / "penalties" / "shootout" / "aggregate".
# (Verified live 2026-06-13: Kalshi WC "after 90 minutes plus stoppage time (does not include extra time or
# penalties)" vs pmus "determined at full time (90 minutes plus stoppage time)" -> both REGULATION.)
_TIMING_REG = re.compile(r"90\s*min|full[\s-]*time|regulation|stoppage time", re.I)
_TIMING_EXT = re.compile(r"extra[\s-]*time|penalt\w*|shoot[\s-]*out|aggregate|golden goal|overtime", re.I)
# A "does not include extra time/penalties" clause names the extended terms only to EXCLUDE them — that's
# still a REGULATION basis, so detect the negation and don't let _TIMING_EXT mislabel it as extended.
_TIMING_NEG = re.compile(r"(?:not include|does not include|excluding|without)\s+[^.]*?(?:extra[\s-]*time|penalt|overtime)", re.I)

# Void/postpone RESCHEDULE WINDOW. The known sports risk (CLAUDE.md core finding): Kalshi MLB waits <=2 DAYS
# then voids to a fair price; pmus waits <=2 WEEKS then settles last-traded. World Cup Kalshi is <=2 WEEKS
# (verified live) — so the window is NOT a per-venue constant; it must be EXTRACTED per market, never assumed.
_WINDOW = re.compile(r"(?:within|over|after)\s+(?:(\d+)\s*)?(day|week)s?\s*(?:away)?", re.I)
# Void FALLBACK price (how the un-replayed game settles): differs across venues even when the window matches
# (Kalshi "fair price" vs pmus "last-traded") -> the legs don't net -> a settle-time divergence.
_FB_FAIR = re.compile(r"fair (?:market )?price", re.I)
_FB_LAST = re.compile(r"last[\s-]*traded|last fair market|last[\s-]*price", re.I)
_FB_5050 = re.compile(r"50[\s\-/]*50|resolve 50", re.I)
_FB_ZERO = re.compile(r"settle to \$?0|resolve to \$?0\.00|\$0\.00", re.I)

# Econ source AGENCY. Identity MATTERS for econ (a different agency = a different print). Canonical, usually
# matches, but verify: U-3/CPI/NFP -> BLS; GDP -> BEA; Fed -> Federal Reserve.
_AGENCY = {"bls": re.compile(r"bureau of labor statistics|\bBLS\b", re.I),
           "bea": re.compile(r"bureau of economic analysis|\bBEA\b", re.I),
           "fed": re.compile(r"federal reserve|\bFOMC\b|\bFed\b", re.I)}
_ECON_AGENCY = {"cpic": "bls", "urc": "bls", "nfpc": "bls", "gdpc": "bea", "rdc": "fed"}


def _series_ticker(kalshi_market):
    """Kalshi SERIES ticker from a market ticker: 'KXWCGAME-26JUN27CODUZB-UZB' -> 'KXWCGAME'."""
    return str(kalshi_market.get("ticker", "")).split("-")[0]


def _series_sources(series_ticker, _cache={}):
    """The SERIES `settlement_sources` [{name,url}] (the canonical reporter list, e.g. weather CLI / econ
    agency). Cached per series; read-only. Returns [] on any fetch miss (-> the caller abstains)."""
    if not series_ticker:
        return []
    if series_ticker not in _cache:
        d = get(f"https://api.elections.kalshi.com/trade-api/v2/series/{series_ticker}")
        ser = (d.get("series") if isinstance(d, dict) else None) or (d if isinstance(d, dict) else {})
        _cache[series_ticker] = ser.get("settlement_sources") or []
    return _cache[series_ticker]


def _kalshi_one(kalshi):
    """A category may pass ONE Kalshi market (weather/econ) or a LIST (sports: two team legs). Normalize to
    a representative market for series-level reads (rules text is per-market; series_sources is per-series)."""
    if isinstance(kalshi, list):
        return kalshi[0] if kalshi else {}
    return kalshi or {}


def _pm_text(pm_market):
    """pmus settlement text = `description` (primary) + any settlement-keyed fields (robust to schema)."""
    desc = str(pm_market.get("description") or "")
    extra = " ".join(fuzzy_rule_fields(pm_market).values())
    return (desc + " " + extra).strip()


# ---------------------------------------------------------------------------------------------------------
# WEATHER: station + CLI source + bucket boundary must ALL provably match.
# ---------------------------------------------------------------------------------------------------------
def _weather(pm_market, kalshi):
    km = _kalshi_one(kalshi)
    dims, reasons = {}, []
    statuses = []
    k_rules = k_text(km)
    k_sources = " ".join(s.get("name", "") for s in _series_sources(_series_ticker(km)))
    k_text_all = (k_rules + " " + k_sources).strip()
    p_text = _pm_text(pm_market)

    # (a) STATION — both venues must name the same SPECIFIC station (ICAO code or landmark), NOT the generic
    #     word "airport" (which any two different-city markets share -> a false match, L1). When a venue names
    #     no specific identifier (Kalshi often gives only the city), abstain: NEEDS_MANUAL, never IDENTICAL.
    k_st = {m.group(0).lower() for m in _STATION_SPECIFIC.finditer(k_text_all)}
    p_st = {m.group(0).lower() for m in _STATION_SPECIFIC.finditer(p_text)}
    dims["station"] = {"kalshi": sorted(k_st), "pm": sorted(p_st)}
    if k_st and p_st:
        if k_st & p_st:
            statuses.append(IDENTICAL); reasons.append(f"station match (specific id): {sorted(k_st & p_st)}")
        else:
            statuses.append(DIVERGENT); reasons.append(f"STATION differs: Kalshi {sorted(k_st)} vs pmus {sorted(p_st)}")
    else:
        statuses.append(NEEDS_MANUAL); reasons.append(f"station not specifically named on >=1 venue (Kalshi {sorted(k_st)}, pmus {sorted(p_st)}) — generic 'airport' is not station-identity; rely on the primary-source station check (verify_settlement.py)")

    # (b) SOURCE — both must be the NWS CLI; one CLI vs one METAR/ASOS = a real number divergence (L2).
    k_cli, p_cli = bool(_CLI.search(k_text_all)), bool(_CLI.search(p_text))
    k_non, p_non = bool(_NONCLI_WX.search(k_text_all)), bool(_NONCLI_WX.search(p_text))
    dims["source"] = {"kalshi_cli": k_cli, "pm_cli": p_cli, "kalshi_nonCLI": k_non, "pm_nonCLI": p_non}
    if (k_cli and p_non and not p_cli) or (p_cli and k_non and not k_cli):
        statuses.append(DIVERGENT); reasons.append("SOURCE differs: one venue grades on NWS CLI, the other on METAR/ASOS/Wunderground (different thermometers, L2)")
    elif k_cli and p_cli:
        statuses.append(IDENTICAL); reasons.append("source match: both NWS Climatological Report (CLI)")
    else:
        statuses.append(NEEDS_MANUAL); reasons.append(f"CLI source not provable both sides (Kalshi CLI={k_cli}, pmus CLI={p_cli})")

    # (c) BOUNDARY — reuse colisted_map's canonical inclusive [lo,hi] equality (the live-verified convention).
    pb, kb = pm_bounds(pm_market.get("slug")), kbounds(km)
    dims["boundary"] = {"pm": pb, "kalshi": kb}
    if pb == (None, None) or kb == (None, None):
        statuses.append(NEEDS_MANUAL); reasons.append(f"boundary not parseable (pm {pb}, kalshi {kb})")
    elif pb == kb:
        statuses.append(IDENTICAL); reasons.append(f"boundary match: inclusive {pb}")
    else:
        statuses.append(DIVERGENT); reasons.append(f"BOUNDARY differs: pm {pb} vs kalshi {kb} (would grade off different thresholds)")

    # 8AM-vs-11AM CLI read-timing residual: a NEEDS_MANUAL NOTE, never an auto-DIVERGENT (CLAUDE.md).
    reasons.append("note: residual 8AM-vs-11AM CLI read-timing on a downward boundary-day correction is NOT auto-flagged here — see settlement-verification.md")
    return _combine(statuses, dims, reasons)


# ---------------------------------------------------------------------------------------------------------
# SPORTS: event + timing-basis + void-handling + outcome-count must match. SOURCE STRING IGNORED.
# ---------------------------------------------------------------------------------------------------------
def _sports(pm_market, kalshi):
    kms = kalshi if isinstance(kalshi, list) else [kalshi]
    kms = [m for m in kms if m]
    dims, reasons = {}, []
    statuses = []
    k_all = " ".join(k_text(m) for m in kms)
    p_text = _pm_text(pm_market)

    # (a) EVENT — the pair is already identity-joined (colisted_map pick_game). Sanity-check the date agrees
    #     between the pm slug and the Kalshi tickers; a mismatch means the join bound the wrong game (L1).
    p_date = _slug_date(pm_market.get("slug"))
    k_dates = {d for d in (_ticker_date(m.get("ticker")) for m in kms) if d}
    dims["event_date"] = {"pm": p_date, "kalshi": sorted(k_dates)}
    if p_date and k_dates:
        if p_date in k_dates:
            statuses.append(IDENTICAL); reasons.append(f"event date match: {p_date}")
        else:
            statuses.append(DIVERGENT); reasons.append(f"EVENT DATE differs: pm {p_date} vs kalshi {sorted(k_dates)} (join bound the wrong game?)")
    else:
        statuses.append(NEEDS_MANUAL); reasons.append(f"event date not extractable both sides (pm {p_date}, kalshi {sorted(k_dates)})")

    # (b) SOURCE STRING — IGNORED by design (owner's refinement). Recorded for transparency, never scored:
    #     ESPN vs FIFA vs the league all report the SAME winner of a deterministic match.
    dims["source_IGNORED"] = {"kalshi": [s.get("name") for s in _series_sources(_series_ticker(kms[0]))] if kms else [],
                              "note": "source string deliberately NOT scored for sports (same reporter-independent outcome)"}

    # (c) RESULT-TIMING basis — regulation (90-min/full-time) vs extended (extra time/penalties).
    k_basis, p_basis = _timing_basis(k_all), _timing_basis(p_text)
    dims["result_timing"] = {"kalshi": k_basis, "pm": p_basis}
    if k_basis and p_basis:
        if k_basis == p_basis:
            statuses.append(IDENTICAL); reasons.append(f"result-timing basis match: both {k_basis}")
        else:
            statuses.append(DIVERGENT); reasons.append(f"RESULT-TIMING differs: Kalshi {k_basis} vs pmus {p_basis} (one settles at regulation, the other incl. extra time/penalties)")
    else:
        statuses.append(NEEDS_MANUAL); reasons.append(f"result-timing basis silent on >=1 venue (Kalshi {k_basis or 'silent'}, pmus {p_basis or 'silent'}) — verify rulebook")

    # (d) VOID/POSTPONE — reschedule WINDOW + fallback price. The known MLB-2d-vs-pmus-2wk risk.
    k_win, p_win = _resched_window(k_all), _resched_window(p_text)
    k_fb, p_fb = _void_fallback(k_all), _void_fallback(p_text)
    dims["void"] = {"kalshi_window_days": k_win, "pm_window_days": p_win, "kalshi_fallback": sorted(k_fb), "pm_fallback": sorted(p_fb)}
    if k_win is not None and p_win is not None:
        if k_win != p_win:
            statuses.append(DIVERGENT); reasons.append(f"VOID reschedule WINDOW differs: Kalshi {k_win}d vs pmus {p_win}d (a game replayed in the gap settles the legs OPPOSITELY — the known MLB risk)")
        elif k_fb and p_fb:
            if k_fb & p_fb:
                statuses.append(IDENTICAL); reasons.append(f"void match: same reschedule window ({k_win}d) and same fallback price {sorted(k_fb & p_fb)}")
            else:
                statuses.append(DIVERGENT); reasons.append(f"VOID fallback price differs: Kalshi {sorted(k_fb)} vs pmus {sorted(p_fb)} (same window, but the un-replayed game settles to different prices — legs don't net)")
        else:
            statuses.append(NEEDS_MANUAL); reasons.append(f"void window matches ({k_win}d) but fallback price not both-provable (Kalshi {sorted(k_fb) or 'silent'}, pmus {sorted(p_fb) or 'silent'}) — confirm the un-replayed-game price")
    else:
        statuses.append(NEEDS_MANUAL); reasons.append(f"void/postpone reschedule window silent on >=1 venue (Kalshi {k_win}, pmus {p_win}) — the void tail is the #1 sports risk, verify")

    # (e) OUTCOME-COUNT — 3-way (a SETTLEABLE draw/tie) vs 2-way. A structural mismatch grades incompatibly.
    #     Use STRUCTURE, not prose: a "tie -> settle 50-50" void clause is NOT a third outcome (baseball pmus
    #     says "tie or draw -> $0.50" with only 2 marketSides). pmus -> marketSides count; Kalshi -> a distinct
    #     "Tie" MARKET named in the rules (or a Tie leg in the bound set), not a bare "ends in a tie" clause.
    k_3, p_3 = _kalshi_three_way(k_all, kms), _pm_three_way(pm_market)
    dims["outcome_count"] = {"kalshi_3way": k_3, "pm_3way": p_3}
    if k_3 is not None and p_3 is not None:
        if k_3 == p_3:
            statuses.append(IDENTICAL); reasons.append(f"outcome-count match: both {'3-way (draw possible)' if k_3 else '2-way'}")
        else:
            statuses.append(DIVERGENT); reasons.append(f"OUTCOME-COUNT differs: Kalshi {'3-way' if k_3 else '2-way'} vs pmus {'3-way' if p_3 else '2-way'} (a draw settles incompatibly)")
    else:
        statuses.append(NEEDS_MANUAL); reasons.append("outcome-count not determinable on >=1 venue")
    return _combine(statuses, dims, reasons)


# ---------------------------------------------------------------------------------------------------------
# ECON: release + agency + boundary/inequality must match (the 0013 grid-step-twin logic, reused).
# ---------------------------------------------------------------------------------------------------------
def _econ(pm_market, kalshi):
    km = _kalshi_one(kalshi)
    dims, reasons = {}, []
    statuses = []
    p = econ_parse(pm_market.get("slug"))
    k_rules = k_text(km)
    k_sources = " ".join(s.get("name", "") for s in _series_sources(_series_ticker(km)))
    k_text_all = (k_rules + " " + k_sources).strip()

    # (a) RELEASE — econ_parse must recognize the pmus family+period (the SAME parser the matcher uses).
    dims["release"] = {"pm_parsed": p}
    if not p or not p.get("fam") or not p.get("period"):
        return {"status": NEEDS_MANUAL, "dims": dims,
                "reasons": ["pmus econ slug not parseable by econ_parse (family/period) -> cannot establish the release"]}
    fam = p["fam"]
    statuses.append(IDENTICAL); reasons.append(f"release: {fam} {p['period']}")

    # (b) AGENCY — both must name the canonical settling agency (BLS/BEA/Fed). A mismatch = different print.
    want = _ECON_AGENCY.get(fam)
    k_agency = {a for a, rx in _AGENCY.items() if rx.search(k_text_all)}
    p_agency = {a for a, rx in _AGENCY.items() if rx.search(_pm_text(pm_market))}
    dims["agency"] = {"expected": want, "kalshi": sorted(k_agency), "pm": sorted(p_agency)}
    if want and want in k_agency and (want in p_agency or not p_agency):
        # pmus econ descriptions sometimes omit the agency; Kalshi naming the canonical agency + pmus not
        # CONTRADICTING it is acceptable (the release identity already pins the print) -> still provable.
        statuses.append(IDENTICAL); reasons.append(f"agency match: {want.upper()}")
    elif (k_agency and want and want not in k_agency) or (p_agency and want and want not in p_agency):
        statuses.append(DIVERGENT); reasons.append(f"AGENCY differs from expected {want.upper()}: Kalshi {sorted(k_agency)}, pmus {sorted(p_agency)}")
    else:
        statuses.append(NEEDS_MANUAL); reasons.append(f"agency not provable (expected {want.upper() if want else '?'}, Kalshi {sorted(k_agency)}, pmus {sorted(p_agency)})")

    # (c) BOUNDARY/INEQUALITY — reuse econ_twin (0013): pmus '>=T' <-> Kalshi 'Above T-step' (identical on
    #     the print grid). Categorical (Fed) matches label-for-label. '<='-tails (opposite orientation) and
    #     point-buckets ('==') have NO settlement-identical cumulative twin -> NEEDS_MANUAL (not co-listable).
    step = ECON[fam][2]
    ineq = p.get("ineq")
    if ineq == "cat":
        want_label = _FEDLBL.get(p.get("label"))
        k_label = str(km.get("yes_sub_title", "")).lower()
        dims["boundary"] = {"kind": "categorical", "pm_label": p.get("label"), "want": want_label, "kalshi_label": k_label}
        if want_label and k_label and want_label == k_label:
            statuses.append(IDENTICAL); reasons.append(f"categorical match: {want_label!r}")
        elif want_label and k_label:
            statuses.append(DIVERGENT); reasons.append(f"CATEGORICAL label differs: pmus->{want_label!r} vs kalshi {k_label!r}")
        else:
            statuses.append(NEEDS_MANUAL); reasons.append(f"categorical label not resolvable (pmus {p.get('label')!r}, kalshi {k_label!r})")
    elif ineq == ">=":
        twin = econ_twin(p["thr"], step)
        k_floor = km.get("floor_strike")
        k_strict = str(km.get("strike_type", "")).lower() in ("greater", "greater_or_equal")
        dims["boundary"] = {"kind": "cumulative", "pm_ineq": ">=", "pm_thr": p["thr"], "step": step,
                            "identical_twin_floor": twin, "kalshi_floor": k_floor, "kalshi_strike_type": km.get("strike_type")}
        if k_floor is None:
            statuses.append(NEEDS_MANUAL); reasons.append("Kalshi market has no floor_strike — cannot confirm the cumulative twin")
        elif not k_strict:
            statuses.append(NEEDS_MANUAL); reasons.append(f"Kalshi strike_type={km.get('strike_type')!r} not the expected strict 'greater' — twin inequality unconfirmed (0013)")
        elif round(float(k_floor), 6) == twin:
            statuses.append(IDENTICAL); reasons.append(f"boundary match: pmus '>= {p['thr']}' == Kalshi 'Above {twin}' on the {step} grid (identical twin, 0013)")
        else:
            statuses.append(DIVERGENT); reasons.append(f"BOUNDARY differs: pmus '>= {p['thr']}' identical twin is floor {twin}, but Kalshi floor is {k_floor} (off by >=1 grid bucket -> the P(print==T) phantom, 0013)")
    elif ineq == "<=":
        dims["boundary"] = {"kind": "tail_le", "pm_ineq": "<="}
        statuses.append(NEEDS_MANUAL); reasons.append("pmus '<=T' tail: pmus-YES == Kalshi-NO (OPPOSITE orientation) — not a same-orientation identical pair (0013)")
    else:  # "=="
        dims["boundary"] = {"kind": "point", "pm_ineq": "=="}
        statuses.append(NEEDS_MANUAL); reasons.append("pmus point bucket ('exactly X'): no cumulative Kalshi twin (0013) — not co-listable")
    return _combine(statuses, dims, reasons)


# ---------------------------------------------------------------------------------------------------------
# Shared rules-text extractors (sports) + the verdict combiner.
# ---------------------------------------------------------------------------------------------------------
def _timing_basis(text):
    """Result-timing basis -> 'regulation' | 'extended' | None (silent). A 'does NOT include extra time/
    penalties' clause is REGULATION even though it names the extended terms (detect the negation)."""
    t = text or ""
    neg = bool(_TIMING_NEG.search(t))
    ext = bool(_TIMING_EXT.search(t)) and not neg
    reg = bool(_TIMING_REG.search(t))
    if ext:
        return "extended"
    if reg:
        return "regulation"
    return None


def _resched_window(text):
    """Reschedule window in DAYS (7*weeks) or None if no window is stated. 'within/over/after N day|week[s]';
    a bare 'two weeks' (no leading digit) maps via the number word. Returns the day-count for comparison."""
    t = (text or "").lower()
    # normalize the common spelled small numbers that precede day/week
    t = re.sub(r"\bone\b", "1", t); t = re.sub(r"\btwo\b", "2", t); t = re.sub(r"\bthree\b", "3", t)
    m = _WINDOW.search(t)
    if not m:
        return None
    n = int(m.group(1)) if m.group(1) else 1
    return n * (7 if m.group(2).startswith("week") else 1)


def _void_fallback(text):
    """The set of fallback-price kinds named for an un-replayed/cancelled game: {'fair','last','5050','zero'}."""
    t = text or ""
    out = set()
    if _FB_FAIR.search(t): out.add("fair")
    if _FB_LAST.search(t): out.add("last")
    if _FB_5050.search(t): out.add("5050")
    if _FB_ZERO.search(t): out.add("zero")
    return out


def _pm_three_way(pm_market):
    """pmus outcome-count from STRUCTURE: marketSides count (3+ -> 3-way draw, 2 -> 2-way). A 'tie/draw ->
    settle 50-50' clause with only 2 sides is 2-way, NOT 3-way (baseball). marketSides is authoritative;
    fall back to '3 possible outcomes' prose only when marketSides is absent."""
    sides = [s for s in (pm_market.get("marketSides") or []) if (s.get("team") or {}).get("name") or s.get("title")]
    if len(sides) >= 3:
        return True
    if len(sides) == 2:
        return False
    t = str(pm_market.get("description") or "").lower()
    if "three possible outcome" in t or "three outcomes" in t:
        return True
    if "two possible outcome" in t or "two outcomes" in t:
        return False
    return None


def _kalshi_three_way(text, markets):
    """Kalshi outcome-count: 3-way iff a DISTINCT 'Tie'/'Draw' MARKET exists — a 'Tie'/'Draw' yes_sub_title
    among the bound legs, or the rules naming a separate tie market ("the market called 'Tie'"). A bare
    "if the game ends in a tie ..." 50-50-style clause is NOT a third settleable outcome -> not 3-way.
    (colisted_map binds only the two TEAM legs, so the rules-text signal is the primary one in --audit.)"""
    subs = " ".join(str(m.get("yes_sub_title") or "").lower() for m in (markets or []))
    if re.search(r"\b(tie|draw)\b", subs):
        return True
    t = (text or "").lower()
    if re.search(r'market called\s*["\']?(tie|draw)|["\'](tie|draw)["\']?\s*market|(tie|draw)\s*market resolves', t):
        return True
    return None


def _slug_date(slug):
    m = re.search(r"(\d{4}-\d{2}-\d{2})", str(slug or ""))
    return m.group(1) if m else None


def _ticker_date(ticker):
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})", str(ticker or ""))
    if not m:
        return None
    mons = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
    return f"20{m.group(1)}-{mons.index(m.group(2)) + 1:02d}-{m.group(3)}"


def _combine(statuses, dims, reasons):
    """Verdict combiner enforcing the conservative discipline: DIVERGENT if ANY dimension provably differs
    (a proven conflict is decisive); else NEEDS_MANUAL if ANY dimension is uncertain (the floor — never
    silently IDENTICAL on a gap); IDENTICAL only when EVERY scored dimension provably matched."""
    if DIVERGENT in statuses:
        status = DIVERGENT
    elif NEEDS_MANUAL in statuses or not statuses:
        status = NEEDS_MANUAL
    else:
        status = IDENTICAL
    return {"status": status, "dims": dims, "reasons": reasons}


def settlement_identity(pm_market, kalshi, cat):
    """Programmatic settlement-identity GATE for one co-listed pair.

    pm_market : the pmus market dict (carries `slug` + `description`).
    kalshi    : the Kalshi market dict (weather/econ) OR list of the two team-leg dicts (sports). Each
                carries rules_primary/rules_secondary/strike_type/floor_strike/cap_strike/yes_sub_title.
    cat       : 'weather' | 'sports' | 'econ'.

    Returns {"status": IDENTICAL|DIVERGENT|NEEDS_MANUAL, "dims": {...}, "reasons": [str]}. Default
    NEEDS_MANUAL — IDENTICAL requires EVERY outcome-determining dimension to PROVABLY match (L1)."""
    c = (cat or "").lower()
    if c == "weather":
        return _weather(pm_market, kalshi)
    if c == "sports":
        return _sports(pm_market, kalshi)
    if c == "econ":
        return _econ(pm_market, kalshi)
    return {"status": NEEDS_MANUAL, "dims": {}, "reasons": [f"unknown category {cat!r} — cannot gate"]}


# =========================================================================================================
# SELFTEST (offline, synthetic dicts) — proves the verdict + the owner's ESPN-vs-FIFA refinement.
# =========================================================================================================
def _selftest():
    print("settlement_identity self-test (offline)")

    # ---- WEATHER ----
    # same station (KNYC/Central Park) + both CLI + identical boundary -> IDENTICAL
    pm_wx = {"slug": "tc-temp-nychigh-2026-06-13-gte84lt85f",
             "description": "highest temperature at Central Park (KNYC) ... National Weather Service's Climatological Report (Daily) ... verified from NWS Climatological Report."}
    k_wx = {"ticker": "KXHIGHNY-26JUN13-B84", "floor_strike": 84, "cap_strike": 85,
            "rules_primary": "If the high temperature at the Central Park (KNYC) station per the National Weather Service Climatological Report (CLI) is 84 to 85 ...",
            "yes_sub_title": "84 to 85"}
    r = settlement_identity(pm_wx, k_wx, "weather")
    assert r["status"] == IDENTICAL, ("weather same station+CLI+boundary -> IDENTICAL", r)

    # CLI (Kalshi) vs METAR (pmus) -> DIVERGENT (source identity MATTERS for weather)
    pm_metar = dict(pm_wx, description="highest temperature at Central Park (KNYC) per the METAR/ASOS observation")
    r = settlement_identity(pm_metar, k_wx, "weather")
    assert r["status"] == DIVERGENT and any("METAR" in s or "thermometer" in s for s in r["reasons"]), ("weather CLI-vs-METAR -> DIVERGENT", r)

    # boundary mismatch (pm 84-85 vs kalshi 85-86) -> DIVERGENT
    r = settlement_identity(pm_wx, dict(k_wx, floor_strike=85, cap_strike=86), "weather")
    assert r["status"] == DIVERGENT and any("BOUNDARY" in s for s in r["reasons"]), ("weather boundary mismatch -> DIVERGENT", r)

    # station unextractable on pmus -> NEEDS_MANUAL (never silently IDENTICAL)
    r = settlement_identity(dict(pm_wx, description="some temperature market, source unstated"), k_wx, "weather")
    assert r["status"] == NEEDS_MANUAL, ("weather missing station -> NEEDS_MANUAL", r)

    # L1 REGRESSION: the generic word "airport" must NOT count as a station match — Kalshi names only the
    # CITY ("San Francisco ... airport") with no ICAO code, pmus names KSFO. Two different cities both say
    # "airport", so a match on it is a false station-identity. This must be NEEDS_MANUAL (was a false
    # IDENTICAL on 'airport' caught in the live --audit self-review), NOT IDENTICAL.
    pm_sfo = {"slug": "tc-temp-sfohigh-2026-06-13-gte74lt75f",
              "description": "highest temperature at San Francisco International Airport (KSFO) ... National Weather Service's Climatological Report (Daily) ... NWS Climatological Report."}
    k_sfo = {"ticker": "KXHIGHTSFO-26JUN13-B74", "floor_strike": 74, "cap_strike": 75, "yes_sub_title": "74 to 75",
             "rules_primary": "If the maximum temperature recorded at San Francisco for Jun 13, 2026 per the National Weather Service's Climatological Report (Daily) is 74 to 75, then Yes.",
             "rules_secondary": "Resolves off the airport observation."}
    r = settlement_identity(pm_sfo, k_sfo, "weather")
    assert r["status"] == NEEDS_MANUAL, ("weather generic-'airport' must NOT be IDENTICAL (no specific station id on Kalshi)", r["status"], r["dims"]["station"], r["reasons"])
    assert r["dims"]["station"]["kalshi"] == [], ("'airport' must not register as a Kalshi station id", r["dims"]["station"])

    # ---- SPORTS (the owner's refinement is the headline) ----
    # THE OWNER'S CASE: same game, DIFFERENT source STRING (ESPN on Kalshi vs FIFA on pmus), every
    # outcome-determining dim aligned (regulation timing, same reschedule window AND fallback, 3-way)
    # -> IDENTICAL. The differing reporter name must NOT block IDENTICAL (source is reporter-independent).
    pm_soc = {"slug": "atc-fwc-ger-cuw-2026-06-14-ger",
              "marketSides": [{"team": {"name": "Germany"}}, {"team": {"name": "Curacao"}}, {"title": "Draw"}],
              "description": ("World Cup event between Germany and Curacao, scheduled for 2026-06-14. Three possible outcomes: "
                              "Germany wins, Curacao wins, or draw. The result is determined at full time (90 minutes plus stoppage time). "
                              "Settled per FIFA official result. If postponed, rescheduled; if not within two weeks, settle at last-traded prices.")}
    k_soc = [{"ticker": "KXWCGAME-26JUN14GERCUW-GER",
              "rules_primary": "If Germany wins the Germany vs Curacao FIFA World Cup soccer game scheduled for Jun 14, 2026 after 90 minutes plus stoppage time (does not include extra time or penalties), then Yes. Result per ESPN.",
              "rules_secondary": "If the game ends in a tie, the market called 'Tie' resolves to Yes. If the game is cancelled or rescheduled to over two weeks away, the market resolves to the last-traded price.",
              "yes_sub_title": "Germany"},
             {"ticker": "KXWCGAME-26JUN14GERCUW-TIE", "yes_sub_title": "Tie", "rules_primary": "the Tie market"}]
    r = settlement_identity(pm_soc, k_soc, "sports")
    assert r["status"] == IDENTICAL, ("sports ESPN-vs-FIFA, all outcome-dims aligned -> IDENTICAL (source string ignored)", r["status"], r["reasons"])
    assert r["dims"]["result_timing"] == {"kalshi": "regulation", "pm": "regulation"}, ("both regulation", r["dims"]["result_timing"])
    assert r["dims"]["outcome_count"] == {"kalshi_3way": True, "pm_3way": True}, ("both 3-way", r["dims"]["outcome_count"])
    # the source dim is RECORDED (ESPN vs FIFA) but explicitly NOT scored — IDENTICAL despite the mismatch
    assert "source_IGNORED" in r["dims"] and r["dims"]["source_IGNORED"]["kalshi"], "sports source must be recorded as IGNORED"

    # Void FALLBACK PRICE conflict is a REAL settle-time divergence even when the source+timing+window match:
    # the actual live WC texts have Kalshi 'fair price' vs pmus 'last-traded' — proven different -> DIVERGENT
    # (not a source issue; the un-replayed game settles the legs to different prices). This is correct, and is
    # a genuine finding about real World Cup pairs (see self-review): they are NOT source-clean-therefore-OK.
    k_soc_fair = [dict(k_soc[0], rules_secondary="If a tie, the 'Tie' market resolves Yes. If cancelled or rescheduled over two weeks away, the market resolves to a fair price."), k_soc[1]]
    r_fair = settlement_identity(pm_soc, k_soc_fair, "sports")
    assert r_fair["status"] == DIVERGENT and any("fallback" in s for s in r_fair["reasons"]), ("sports fair-vs-last void fallback -> DIVERGENT", r_fair["status"], r_fair["reasons"])

    # 90-min (Kalshi regulation) vs extra-time/penalties (pmus extended) -> DIVERGENT
    pm_ext = dict(pm_soc, description=pm_soc["description"].replace("full time (90 minutes plus stoppage time)", "the result including extra time and penalty shootout"))
    r = settlement_identity(pm_ext, k_soc, "sports")
    assert r["status"] == DIVERGENT and any("RESULT-TIMING" in s for s in r["reasons"]), ("sports 90min-vs-ET -> DIVERGENT", r)

    # one venue SILENT on void -> NEEDS_MANUAL (the void tail is the #1 sports risk; never assume)
    k_silent = [dict(k_soc[0], rules_secondary="If the game ends in a tie, the 'Tie' market resolves to Yes."), k_soc[1]]  # no reschedule clause
    r = settlement_identity(pm_soc, k_silent, "sports")
    assert r["status"] == NEEDS_MANUAL and any("void" in s.lower() for s in r["reasons"]), ("sports one-venue-silent-on-void -> NEEDS_MANUAL", r)

    # void WINDOW differs (Kalshi 2 days [MLB] vs pmus 2 weeks) -> DIVERGENT (the known MLB risk). Also a
    # baseball "tie or draw -> settle $0.50" clause with only 2 marketSides must read as 2-way, NOT 3-way
    # (regression: the prose word "draw" in a void clause was falsely tripping a 3-way signal in --audit).
    pm_mlb = {"slug": "aec-mlb-min-tex-2026-06-16",
              "marketSides": [{"team": {"name": "Minnesota"}}, {"team": {"name": "Texas"}}],
              "description": "baseball game Minnesota vs Texas on 2026-06-16. In the case of a tie or draw, the instrument will settle at $0.50. If postponed, remains open; if not rescheduled within two weeks, settle at last-traded prices."}
    k_mlb = [{"ticker": "KXMLBGAME-26JUN16MINTEX-TEX",
              "rules_primary": "If Texas wins the Minnesota vs Texas baseball game scheduled for Jun 16, 2026, then Yes.",
              "rules_secondary": "If this game is postponed or delayed, the market remains open and closes after the rescheduled game has finished (within two days). If cancelled or rescheduled to over two days away, the market resolves to a fair price.",
              "yes_sub_title": "Texas"},
             {"ticker": "KXMLBGAME-26JUN16MINTEX-MIN", "yes_sub_title": "Minnesota", "rules_primary": "Minnesota leg"}]
    r = settlement_identity(pm_mlb, k_mlb, "sports")
    assert r["status"] == DIVERGENT and any("WINDOW" in s for s in r["reasons"]), ("sports void-window 2d-vs-2wk -> DIVERGENT", r)
    assert r["dims"]["outcome_count"]["pm_3way"] is False, ("baseball 'tie->50-50' clause + 2 marketSides must be 2-way, not 3-way", r["dims"]["outcome_count"])

    # ---- ECON ----
    # pmus '>= 4.4' U-3 <-> Kalshi 'Above 4.3' (identical twin on the 0.1 grid), BLS both -> IDENTICAL
    pm_u3 = {"slug": "urc-us-seasonadj-gte-june-2026-07-02-atl4pt4",
             "description": "Will the U-3 unemployment rate reported by the Bureau of Labor Statistics be at least 4.4% ..."}
    k_u3 = {"ticker": "KXU3-26JUN-T4.3", "floor_strike": 4.3, "cap_strike": None, "strike_type": "greater",
            "yes_sub_title": "Above 4.3%",
            "rules_primary": "If the seasonally adjusted unemployment rate (U-3) reported by the Bureau of Labor Statistics is above 4.3% in June 2026, then Yes."}
    r = settlement_identity(pm_u3, k_u3, "econ")
    assert r["status"] == IDENTICAL, ("econ grid-twin match -> IDENTICAL", r["status"], r["reasons"])

    # off-by-one (Kalshi floor == T == 4.4, the pre-0013 wrong partner) -> DIVERGENT (the phantom)
    r = settlement_identity(pm_u3, dict(k_u3, floor_strike=4.4, ticker="KXU3-26JUN-T4.4", yes_sub_title="Above 4.4%"), "econ")
    assert r["status"] == DIVERGENT and any("phantom" in s or "off by" in s for s in r["reasons"]), ("econ off-by-one -> DIVERGENT", r)

    # pmus '<=' tail -> NEEDS_MANUAL (opposite orientation, not co-listable)
    pm_le = {"slug": "cpic-uscpi-may2026yoy-2026-06-10-lte3pt7pct", "description": "CPI YoY at most 3.7% per BLS"}
    r = settlement_identity(pm_le, k_u3, "econ")
    assert r["status"] == NEEDS_MANUAL, ("econ <= tail -> NEEDS_MANUAL", r)

    # unparseable econ slug -> NEEDS_MANUAL
    r = settlement_identity({"slug": "not-an-econ-slug", "description": "x"}, k_u3, "econ")
    assert r["status"] == NEEDS_MANUAL, ("econ unparseable -> NEEDS_MANUAL", r)

    # ---- generic unextractable -> NEEDS_MANUAL ----
    r = settlement_identity({"slug": "x"}, {}, "weather")
    assert r["status"] == NEEDS_MANUAL, ("empty weather -> NEEDS_MANUAL", r)
    r = settlement_identity({"slug": "x"}, {}, "bogus-category")
    assert r["status"] == NEEDS_MANUAL, ("unknown category -> NEEDS_MANUAL", r)

    print("OK - weather station/CLI/boundary; sports ESPN-vs-FIFA IDENTICAL (source ignored), timing/void-window/3way DIVERGENT, silent-void NEEDS_MANUAL; econ twin IDENTICAL / off-by-one DIVERGENT / <=-tail+unparseable NEEDS_MANUAL")


# =========================================================================================================
# AUDIT — run the gate over the LIVE co-listed universe (every discovered pair gets checked).
# =========================================================================================================
def _decisive_reasons(r, n=2):
    """Pick the reasons that DROVE the verdict for the example display: a DIVERGENT/NEEDS_MANUAL reason names
    its dimension in CAPS or 'not ...' — surface those first so the example explains WHY (the void-window
    DIVERGENT shouldn't be hidden behind an earlier 'timing silent' note)."""
    reasons = r.get("reasons", [])
    decisive = [s for s in reasons if re.search(r"differs|DIFFERS|phantom|off by|not (?:extractable|provable|specifically|determinable|both-provable|resolvable|parseable)|silent|OPPOSITE|point bucket", s)]
    picked = decisive[:n] or reasons[:n]
    return picked


def _audit():
    print("settlement-identity GATE over the live co-listed universe (read-only)...\n")
    colisted, rep = build_colisted_map()
    pm_by_slug = {m.get("slug"): m for m in pm_catalog()}
    counts = collections.defaultdict(lambda: collections.Counter())
    examples = collections.defaultdict(list)

    for cat in ("weather", "sports", "econ"):
        for e in colisted.get(cat, []):
            pm = pm_by_slug.get(e["slug"], {"slug": e["slug"]})
            try:
                if cat == "sports":
                    kalshi = [kalshi_detail(e["kalshi_a"]), kalshi_detail(e["kalshi_b"])]
                else:
                    kalshi = kalshi_detail(e["kalshi"])
                r = settlement_identity(pm, kalshi, cat)
            except Exception as ex:
                r = {"status": NEEDS_MANUAL, "reasons": [f"audit fetch/eval error {ex!r}"], "dims": {}}
            counts[cat][r["status"]] += 1
            if len(examples[(cat, r["status"])]) < 3:
                tag = e.get("city") or e.get("league") or e.get("family") or "?"
                examples[(cat, r["status"])].append((tag, e["slug"], _decisive_reasons(r)))

    print(f"{'category':10} {'IDENTICAL':>10} {'DIVERGENT':>10} {'NEEDS_MANUAL':>13}   total")
    grand = collections.Counter()
    for cat in ("weather", "sports", "econ"):
        c = counts[cat]; grand.update(c)
        tot = sum(c.values())
        print(f"{cat:10} {c[IDENTICAL]:>10} {c[DIVERGENT]:>10} {c[NEEDS_MANUAL]:>13}   {tot}")
    print(f"{'TOTAL':10} {grand[IDENTICAL]:>10} {grand[DIVERGENT]:>10} {grand[NEEDS_MANUAL]:>13}   {sum(grand.values())}")

    print("\nexample reasons by (category, status):")
    for (cat, status), exs in sorted(examples.items()):
        print(f"\n  [{cat} / {status}]")
        for tag, slug, reasons in exs:
            print(f"    {tag:6} {slug}")
            for rs in reasons:
                print(f"        - {rs}")

    if rep["counts"]:
        print(f"\n(universe: {rep['counts']['weather_pairs']} weather, {rep['counts']['sports_pairs']} sports, "
              f"{rep['counts']['econ_pairs']} econ co-listed pairs from build_colisted_map)")
    print("\nGATE SEMANTICS: IDENTICAL = every outcome-determining dimension provably matched (clean arb input). "
          "DIVERGENT = a dimension provably differs (a 'locked' pair can lose BOTH legs — do NOT trade as an arb). "
          "NEEDS_MANUAL = something unextractable (default; read the rules + decide). A verdict gates; the bot sizes.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="reusable settlement-identity gate (invariant #1)")
    ap.add_argument("--selftest", action="store_true", help="offline synthetic-dict proofs (no network)")
    ap.add_argument("--audit", action="store_true", help="run the gate over the live co-listed universe (read-only)")
    a = ap.parse_args()
    if a.selftest:
        _selftest()
    elif a.audit:
        _audit()
    else:
        ap.print_help()
        print("\n(default: nothing run — pass --selftest or --audit)")
