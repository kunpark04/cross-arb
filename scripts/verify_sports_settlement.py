"""scripts/verify_sports_settlement.py - settlement-identity verification for the SPORTS pair (review C5).

Sibling of verify_settlement.py (weather). Invariant #1 says the cross-venue arb is CLEAN only if BOTH
venues grade the SAME game off the SAME outcome. For weather that is a deterministic public number (NWS CLI);
for SPORTS it is an official-result CALL, and that call can DIVERGE on a contested game - Kalshi (league
official + AP/Sportradar + its Outcome Review Committee) vs polymarket.us (its DCM rulebook) - especially on
postponed / suspended / protested / overturned / forfeited / walkover / no-contest games. If the two
rulebooks resolve such a game differently, a "locked" YES+NO pair loses BOTH legs on exactly the deep MLB
positions the scalability thesis rides on. This was NEVER verified (verify_settlement.py is weather-only).

This probe pulls BOTH venues' ACTUAL rules for each co-listed game and prints them side-by-side with
heuristic flags on (a) the named result SOURCE and (b) VOID/postponement handling, so a human can verify
per league. READ-ONLY, public APIs. It does NOT decide the thesis - a flag is a prompt to look, not a verdict.

  python scripts/verify_sports_settlement.py [--league mlb]
"""
import os, sys, re, argparse
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bot"))
from colisted_map import get, build_colisted_map, pm_catalog, KAL   # reuse the proven catalog pull
from verify_settlement import fuzzy_rule_fields, kalshi_detail      # reuse the rule-field extractors

# heuristic extractors - what result SOURCE and what VOID/postponement handling does the rules text name?
SOURCE = re.compile(r"sportradar|associated press|\bAP\b|official(?:\s+league)?|league office|"
                    r"\bMLB\b|\bNBA\b|\bNHL\b|\bWNBA\b|\bUFC\b|\bATP\b|\bWTA\b|\bITF\b|"
                    r"box ?score|final score|result|determin\w+", re.I)
VOID   = re.compile(r"postpone\w*|suspend\w*|\brain\b|delay\w*|cancel\w*|void\w*|no[- ]contest|"
                    r"walkover|w/o|retir\w*|forfeit\w*|abandon\w*|overturn\w*|protest\w*|"
                    r"called game|official game|mercy rule|remak\w*|rescheduled?", re.I)

def hits(text, rx):
    return sorted({m.group(0).lower() for m in rx.finditer(text or "")})

def k_text(km):
    return " ".join(str(km.get(k) or "") for k in
                    ("rules_primary", "rules_secondary", "title", "subtitle", "yes_sub_title"))

def show(league, e, kmA, kmB, pm):
    print("=" * 92)
    print(f"LEAGUE {league.upper()}   {e.get('teamA')} vs {e.get('teamB')}   date {e.get('date')}")
    print(f"  kalshi A {e['kalshi_a']}   kalshi B {e['kalshi_b']}   <->  pmus {e['slug']}")
    print("-" * 92)
    kt = k_text(kmA) + " " + k_text(kmB)
    pt = " ".join(fuzzy_rule_fields(pm).values())
    print("KALSHI")
    print(f"  source : {hits(kt, SOURCE) or '(none found in rules text)'}")
    print(f"  void/postpone handling : {hits(kt, VOID) or '(none found - rules silent on void/postponement)'}")
    for k in ("rules_primary", "rules_secondary", "settlement_sources"):
        if kmA.get(k): print(f"  {k}: {str(kmA.get(k))[:320]}")
    print("POLYMARKET.US")
    print(f"  source : {hits(pt, SOURCE) or '(none found)'}")
    print(f"  void/postpone handling : {hits(pt, VOID) or '(none found - verify rulebook MANUALLY)'}")
    for k, v in fuzzy_rule_fields(pm).items():
        print(f"  {k}: {v[:320]}")
    # FLAGS (heuristic; a prompt to look, not a verdict)
    flags = []
    ks, ps = set(hits(kt, SOURCE)), set(hits(pt, SOURCE))
    if ks and ps and not (ks & ps): flags.append(f"RESULT SOURCE differ: Kalshi {sorted(ks)} vs pmus {sorted(ps)}")
    if not ps: flags.append("pmus rules text not in the API object — verify source + void handling MANUALLY (rulebook)")
    kv, pv = set(hits(kt, VOID)), set(hits(pt, VOID))
    if kv != pv: flags.append(f"VOID/POSTPONE handling differs (or one is silent): Kalshi {sorted(kv) or 'silent'} vs pmus {sorted(pv) or 'silent'}")
    print("  >>> " + ("  |  ".join(flags) if flags else "no obvious mismatch flagged — STILL read the void/postpone clauses above"))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="settlement-identity check for the sports pair (C5)")
    ap.add_argument("--league", help="only this league (e.g. mlb); default = one sample game per league")
    a = ap.parse_args()
    print("pulling both venues' co-listed sports games + rules (read-only)...\n")
    colisted, _ = build_colisted_map()
    pm_by_slug = {m.get("slug"): m for m in pm_catalog()}
    by_league = {}
    for e in colisted["sports"]:
        by_league.setdefault(e["league"], e)               # one sample co-listed game per league
    if not by_league:
        print("no co-listed sports games right now — re-run when games are listed."); sys.exit(0)
    leagues = [a.league] if a.league else sorted(by_league)
    for lg in leagues:
        e = by_league.get(lg)
        if not e: print(f"{lg}: not co-listed right now"); continue
        try:
            show(lg, e, kalshi_detail(e["kalshi_a"]), kalshi_detail(e["kalshi_b"]), pm_by_slug.get(e["slug"], {}))
        except Exception as ex:
            print(f"{lg}: probe error {ex!r}")
    print("=" * 92)
    print("VERDICT IS YOURS: for each league read the RESULT SOURCE + the VOID/POSTPONEMENT clauses. Identical")
    print("named source AND identical void/suspended-game/forfeit handling on both venues = settlement-clean for")
    print("that league. ANY divergence (different provider, or one voids/refunds while the other rolls to the")
    print("rescheduled date) = a contested game can settle the two legs to OPPOSITE outcomes -> lose BOTH legs.")
    print("Verify PER LEAGUE (MLB suspended-game, tennis/UFC retirement, esports forfeit/remake all differ).")
