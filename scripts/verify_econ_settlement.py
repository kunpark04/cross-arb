"""scripts/verify_econ_settlement.py - settlement-identity verification for the ECON pair (CPI/U-3/NFP/GDP/Fed).

Econ is the structurally cleanest US-legal subset (same government print on both venues -- BLS/BEA/Fed) but was
NEVER mapped into coverage. Before adding it to bot/colisted_map.py we must establish the exact join + settlement
identity, because econ has three subtleties weather does not:
  (1) INEQUALITY: pmus uses "<= T" / ">= T" (closed) at a specific T; Kalshi uses "Above T" (= strictly > T). So
      pmus ">= T" and Kalshi "Above T" are the SAME outcome ONLY away from the exact boundary; a print landing
      EXACTLY on T resolves them oppositely (a narrow both-legs residual, like the weather downward-correction).
  (2) STRUCTURE: pmus CPI MIDDLE markets are "exactly X%" POINT buckets; Kalshi is cumulative "Above X" only -- so
      only the pmus CPI TAILS (<=lo / >=hi) have a clean Kalshi counterpart. GDP/NFP/U-3 are all cumulative (">= X")
      so they map directly. Fed is a 5-way CATEGORICAL (maintains / +-25bps / +->25bps) that matches label-for-label.
  (3) ORIENTATION: the pmus `outcomes` order varies (["Yes","No"] vs ["No","Yes"]); the YES leg must be identified
      from the description, not assumed, or the cross-venue pair is mis-oriented (invariant #2).

This probe pulls BOTH venues' live econ markets, joins on (family, period, threshold), and prints each candidate
pair's exact rule + inequality + outcome so a human can confirm before it goes in the live matcher. READ-ONLY.

  python scripts/verify_econ_settlement.py [--family cpi]
"""
import os, sys, re, argparse, collections
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bot"))
from colisted_map import get, KAL, pm_catalog

# pmus slug prefix -> (Kalshi series, family label, kind)
FAMS = {"cpic": ("KXCPIYOY", "CPI YoY", "cumthr"), "urc": ("KXU3", "Unemployment U-3", "cumthr"),
        "nfpc": ("KXPAYROLLS", "Nonfarm Payrolls", "cumthr"), "gdpc": ("KXGDP", "GDP SAAR", "cumthr"),
        "rdc": ("KXFEDDECISION", "Fed decision", "fedcat")}
MON = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
KMON = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]


def num(tok):
    """'3pt7' -> 3.7 ; '250k' -> 250000 ; '4' -> 4.0 ; '4pt25' -> 4.25."""
    t = tok.lower().replace("pct", "").strip()
    mult = 1
    if t.endswith("k"): mult = 1000; t = t[:-1]
    t = t.replace("pt", ".")
    try: return float(t) * mult
    except ValueError: return None


def pm_econ(slug):
    """pmus econ market -> {fam, period(YYMMM or YYMMMDD or Qx), thr, ineq, label} or None."""
    s = str(slug).lower(); pre = s.split("-")[0]
    if pre not in FAMS: return None
    fam = pre
    if fam == "rdc":                                          # Fed categorical
        m = re.search(r"-(maintains|cut25bps|cutgt25bps|hike25bps|hikegt25bps)$", s)
        dm = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
        per = f"{dm.group(1)[2:]}{KMON[int(dm.group(2))-1]}" if dm else "?"   # 26JUN
        return {"fam": fam, "period": per, "thr": None, "ineq": "cat", "label": m.group(1) if m else "?"}
    # cumulative-threshold families
    tail = re.search(r"-(lte|gte)([0-9pt]+)pct$", s) or re.search(r"-(atl|atm)([0-9ptk]+)$", s)
    ineq, thr = None, None
    if tail:
        d = tail.group(1); thr = num(tail.group(2))
        ineq = "<=" if d == "lte" else ">="                  # lte=<=, gte/atl=>=
    else:
        pt = re.search(r"-([0-9pt]+)pct$", s)                # bare "X.Xpct" = EXACTLY X% (point bucket)
        if pt: thr = num(pt.group(1)); ineq = "=="
    # period
    if fam == "cpic":
        mm = re.search(r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*?(\d{4})yoy", s)
        per = f"{mm.group(2)[2:]}{KMON[MON[mm.group(1)[:3]]-1]}" if mm else "?"
    elif fam == "gdpc":
        dm = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
        per = f"{dm.group(1)[2:]}{KMON[int(dm.group(2))-1]}{dm.group(3)}" if dm else "?"   # 26JUL30
    else:                                                    # urc / nfpc -> month name in slug
        mm = re.search(r"-(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*-", s)
        ym = re.search(r"(\d{4})-\d{2}-\d{2}", s)
        per = f"{ym.group(1)[2:]}{KMON[MON[mm.group(1)[:3]]-1]}" if (mm and ym) else "?"
    return {"fam": fam, "period": per, "thr": thr, "ineq": ineq, "label": None}


def k_period(ticker):
    m = re.search(r"-(\d{2}[A-Z]{3}\d{0,2})-", str(ticker))
    return m.group(1) if m else "?"


def show(fam, label, kind, pm_mkts, k_mkts):
    print("=" * 96)
    print(f"FAMILY {label}   (pmus {len(pm_mkts)} <-> Kalshi {len(k_mkts)})   kind={kind}")
    print("-" * 96)
    kby = collections.defaultdict(dict)                      # period -> {thr: market}  (Kalshi "Above thr")
    klabels = collections.defaultdict(dict)                  # for fed: period -> {sub_title: market}
    for m in k_mkts:
        per = k_period(m.get("ticker"))
        kby[per][m.get("floor_strike")] = m
        klabels[per][str(m.get("yes_sub_title", "")).lower()] = m
    flags = []
    for x in pm_mkts:
        p = pm_econ(x.get("slug"))
        if not p: continue
        if kind == "fedcat":
            want = {"maintains": "fed maintains rate", "hike25bps": "hike 25bps", "hikegt25bps": "hike >25bps",
                    "cut25bps": "cut 25bps", "cutgt25bps": "cut >25bps"}.get(p["label"], "?")
            km = klabels.get(p["period"], {}).get(want)
            mark = "MATCH" if km else "no-kalshi"
            print(f"  {mark:9} pmus {p['label']:12} {p['period']}  <->  kalshi {km.get('ticker') if km else '--':28} ({want})")
            continue
        if p["ineq"] == "==":
            flags.append(f"POINT-BUCKET (no cumulative Kalshi twin): {x.get('slug')}")
            continue
        km = kby.get(p["period"], {}).get(p["thr"])
        rel = ("pmus YES(<=T) == kalshi NO(Above T)" if p["ineq"] == "<=" else
               "pmus YES(>=T) ~= kalshi YES(Above T) [differs only if print==T exactly]")
        if km:
            print(f"  MATCH @ {p['thr']:<8} {p['period']}  pmus {p['ineq']}{p['thr']}  <->  kalshi {km.get('ticker')}  | {rel}")
        else:
            flags.append(f"no Kalshi @ {p['period']} thr {p['thr']} for {x.get('slug')}")
    if flags:
        print("  FLAGS:")
        for f in flags[:12]: print(f"    - {f}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="econ settlement-identity / co-listing check")
    ap.add_argument("--family", help="cpi|u3|nfp|gdp|fed (default all)")
    a = ap.parse_args()
    sel = {"cpi": "cpic", "u3": "urc", "nfp": "nfpc", "gdp": "gdpc", "fed": "rdc"}.get((a.family or "").lower())
    print("pulling both venues' live econ markets (read-only)...\n")
    macro = [m for m in pm_catalog() if m.get("category") == "macro"]
    bypre = collections.defaultdict(list)
    for m in macro: bypre[str(m.get("slug", "")).split("-")[0]].append(m)
    for pre, (kser, label, kind) in FAMS.items():
        if sel and pre != sel: continue
        if not bypre.get(pre): continue
        kd = get(f"{KAL}?series_ticker={kser}&limit=400")
        show(pre, label, kind, bypre[pre], kd.get("markets", []) if isinstance(kd, dict) else [])
    print("=" * 96)
    print("VERDICT IS YOURS: a MATCH line is a clean co-listed pair (same family/period/threshold, same govt source).")
    print("Note the inequality relationship per line (<= -> kalshi NO ; >= -> kalshi YES with a boundary residual at")
    print("an exact-T print). POINT-BUCKET / no-kalshi lines are NOT directly co-listable. Confirm the source agency")
    print("matches per family (BLS/BEA/Fed) before sizing. Feeds the ECON config in bot/colisted_map.py.")
