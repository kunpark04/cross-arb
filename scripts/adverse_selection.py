"""scripts/adverse_selection.py - "why is the cheap side cheap?" read-only test (review B2).

The arb buys YES on the CHEAP venue + NO on the DEAR venue. Adverse selection bites on the FILL: you fill the
EASY (stale) leg and MISS the leg that MOVES. So "which venue moved when the edge closed" tells you which leg you
would most likely have missed -- and whether the cheap quote was a benign laggard or an INFORMED quote the dear
venue then corrected toward.

For each OPEN->CLOSE episode (weather 1:1) we read the per-venue YES touches logged at OPEN and at CLOSE
(`px` field, added to bot/monitor.py) and attribute the gap collapse:
  dir P : you buy YES@pmus(ask p_ya, CHEAP) + NO@Kalshi(=lift bid k_yb, DEAR).  gap = k_yb - p_ya.
  dir K : you buy YES@Kalshi(ask k_ya, CHEAP) + NO@pmus(=lift bid p_yb, DEAR).  gap = p_yb - k_ya.
  CHEAP-ROSE  (cheap ask climbs toward dear)  => the cheap venue was stale-LOW; you'd miss the CHEAP leg.
  DEAR-FELL   (dear bid drops toward cheap)   => the dear venue was stale-HIGH; the cheap quote was RIGHT
                                                  (informed) and you'd miss the DEAR leg.
A book dominated by DEAR-FELL closes is the toxic case (the cheap side you lift was the correct price; the dear
side you needed was the mover). Roughly symmetric = less directional fill toxicity.

READ-ONLY. Needs the `px` field, which the monitor logs as of the 2026-06-09 enhancement -- data collected
BEFORE the next droplet deploy will not have it (the tool says so and exits cleanly). `--selftest` proves the
attribution logic on synthetic episodes.

  python scripts/adverse_selection.py --selftest
  python scripts/adverse_selection.py [--data-dir PATH]
"""
import os, sys, json, glob, gzip, argparse

def _open_any(p):
    return gzip.open(p, "rt", encoding="utf-8") if p.endswith(".gz") else open(p, encoding="utf-8")

def load_transitions(data_dir):
    recs = []
    for p in sorted(glob.glob(os.path.join(data_dir, "transitions-*.jsonl")) +
                    glob.glob(os.path.join(data_dir, "transitions-*.jsonl.gz"))):
        with _open_any(p) as f:
            for ln in f:
                ln = ln.strip()
                if ln:
                    try: recs.append(json.loads(ln))
                    except ValueError: pass
    recs.sort(key=lambda r: r.get("t", 0))
    return recs


def attribute(dir_, px_open, px_close):
    """Return ('cheap_rose'|'dear_fell'|'mixed'|None, cheap_move, dear_move). Weather dirs P/K only."""
    if dir_ == "P":
        cheap_o, cheap_c = px_open.get("p_ya"), px_close.get("p_ya")   # cheap = pmus YES ask
        dear_o,  dear_c  = px_open.get("k_yb"), px_close.get("k_yb")   # dear  = Kalshi YES bid
    elif dir_ == "K":
        cheap_o, cheap_c = px_open.get("k_ya"), px_close.get("k_ya")   # cheap = Kalshi YES ask
        dear_o,  dear_c  = px_open.get("p_yb"), px_close.get("p_yb")   # dear  = pmus YES bid
    else:
        return None, None, None                                       # sports (2-outcome) not attributed here
    if None in (cheap_o, cheap_c, dear_o, dear_c):
        return None, None, None
    cheap_rise = cheap_c - cheap_o          # cheap ask climbing CLOSES the gap (positive contribution)
    dear_fall  = dear_o - dear_c            # dear bid dropping CLOSES the gap (positive contribution)
    if cheap_rise <= 0 and dear_fall <= 0:
        return "mixed", cheap_rise, dear_fall
    tag = "cheap_rose" if cheap_rise > dear_fall else "dear_fell"
    return tag, cheap_rise, dear_fall


def episodes_with_px(recs):
    """Pair OPEN->CLOSE per market, keeping the px at each end (weather only)."""
    open_st, out = {}, []
    for r in recs:
        m, lab = r.get("market"), r.get("transition")
        if lab == "OPEN":
            open_st[m] = r
        elif lab == "CLOSE" and m in open_st:
            o = open_st.pop(m)
            out.append((o, r))
    return out


def analyze(recs):
    pairs = episodes_with_px(recs)
    rows = [(o, c) for (o, c) in pairs if o.get("px") and c.get("px") and o.get("dir") in ("P", "K")]
    counts = {"cheap_rose": 0, "dear_fell": 0, "mixed": 0}
    cheap_moves, dear_moves = [], []
    for o, c in rows:
        tag, cm, dm = attribute(o["dir"], o["px"], c["px"])
        if tag is None:
            continue
        counts[tag] += 1
        cheap_moves.append(abs(cm or 0.0)); dear_moves.append(abs(dm or 0.0))
    return {"weather_open_close_pairs": len(pairs), "with_px_and_weather_dir": len(rows),
            "attributed": sum(counts.values()), "counts": counts,
            "med_cheap_move": _med(cheap_moves), "med_dear_move": _med(dear_moves)}


def _med(xs):
    return sorted(xs)[len(xs) // 2] if xs else None


def report(data_dir):
    recs = load_transitions(data_dir)
    a = analyze(recs)
    L = ["=" * 68, "ADVERSE SELECTION  (which venue moved when the edge closed?)", "=" * 68]
    L.append(f"transitions read       : {len(recs)}")
    L.append(f"weather OPEN->CLOSE     : {a['weather_open_close_pairs']}")
    L.append(f"  with px + weather dir : {a['with_px_and_weather_dir']}")
    if a["attributed"] == 0:
        L += ["", "No attributable episodes yet. The `px` field (per-venue YES touches) is logged by bot/monitor.py",
              "as of the 2026-06-09 enhancement; data collected BEFORE the next droplet deploy lacks it. Re-pull",
              "after redeploy, then re-run -- the dominant close-direction is the leg you'd most often MISS."]
        return "\n".join(L)
    n = a["attributed"]; c = a["counts"]
    L.append(f"  attributed            : {n}")
    L += ["", f"  CHEAP-ROSE (cheap was stale-low; you'd miss the CHEAP leg) : {c['cheap_rose']:4d}  ({100*c['cheap_rose']/n:.0f}%)",
          f"  DEAR-FELL  (cheap was RIGHT/informed; you'd miss the DEAR leg): {c['dear_fell']:4d}  ({100*c['dear_fell']/n:.0f}%)  <- TOXIC",
          f"  mixed/other                                                  : {c['mixed']:4d}  ({100*c['mixed']/n:.0f}%)",
          "", f"  median cheap-side move {a['med_cheap_move']}   median dear-side move {a['med_dear_move']}"]
    tox = c["dear_fell"] / n
    L += ["", f"  toxic-close share = {tox*100:.0f}%.  High => the cheap quote you lift is the CORRECT price and the",
          "  dear leg is the mover (you fill the wrong leg, miss the right one). Low/symmetric => benign line-lag."]
    return "\n".join(L)


def _selftest():
    # dir P: gap = k_yb - p_ya. CHEAP-ROSE: pmus ask 0.60->0.66 (rose .06), Kalshi bid 0.67->0.66 (fell .01)
    t, cm, dm = attribute("P", {"p_ya": 0.60, "k_yb": 0.67}, {"p_ya": 0.66, "k_yb": 0.66})
    assert t == "cheap_rose" and abs(cm - 0.06) < 1e-9, (t, cm)
    # DEAR-FELL: pmus ask 0.60->0.61 (rose .01), Kalshi bid 0.67->0.61 (fell .06) -> dear was the mover (toxic)
    t, cm, dm = attribute("P", {"p_ya": 0.60, "k_yb": 0.67}, {"p_ya": 0.61, "k_yb": 0.61})
    assert t == "dear_fell" and abs(dm - 0.06) < 1e-9, (t, dm)
    # dir K: gap = p_yb - k_ya. DEAR-FELL: pmus bid 0.70->0.64 (fell .06), Kalshi ask 0.63->0.64 (rose .01)
    t, _, dm = attribute("K", {"k_ya": 0.63, "p_yb": 0.70}, {"k_ya": 0.64, "p_yb": 0.64})
    assert t == "dear_fell" and abs(dm - 0.06) < 1e-9, (t, dm)
    # sports / missing px -> not attributed
    assert attribute("PK", {}, {})[0] is None
    assert attribute("P", {"p_ya": None, "k_yb": 0.6}, {"p_ya": 0.6, "k_yb": 0.6})[0] is None
    # end-to-end pairing + aggregation on synthetic transitions
    recs = [
        {"t": 1, "market": "tc-x", "transition": "OPEN",  "dir": "P", "px": {"p_ya": 0.60, "k_yb": 0.67}},
        {"t": 5, "market": "tc-x", "transition": "CLOSE", "dir": "P", "px": {"p_ya": 0.61, "k_yb": 0.61}},  # dear_fell
        {"t": 2, "market": "tc-y", "transition": "OPEN",  "dir": "P", "px": {"p_ya": 0.50, "k_yb": 0.58}},
        {"t": 6, "market": "tc-y", "transition": "CLOSE", "dir": "P", "px": {"p_ya": 0.57, "k_yb": 0.57}},  # cheap_rose
    ]
    a = analyze(recs)
    assert a["attributed"] == 2 and a["counts"]["dear_fell"] == 1 and a["counts"]["cheap_rose"] == 1, a
    print("OK - adverse_selection: cheap_rose / dear_fell attribution + OPEN->CLOSE pairing")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="adverse-selection (which venue moved on edge close)")
    ap.add_argument("--data-dir", default=os.path.join(os.path.dirname(__file__), "..", "..", "data", "cross-arb"))
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        _selftest(); sys.exit(0)
    dd = os.path.abspath(a.data_dir)
    if not glob.glob(os.path.join(dd, "transitions-*.jsonl*")):
        print(f"no data at {dd} - run `pwsh deploy/pull-data.ps1` first, or `--selftest`."); sys.exit(0)
    print(report(dd))
