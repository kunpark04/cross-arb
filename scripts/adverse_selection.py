"""scripts/adverse_selection.py - "why is the cheap side cheap?" read-only test (review B2 + probe #5).

The arb buys YES on the CHEAP venue + NO on the DEAR venue. Adverse selection bites on the FILL: you fill the
EASY (stale) leg and MISS the leg that MOVES. So "which venue moved when the edge closed" tells you which leg you
would most likely have missed -- and whether the cheap quote was a benign laggard or an INFORMED quote the dear
venue then corrected toward.

All four logged directions reduce to one cheap/dear YES-space pair (verified against bot/monitor.py, [L3]):
  binary (MarketTracker px {p_yb,p_ya,k_yb,k_ya} = per-venue YES bid/ask):
    dir P : buy YES@pmus ask + sell into Kalshi YES bid.   cheap = p_ya, dear = k_yb.  gap = k_yb - p_ya.
    dir K : buy YES@Kalshi ask + sell into pmus YES bid.   cheap = k_ya, dear = p_yb.  gap = p_yb - k_ya.
  sports (GameTracker px {pm_b,pm_a,ka,kb}: pm_* = pmus YES(=team A) bid/ask; ka/kb = Kalshi YES *asks* on A/B):
    dir PK: back A@P (lift pm_a) + back B@K (lift kb). Buying B@kb == selling A at the implied (1-kb):
            cheap = pm_a, dear = 1-kb (implied A-bid on Kalshi via the B book).
    dir KP: back A@K (lift ka) + back B@P (== sell A into pm_b).  cheap = ka, dear = pm_b.

CLOSE attribution (which leg killed the edge):
  CHEAP-ROSE  (cheap ask climbs toward dear)  => the cheap venue was stale-LOW; you'd miss the CHEAP leg.
  DEAR-FELL   (dear side drops toward cheap)  => the dear venue was stale-HIGH; the cheap quote was RIGHT
                                                  (informed) and you'd miss the DEAR leg.   <- TOXIC
OPEN attribution (the #5 DIRECTION-GATE candidate -- classifiable AT OPEN, before entering):
  compare the OPEN px against the SAME market's most recent px-bearing record (its previous episode's
  CLOSE, normally -- the monitor logs px on every transition, so any prior transition works):
  CHEAP-MADE (the buy side got cheaper)   => the cheap venue led; possibly informed flow (toxicity suspect).
  DEAR-MADE  (the sell side moved away)   => the cheap venue is a LAGGARD behind a real move (benign lag).
The gate question: does the at-open class predict (i) duration, (ii) close toxicity, (iii) shadow-fill
survival/realized edge at execution-relevant latencies? gate_report() measures all three.

READ-ONLY. Needs the `px` field (logged since the 2026-06-09 deploy). `--post-epoch` restricts to post-0013
records (detection-time stamps -- required for sub-second numbers, decision 0013). `--selftest` is offline.

  python scripts/adverse_selection.py --selftest
  python scripts/adverse_selection.py [--data-dir PATH] [--post-epoch] [--edge-min 0.01] [--json-out PATH]
"""
import os, sys, glob, argparse
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from analyze_persistence import load, build_episodes, category, ECON_REMAP_DEPLOY_TS
# the ONE loader (raw-over-gz dedup + econ quarantine + censor timestamps) and the ONE episode builder.


def _legs(dir_, px):
    """(cheap, dear) YES-space touch pair for direction dir_ (see module docstring). None if unavailable."""
    if not px:
        return None, None
    if dir_ == "P":
        return px.get("p_ya"), px.get("k_yb")
    if dir_ == "K":
        return px.get("k_ya"), px.get("p_yb")
    if dir_ == "PK":
        kb = px.get("kb")
        return px.get("pm_a"), (None if kb is None else round(1.0 - kb, 4))
    if dir_ == "KP":
        return px.get("ka"), px.get("pm_b")
    return None, None


def attribute(dir_, px_open, px_close):
    """CLOSE attribution: ('cheap_rose'|'dear_fell'|'mixed'|None, cheap_move, dear_move)."""
    co, do_ = _legs(dir_, px_open)
    cc, dc = _legs(dir_, px_close)
    if None in (co, cc, do_, dc):
        return None, None, None
    cheap_rise = cc - co                    # cheap ask climbing CLOSES the gap (positive contribution)
    dear_fall = do_ - dc                    # dear side dropping CLOSES the gap (positive contribution)
    if cheap_rise <= 0 and dear_fall <= 0:
        return "mixed", cheap_rise, dear_fall
    tag = "cheap_rose" if cheap_rise > dear_fall else "dear_fell"
    return tag, cheap_rise, dear_fall


def open_attrib(dir_, px_prior, px_open):
    """OPEN attribution: ('cheap_made'|'dear_made'|'mixed'|None, cheap_fall, dear_rise).
    cheap_fall = cheap_prior - cheap_open (the buy side got cheaper -> it created the edge);
    dear_rise  = dear_open - dear_prior   (the sell side moved away -> it created the edge).
    Both <= 0 (gap unchanged, net opened via the price-dependent fee term) -> 'mixed'."""
    cp, dp = _legs(dir_, px_prior)
    co, do_ = _legs(dir_, px_open)
    if None in (cp, dp, co, do_):
        return None, None, None
    cheap_fall = cp - co
    dear_rise = do_ - dp
    if cheap_fall <= 0 and dear_rise <= 0:
        return "mixed", cheap_fall, dear_rise
    tag = "cheap_made" if cheap_fall > dear_rise else "dear_made"
    return tag, cheap_fall, dear_rise


def episodes_with_px(recs, sessions=()):
    """Pair OPEN->CLOSE per market, keeping the px at each end. A pair spanning a censoring event
    (restart/resync/reconnect) is DROPPED — its two ends were observed by different processes/books,
    so the close attribution would be measuring the outage, not the market."""
    open_st, out = {}, []
    sess = sorted(sessions)
    for r in recs:
        m, lab = r.get("market"), r.get("transition")
        if lab == "OPEN":
            open_st[m] = r
        elif lab == "CLOSE" and m in open_st:
            o = open_st.pop(m)
            if not any(o["t"] < s <= r["t"] for s in sess):
                out.append((o, r))
    return out


def open_classes(recs, sessions=()):
    """AT-OPEN classification of every OPEN-with-px against the same market's most recent px-bearing
    record. Skips (counted): no_prior (first sighting), censor_gap (an outage sits between prior and
    open -- the move would measure the outage), missing_fields (a leg touch absent on either end).
    Returns (rows, skips); rows = {market, cat, open_t, dir, cls, cheap_fall, dear_rise, gap_s}."""
    sess = sorted(sessions)
    last_px = {}                                        # market -> (t, px)
    rows, skips = [], {"no_prior": 0, "censor_gap": 0, "missing_fields": 0}
    for r in recs:
        m, lab, px = r.get("market"), r.get("transition"), r.get("px")
        if lab == "OPEN" and px:
            prior = last_px.get(m)
            if prior is None:
                skips["no_prior"] += 1
            elif any(prior[0] < s <= r["t"] for s in sess):
                skips["censor_gap"] += 1
            else:
                cls, cf, dr = open_attrib(r.get("dir"), prior[1], px)
                if cls is None:
                    skips["missing_fields"] += 1
                else:
                    rows.append({"market": m, "cat": category(m), "open_t": r["t"], "dir": r.get("dir"),
                                 "cls": cls, "cheap_fall": cf, "dear_rise": dr,
                                 "gap_s": round(r["t"] - prior[0], 3)})
        if px:
            last_px[m] = (r["t"], px)
    return rows, skips


def analyze(recs, sessions=()):
    """CLOSE-side attribution over all OPEN->CLOSE pairs with px (all four dirs), overall + by category."""
    pairs = episodes_with_px(recs, sessions)
    rows = [(o, c) for (o, c) in pairs if o.get("px") and c.get("px")]
    counts = {"cheap_rose": 0, "dear_fell": 0, "mixed": 0}
    by_cat = {}
    cheap_moves, dear_moves = [], []
    for o, c in rows:
        tag, cm, dm = attribute(o.get("dir"), o["px"], c["px"])
        if tag is None:
            continue
        counts[tag] += 1
        by_cat.setdefault(category(o["market"]), {"cheap_rose": 0, "dear_fell": 0, "mixed": 0})[tag] += 1
        cheap_moves.append(abs(cm or 0.0)); dear_moves.append(abs(dm or 0.0))
    return {"open_close_pairs": len(pairs), "with_px": len(rows),
            "attributed": sum(counts.values()), "counts": counts, "by_cat": by_cat,
            "med_cheap_move": _med(cheap_moves), "med_dear_move": _med(dear_moves)}


def _med(xs):
    return sorted(xs)[len(xs) // 2] if xs else None


def _z_two_prop(k1, n1, k2, n2):
    """Two-proportion z (pooled). None if a cell is empty."""
    if not n1 or not n2:
        return None
    p1, p2, p = k1 / n1, k2 / n2, (k1 + k2) / (n1 + n2)
    se2 = p * (1 - p) * (1 / n1 + 1 / n2)
    return (p1 - p2) / (se2 ** 0.5) if se2 > 0 else None


def gate_report(recs, sessions, edge_min=0.01, gate_L=(0.15, 1.0)):
    """The #5 direction-gate measurement: per at-open class on the CAPTURABLE cohort (shared gate),
    (i) duration, (ii) close-toxicity share, (iii) shadow-fill survival + realized edge at gate_L."""
    from shadow_fill import build_trajectories, shadow_fill as sf_grid, capturable as sf_capturable
    eps = build_episodes(recs, sessions)
    build_trajectories(recs, sessions, eps)
    oc_rows, skips = open_classes(recs, sessions)
    oc = {(r["market"], r["open_t"]): r for r in oc_rows}
    close_tag = {}
    for o, c in episodes_with_px(recs, sessions):
        tag, _, _ = attribute(o.get("dir"), o.get("px"), c.get("px"))
        if tag:
            close_tag[(o["market"], o["t"])] = tag

    cap = sf_capturable(eps, edge_min)                  # decision-relevant cohort ([L20] shared gate)
    per_cls = {}
    for e in cap:
        r = oc.get((e["market"], e["open_t"]))
        per_cls.setdefault(r["cls"] if r else "unclassified", []).append(e)

    out = {"edge_min": edge_min, "gate_L": list(gate_L), "n_capturable": len(cap),
           "open_class_skips": skips, "n_opens_classified": len(oc_rows), "classes": {}}
    for cls, es in sorted(per_cls.items()):
        durs = sorted(e["duration"] for e in es)
        tags = [close_tag.get((e["market"], e["open_t"])) for e in es]
        n_tag = sum(1 for t in tags if t)
        stats = {"n": len(es),
                 "dur_median": durs[len(durs) // 2] if durs else None,
                 "dur_mean": sum(durs) / len(durs) if durs else None,
                 "close_attributed": n_tag,
                 "close_toxic_share": (sum(1 for t in tags if t == "dear_fell") / n_tag) if n_tag else None,
                 "by_cat": {}}
        for e in es:
            stats["by_cat"][e["cat"]] = stats["by_cat"].get(e["cat"], 0) + 1
        rows, _ = sf_grid(es, edge_min, latencies=gate_L)
        stats["shadow"] = {str(r["L"]): {"survival": r["survival"], "realized_median": r["realized_median"],
                                         "realized_mean": r["realized_mean"], "n_survived": r["n_survived"]}
                           for r in rows}
        out["classes"][cls] = stats

    cm, dm = out["classes"].get("cheap_made"), out["classes"].get("dear_made")
    if cm and dm and cm["close_attributed"] and dm["close_attributed"]:
        out["toxicity_z_cheap_vs_dear"] = _z_two_prop(
            round(cm["close_toxic_share"] * cm["close_attributed"]), cm["close_attributed"],
            round(dm["close_toxic_share"] * dm["close_attributed"]), dm["close_attributed"])
    return out


def render_gate(g):
    L = ["", "=" * 68, "DIRECTION GATE (at-open class -> outcomes; capturable cohort, "
         f"open_net >= {100*g['edge_min']:.1f}c, c2 >= 1)", "=" * 68,
         f"capturable episodes: {g['n_capturable']}   opens classified: {g['n_opens_classified']}"
         f"   skips: {g['open_class_skips']}"]
    for cls, s in g["classes"].items():
        L.append("")
        L.append(f"  [{cls}]  n={s['n']}  cats={s['by_cat']}")
        L.append(f"    duration: median {s['dur_median']:.2f}s  mean {s['dur_mean']:.1f}s")
        tox = "n/a" if s["close_toxic_share"] is None else f"{100*s['close_toxic_share']:.0f}% (n={s['close_attributed']})"
        L.append(f"    close toxic (dear_fell) share: {tox}")
        for lk, sv in s["shadow"].items():
            L.append(f"    shadow-fill @L={lk}s: survival {100*sv['survival']:.1f}%  "
                     f"realized median {100*sv['realized_median']:.2f}c mean {100*sv['realized_mean']:.2f}c"
                     f"  (n_surv {sv['n_survived']})")
    z = g.get("toxicity_z_cheap_vs_dear")
    if z is not None:
        L.append("")
        L.append(f"  toxic-share difference cheap_made vs dear_made: z = {z:.2f} (two-proportion, pooled)")
    return "\n".join(L)


def report(data_dir, post_epoch_only=False, edge_min=0.01, json_out=None):
    recs, sessions = load(data_dir)
    if post_epoch_only:
        n0 = len(recs)
        recs = [r for r in recs if r["t"] >= ECON_REMAP_DEPLOY_TS]
        note = f"POST-EPOCH: {len(recs)}/{n0} records at t >= {ECON_REMAP_DEPLOY_TS} (0013 stamps)"
    else:
        note = None
    a = analyze(recs, sessions)
    L = ["=" * 68, "ADVERSE SELECTION  (which venue moved when the edge closed?)", "=" * 68]
    if note:
        L.append(note)
    L.append(f"transitions read       : {len(recs)}")
    L.append(f"OPEN->CLOSE pairs       : {a['open_close_pairs']}  (all dirs: weather/econ P,K + sports PK,KP)")
    L.append(f"  with px on both ends  : {a['with_px']}")
    if a["attributed"] == 0:
        L += ["", "No attributable episodes yet. The `px` field (per-venue YES touches) is logged by bot/monitor.py",
              "as of the 2026-06-09 enhancement; data collected BEFORE that deploy lacks it. Re-pull",
              "after redeploy, then re-run -- the dominant close-direction is the leg you'd most often MISS."]
        return "\n".join(L)
    n = a["attributed"]; c = a["counts"]
    L.append(f"  attributed            : {n}")
    L += ["", f"  CHEAP-ROSE (cheap was stale-low; you'd miss the CHEAP leg) : {c['cheap_rose']:4d}  ({100*c['cheap_rose']/n:.0f}%)",
          f"  DEAR-FELL  (cheap was RIGHT/informed; you'd miss the DEAR leg): {c['dear_fell']:4d}  ({100*c['dear_fell']/n:.0f}%)  <- TOXIC",
          f"  mixed/other                                                  : {c['mixed']:4d}  ({100*c['mixed']/n:.0f}%)",
          "", f"  median |cheap-side move| {a['med_cheap_move']}   median |dear-side move| {a['med_dear_move']}"]
    L.append("")
    L.append("  by category (cheap_rose / dear_fell / mixed):")
    for cat, cc in sorted(a["by_cat"].items()):
        tot = sum(cc.values())
        L.append(f"    {cat:8} n={tot:5d}  {cc['cheap_rose']:5d} ({100*cc['cheap_rose']/tot:.0f}%) / "
                 f"{cc['dear_fell']:5d} ({100*cc['dear_fell']/tot:.0f}%) / {cc['mixed']:4d} ({100*cc['mixed']/tot:.0f}%)")
    tox = c["dear_fell"] / n
    L += ["", f"  toxic-close share = {tox*100:.0f}%.  High => the cheap quote you lift is the CORRECT price and the",
          "  dear leg is the mover (you fill the wrong leg, miss the right one). Low/symmetric => benign line-lag."]
    g = gate_report(recs, sessions, edge_min=edge_min)
    L.append(render_gate(g))
    if json_out:
        import json as _json
        with open(json_out, "w", encoding="utf-8") as f:
            _json.dump({"close_attribution": a, "gate": g}, f, indent=1)
        L.append(f"\njson -> {json_out}")
    return "\n".join(L)


def _selftest():
    # --- binary close attribution (unchanged behavior) ---
    # dir P: gap = k_yb - p_ya. CHEAP-ROSE: pmus ask 0.60->0.66 (rose .06), Kalshi bid 0.67->0.66 (fell .01)
    t, cm, dm = attribute("P", {"p_ya": 0.60, "k_yb": 0.67}, {"p_ya": 0.66, "k_yb": 0.66})
    assert t == "cheap_rose" and abs(cm - 0.06) < 1e-9, (t, cm)
    # DEAR-FELL: pmus ask 0.60->0.61 (rose .01), Kalshi bid 0.67->0.61 (fell .06) -> dear was the mover (toxic)
    t, cm, dm = attribute("P", {"p_ya": 0.60, "k_yb": 0.67}, {"p_ya": 0.61, "k_yb": 0.61})
    assert t == "dear_fell" and abs(dm - 0.06) < 1e-9, (t, dm)
    # dir K: gap = p_yb - k_ya. DEAR-FELL: pmus bid 0.70->0.64 (fell .06), Kalshi ask 0.63->0.64 (rose .01)
    t, _, dm = attribute("K", {"k_ya": 0.63, "p_yb": 0.70}, {"k_ya": 0.64, "p_yb": 0.64})
    assert t == "dear_fell" and abs(dm - 0.06) < 1e-9, (t, dm)
    # missing px -> not attributed (incl. the sports dirs on empty px)
    assert attribute("PK", {}, {})[0] is None
    assert attribute("P", {"p_ya": None, "k_yb": 0.6}, {"p_ya": 0.6, "k_yb": 0.6})[0] is None

    # --- sports close attribution (PK/KP via implied A-space; verified vs bot/monitor.py semantics) ---
    # PK: cheap = pm_a, dear = 1-kb. kb (Kalshi B-ask) 0.45->0.52 => implied A-bid 0.55->0.48 fell .07 -> TOXIC
    t, cm, dm = attribute("PK", {"pm_a": 0.50, "kb": 0.45}, {"pm_a": 0.50, "kb": 0.52})
    assert t == "dear_fell" and abs(dm - 0.07) < 1e-9 and abs(cm) < 1e-9, (t, cm, dm)
    # KP: cheap = ka, dear = pm_b. ka 0.03->0.05 (rose .02), pm_b flat -> benign laggard repricing
    t, cm, dm = attribute("KP", {"ka": 0.03, "pm_b": 0.06}, {"ka": 0.05, "pm_b": 0.06})
    assert t == "cheap_rose" and abs(cm - 0.02) < 1e-9, (t, cm)

    # --- at-open attribution ---
    # cheap side fell .06 vs dear rose .01 -> the cheap venue's move created the edge
    t, cf, dr = open_attrib("P", {"p_ya": 0.66, "k_yb": 0.66}, {"p_ya": 0.60, "k_yb": 0.67})
    assert t == "cheap_made" and abs(cf - 0.06) < 1e-9 and abs(dr - 0.01) < 1e-9, (t, cf, dr)
    # dear side rose .07, cheap flat -> the dear venue moved away (cheap is the laggard)
    t, cf, dr = open_attrib("P", {"p_ya": 0.60, "k_yb": 0.60}, {"p_ya": 0.60, "k_yb": 0.67})
    assert t == "dear_made" and abs(dr - 0.07) < 1e-9, (t, dr)
    # sports KP: ka fell .02 -> cheap_made
    t, cf, dr = open_attrib("KP", {"ka": 0.05, "pm_b": 0.04}, {"ka": 0.03, "pm_b": 0.04})
    assert t == "cheap_made" and abs(cf - 0.02) < 1e-9, (t, cf)
    assert open_attrib("P", {}, {"p_ya": 0.6, "k_yb": 0.6})[0] is None

    # --- end-to-end pairing + aggregation on synthetic transitions (by_cat now included) ---
    recs = [
        {"t": 1, "market": "tc-x", "transition": "OPEN",  "dir": "P", "px": {"p_ya": 0.60, "k_yb": 0.67}},
        {"t": 5, "market": "tc-x", "transition": "CLOSE", "dir": "P", "px": {"p_ya": 0.61, "k_yb": 0.61}},  # dear_fell
        {"t": 2, "market": "tc-y", "transition": "OPEN",  "dir": "P", "px": {"p_ya": 0.50, "k_yb": 0.58}},
        {"t": 6, "market": "tc-y", "transition": "CLOSE", "dir": "P", "px": {"p_ya": 0.57, "k_yb": 0.57}},  # cheap_rose
    ]
    a = analyze(recs)
    assert a["attributed"] == 2 and a["counts"]["dear_fell"] == 1 and a["counts"]["cheap_rose"] == 1, a
    assert a["by_cat"]["weather"]["dear_fell"] == 1, a["by_cat"]
    # a pair spanning a censoring event (restart at t=3) is dropped: tc-x (1..5) spans it, tc-y (2..6) too;
    # with the restart at t=5.5 only tc-y spans it and tc-x is kept.
    assert analyze(recs, sessions=[3])["attributed"] == 0
    a2 = analyze(recs, sessions=[5.5])
    assert a2["attributed"] == 1 and a2["counts"]["dear_fell"] == 1, a2

    # --- open_classes: prior walk + censor gap + no-prior ---
    seq = [
        {"t": 1,  "market": "tc-x", "transition": "CLOSE", "dir": "P", "px": {"p_ya": 0.66, "k_yb": 0.66}},
        {"t": 5,  "market": "tc-x", "transition": "OPEN",  "dir": "P", "px": {"p_ya": 0.60, "k_yb": 0.67}},  # vs t=1: cheap_made
        {"t": 7,  "market": "tc-z", "transition": "OPEN",  "dir": "P", "px": {"p_ya": 0.50, "k_yb": 0.58}},  # no prior
        {"t": 9,  "market": "tc-x", "transition": "CLOSE", "dir": "P", "px": {"p_ya": 0.66, "k_yb": 0.66}},
        {"t": 12, "market": "tc-x", "transition": "OPEN",  "dir": "P", "px": {"p_ya": 0.60, "k_yb": 0.67}},  # censor at t=10 -> skipped
    ]
    rows, skips = open_classes(seq, sessions=[10])
    assert len(rows) == 1 and rows[0]["cls"] == "cheap_made" and abs(rows[0]["gap_s"] - 4) < 1e-9, (rows, skips)
    assert skips == {"no_prior": 1, "censor_gap": 1, "missing_fields": 0}, skips

    # --- gate_report end-to-end: class -> (duration, toxicity, shadow-fill) join ---
    # M1: dear_made open, lives 100s, closes cheap_rose (benign). M2: cheap_made open, dies in 0.4s, dear_fell.
    # Stamped POST-epoch (like the real data the gate runs on) so no flush-lag correction applies.
    E = ECON_REMAP_DEPLOY_TS
    g_recs = [
        {"t": E + 0.0,   "market": "tc-m1", "transition": "CLOSE", "dir": "P", "net_edge": -0.02,
         "px": {"p_ya": 0.50, "k_yb": 0.50}},
        {"t": E + 10.0,  "market": "tc-m1", "transition": "OPEN",  "dir": "P", "net_edge": 0.03,
         "depth": {"c2": 50, "c1": 50, "c0": 50}, "px": {"p_ya": 0.50, "k_yb": 0.55}},                     # dear rose -> dear_made
        {"t": E + 110.0, "market": "tc-m1", "transition": "CLOSE", "dir": "P", "net_edge": -0.01,
         "px": {"p_ya": 0.55, "k_yb": 0.55}},                                                              # cheap rose -> benign
        {"t": E + 20.0,  "market": "tc-m2", "transition": "CLOSE", "dir": "P", "net_edge": -0.02,
         "px": {"p_ya": 0.60, "k_yb": 0.60}},
        {"t": E + 30.0,  "market": "tc-m2", "transition": "OPEN",  "dir": "P", "net_edge": 0.03,
         "depth": {"c2": 50, "c1": 50, "c0": 50}, "px": {"p_ya": 0.55, "k_yb": 0.60}},                     # cheap fell -> cheap_made
        {"t": E + 30.4,  "market": "tc-m2", "transition": "CLOSE", "dir": "P", "net_edge": -0.01,
         "px": {"p_ya": 0.55, "k_yb": 0.55}},                                                              # dear fell -> TOXIC
    ]
    g_recs.sort(key=lambda r: r["t"])
    g = gate_report(g_recs, [], edge_min=0.01, gate_L=(0.15, 1.0))
    dm_, cm_ = g["classes"]["dear_made"], g["classes"]["cheap_made"]
    assert dm_["n"] == 1 and abs(dm_["dur_median"] - 100.0) < 1e-6 and dm_["close_toxic_share"] == 0.0, dm_
    assert cm_["n"] == 1 and abs(cm_["dur_median"] - 0.4) < 1e-6 and cm_["close_toxic_share"] == 1.0, cm_
    assert dm_["shadow"]["1.0"]["survival"] == 1.0, dm_["shadow"]       # alive at 1s
    assert cm_["shadow"]["0.15"]["survival"] == 1.0 and cm_["shadow"]["1.0"]["survival"] == 0.0, cm_["shadow"]
    print("OK - adverse_selection: close attribution (P/K + PK/KP), at-open gate classes, censor-aware")
    print("     pairing, and the gate_report class->outcome join (duration / toxicity / shadow-fill)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="adverse-selection (which venue moved on edge close) + direction gate")
    ap.add_argument("--data-dir", default=os.path.join(os.path.dirname(__file__), "..", "..", "data", "cross-arb"))
    ap.add_argument("--post-epoch", action="store_true",
                    help="post-0013 records only (detection-time stamps; sub-second numbers are real)")
    ap.add_argument("--edge-min", type=float, default=0.01, help="capturable gate for the direction-gate section")
    ap.add_argument("--json-out", default=None, help="dump attribution + gate stats to this JSON path")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        _selftest(); sys.exit(0)
    dd = os.path.abspath(a.data_dir)
    if not glob.glob(os.path.join(dd, "transitions-*.jsonl*")):
        print(f"no data at {dd} - run `pwsh deploy/pull-data.ps1` first, or `--selftest`."); sys.exit(0)
    print(report(dd, post_epoch_only=a.post_epoch, edge_min=a.edge_min, json_out=a.json_out))
