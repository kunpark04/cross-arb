"""scripts/verify_econ_settlement.py - settlement-identity verification for the ECON pair (CPI/U-3/NFP/GDP/Fed).

Econ is the structurally cleanest US-legal subset (same government print on both venues -- BLS/BEA/Fed). It has
three subtleties weather does not, and (1) is LOAD-BEARING (decision 0013):
  (1) INEQUALITY: pmus ">= T" is INCLUSIVE ("at least T"); Kalshi "Above T" is STRICT (strike_type=greater).
      On the print grid (one decimal for U-3/CPI/GDP; 1000s for NFP), ">= T" == "> T-step", so the settlement-
      IDENTICAL Kalshi twin has floor_strike = T - step (econ_twin). Pairing floor == T is OFF BY ONE BUCKET:
      a print exactly on T settles the venues OPPOSITELY, and that pair's cross-venue gap is the market-priced
      P(print==T) (verified live 2026-06-10: pmus >=4.4 mid ~0.275 sat next to Kalshi T4.3 mid ~0.33, NOT its
      old T4.4 partner mid ~0.105 -- the "12c edge" was the boundary mass, a phantom).
  (2) STRUCTURE: pmus CPI MIDDLE markets are "exactly X%" POINT buckets; Kalshi is cumulative "Above X" only -- so
      only the pmus CPI TAILS (<=lo / >=hi) have a clean Kalshi counterpart. GDP/NFP/U-3 are all cumulative (">= X")
      so they map directly. Fed is a 5-way CATEGORICAL (maintains / +-25bps / +->25bps) that matches label-for-label.
  (3) ORIENTATION: the pmus `outcomes` order varies (["Yes","No"] vs ["No","Yes"]); the YES leg must be identified
      from the description, not assumed, or the cross-venue pair is mis-oriented (invariant #2).

This probe pulls BOTH venues' live econ markets, joins with the SAME matcher the monitor uses
(colisted_map.econ_parse + econ_twin — no private copy to drift), and prints each candidate pair's exact rule +
inequality + outcome so a human can confirm. READ-ONLY.

  python scripts/verify_econ_settlement.py [--family cpi]
"""
import os, sys, re, argparse, collections
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bot"))
from colisted_map import get, KAL, pm_catalog, econ_parse, econ_twin, ECON, _FEDLBL  # noqa: E402

# pmus slug prefix -> human label + kind (series + grid step come from colisted_map.ECON)
FAMS = {"cpic": ("CPI YoY", "cumthr"), "urc": ("Unemployment U-3", "cumthr"),
        "nfpc": ("Nonfarm Payrolls", "cumthr"), "gdpc": ("GDP SAAR", "cumthr"),
        "rdc": ("Fed decision", "fedcat")}


def k_period(ticker):
    m = re.search(r"-(\d{2}[A-Z]{3}\d{0,2})-", str(ticker))
    return m.group(1) if m else "?"


def show(fam, label, kind, pm_mkts, k_mkts):
    step = ECON[fam][2]
    print("=" * 96)
    print(f"FAMILY {label}   (pmus {len(pm_mkts)} <-> Kalshi {len(k_mkts)})   kind={kind}"
          + (f"   print-grid step={step}" if step else ""))
    print("-" * 96)
    kby = collections.defaultdict(dict)                      # period -> {floor: market}  (Kalshi "Above floor")
    klabels = collections.defaultdict(dict)                  # for fed: period -> {sub_title: market}
    for m in k_mkts:
        per = k_period(m.get("ticker"))
        if m.get("floor_strike") is not None:
            kby[per][round(float(m["floor_strike"]), 6)] = m
        klabels[per][str(m.get("yes_sub_title", "")).lower()] = m
    flags = []
    for x in pm_mkts:
        p = econ_parse(x.get("slug"))                        # the SAME parser the live matcher uses
        if not p: continue
        if kind == "fedcat":
            want = _FEDLBL.get(p["label"], "?")
            km = klabels.get(p["period"], {}).get(want)
            mark = "MATCH" if km else "no-kalshi"
            print(f"  {mark:9} pmus {p['label']:12} {p['period']}  <->  kalshi {km.get('ticker') if km else '--':28} ({want})")
            continue
        if p["ineq"] == "==":
            flags.append(f"POINT-BUCKET (no cumulative Kalshi twin): {x.get('slug')}")
            continue
        if p["ineq"] == "<=":
            flags.append(f"<=-tail (pmus YES == kalshi NO, OPPOSITE orientation; not paired): {x.get('slug')}")
            continue
        twin = econ_twin(p["thr"], step)                     # the IDENTICAL twin: floor = T - step (0013)
        km = kby.get(p["period"], {}).get(twin)
        off = kby.get(p["period"], {}).get(round(float(p["thr"]), 6))   # the OLD off-by-one partner, for contrast
        if km:
            print(f"  MATCH @ >={p['thr']:<8} {p['period']}  pmus >= {p['thr']}  <->  kalshi {km.get('ticker')} "
                  f"(Above {twin})  | IDENTICAL on the print grid"
                  + (f"   [old off-by-one partner: {off.get('ticker')}]" if off is not None else ""))
        else:
            flags.append(f"no IDENTICAL twin (Above {twin}) listed @ {p['period']} for {x.get('slug')} "
                         f"-> NOT co-listed (pairing 'Above {p['thr']}' would differ on a print == {p['thr']})")
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
    for pre, (label, kind) in FAMS.items():
        if sel and pre != sel: continue
        if not bypre.get(pre): continue
        kd = get(f"{KAL}?series_ticker={ECON[pre][0]}&limit=400")
        show(pre, label, kind, bypre[pre], kd.get("markets", []) if isinstance(kd, dict) else [])
    print("=" * 96)
    print("VERDICT IS YOURS: a MATCH line is a settlement-IDENTICAL pair — same govt print, same period, and")
    print("pmus '>= T' joined to Kalshi 'Above T-step' (identical on the print grid; decision 0013). <=-tails,")
    print("point buckets, and >=T markets with NO listed twin are NOT co-listable (pairing 'Above T' instead is")
    print("off by one bucket: the gap = market-priced P(print==T), a phantom). Confirm the source agency per")
    print("family (BLS/BEA/Fed) before sizing. Feeds the ECON config in bot/colisted_map.py.")
