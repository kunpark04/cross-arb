"""scripts/verify_settlement.py — settlement-identity verification for the WEATHER pair (review risk #1).

The thesis ("buy YES cheap + NO dear, hold to settlement, collect the gap, outcome-independent") is a
CLEAN arb ONLY if both venues grade the SAME market off the IDENTICAL number: same station, same bucket
boundary inequalities, same settlement timing/revision. If they differ, a "locked" pair can lose BOTH
legs (invariant #1 / lesson L2). The project's own research left this UNVERIFIED and even self-contradicted
it. This probe pulls BOTH venues' ACTUAL market rules for each co-listed weather city and prints them
side-by-side with heuristic mismatch flags, so a human can verify per city. READ-ONLY, public APIs.

  python scripts/verify_settlement.py [--city lax]

It does NOT decide the thesis — it surfaces the real terms so YOU can. Read the printed rules; a flag is
a prompt to look, not a verdict.
"""
import os, sys, re, json, argparse
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bot"))
from colisted_map import get, build_colisted_map, pm_catalog, WX, KAL, PM   # reuse the proven catalog pull

# heuristic extractors — what number/station/timing does the rules text name?
STATION = re.compile(r"\bK[A-Z]{3}\b|central park|laguardia|o'?hare|midway|"
                     r"climatolog\w*|\bCLI\b|airport|national weather service|\bNWS\b|"
                     r"weather underground|wunderground|\bMETAR\b|\bASOS\b", re.I)
TIMING  = re.compile(r"\b\d{1,2}:\d{2}\s*[ap]\.?m\.?|\bfinal\b|prelimin\w*|revis\w*|next[- ]day|"
                     r"\b8\s*a\.?m\.?|midnight|end of day", re.I)

def hits(text, rx):
    return sorted({m.group(0).lower() for m in rx.finditer(text or "")})

def pm_boundary(slug):
    s = str(slug).lower()
    m = re.search(r"gte(\d+)lt(\d+)", s)
    if m: return f"{m.group(1)} <= T < {m.group(2)}   (gte{m.group(1)}lt{m.group(2)})"
    m = re.search(r"-lt(\d+)f", s)
    if m: return f"T < {m.group(1)}   (low tail)"
    m = re.search(r"gte(\d+)f?\b", s)
    if m: return f"T >= {m.group(1)}   (high tail)"
    return "?? (could not parse slug)"

def kalshi_boundary(km):
    fs, cs = km.get("floor_strike"), km.get("cap_strike")
    sub = km.get("yes_sub_title") or km.get("subtitle") or ""
    rng = (f"floor_strike={fs}  cap_strike={cs}" if (fs is not None or cs is not None) else "no strikes")
    return f"{rng}   subtitle={sub!r}"

def fuzzy_rule_fields(obj):
    """Any field whose KEY looks settlement-relevant (robust to the pmus schema we don't fully know)."""
    out = {}
    for k, v in (obj or {}).items():
        if isinstance(v, (str, int, float)) and re.search(r"rule|resol|settle|source|descr|expir|grade|outcome", k, re.I):
            sv = str(v)
            if sv.strip(): out[k] = sv[:600]
    return out

def kalshi_detail(ticker):
    d = get(f"{KAL}/{ticker}")
    return d.get("market", d) if isinstance(d, dict) else {}

def show(city, e, km, pm):
    print("=" * 92)
    print(f"CITY {city.upper()}   (kalshi {e['kalshi']}  <->  pmus {e['slug']})")
    print("-" * 92)
    k_rules = " ".join(str(km.get(k) or "") for k in ("rules_primary", "rules_secondary", "title", "subtitle"))
    p_rules = " ".join(fuzzy_rule_fields(pm).values())
    print("KALSHI")
    print(f"  bucket : {kalshi_boundary(km)}")
    print(f"  source : {hits(k_rules, STATION) or '(none found in rules text)'}")
    print(f"  timing : {hits(k_rules, TIMING) or '(none found)'}    expiry={km.get('expected_expiration_time') or km.get('expiration_time')}")
    for k in ("rules_primary", "rules_secondary", "settlement_sources"):
        if km.get(k): print(f"  {k}: {str(km.get(k))[:300]}")
    print("POLYMARKET.US")
    print(f"  bucket : {pm_boundary(e['slug'])}")
    print(f"  source : {hits(p_rules, STATION) or '(none found)'}")
    print(f"  timing : {hits(p_rules, TIMING) or '(none found)'}")
    for k, v in fuzzy_rule_fields(pm).items():
        print(f"  {k}: {v[:300]}")
    # FLAGS (heuristic; a prompt to look, not a verdict)
    flags = []
    ks, ps = set(hits(k_rules, STATION)), set(hits(p_rules, STATION))
    if ks and ps and not (ks & ps): flags.append(f"STATION/SOURCE differ: Kalshi {sorted(ks)} vs pmus {sorted(ps)}")
    if not ps: flags.append("pmus rules text not found in the API object — verify the source MANUALLY (FAQ/rulebook)")
    kt, pt = set(hits(k_rules, TIMING)), set(hits(p_rules, TIMING))
    if kt and pt and not (kt & pt): flags.append(f"SETTLEMENT TIMING differ: Kalshi {sorted(kt)} vs pmus {sorted(pt)}")
    print("  >>> " + ("  |  ".join(flags) if flags else "no obvious mismatch flagged — STILL eyeball the bucket boundaries above"))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="settlement-identity check for the weather pair")
    ap.add_argument("--city", help="only this city (e.g. lax); default = all mapped")
    a = ap.parse_args()
    print("pulling both venues' co-listed weather markets + rules (read-only)...\n")
    colisted, _ = build_colisted_map()
    pm_by_slug = {m.get("slug"): m for m in pm_catalog()}        # full pmus objects (rules/description)
    by_city = {}
    for e in colisted["weather"]:
        by_city.setdefault(e["city"], e)                         # one sample co-listed pair per city
    if not by_city:
        print("no co-listed weather pairs right now — re-run when weather markets are listed."); sys.exit(0)
    cities = [a.city] if a.city else sorted(by_city)
    for city in cities:
        e = by_city.get(city)
        if not e: print(f"{city}: not co-listed right now"); continue
        try:
            show(city, e, kalshi_detail(e["kalshi"]), pm_by_slug.get(e["slug"], {}))
        except Exception as ex:
            print(f"{city}: probe error {ex!r}")
    print("=" * 92)
    print("VERDICT IS YOURS: read the bucket boundaries + source + timing above per city. Identical on all")
    print("three = clean arb. Any difference (station, inequality, or settle time/revision) = the weather")
    print("pair can lose BOTH legs and is NOT a clean arb for that city.")
