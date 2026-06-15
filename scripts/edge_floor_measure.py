"""scripts/edge_floor_measure.py — does the 2.0c LIVE edge floor (0023) leave FILLABLE money on the table vs
1.5c? Measured DIRECTLY from the ladders log (both-venue top-5 depth), not assumed.

The question (the owner's "are we too strict?"): of the weather TAKER-arb opportunities whose net edge lands in
the [1.5, 2.0)c band — which a 1.5c floor would TAKE but the 2.0c floor SKIPS — how many are actually FILLABLE
(pmus has >= clip depth at the edge, since pmus is the thin binding leg) and how much per-contract net is
forgone? If the band is thin/un-fillable, the floor is NOT the constraint (liquidity is, L40) and lowering it
just diversifies into un-fillable/fee-eaten arbs (the 0012/0014 finding). If it's fillable money, 2c is too
strict and 1.5c is justified.

A weather pair's two TAKER directions (1:1, both legs cross the touch), net cents/contract-pair, marginal fees:
  PK = buy YES@pmus(ask) + NO@Kalshi(=1-Kbid): gross = Kbid - Pask; fillable = min(pmus-ask vol, Kalshi-bid vol).
  KP = buy YES@Kalshi(ask) + NO@pmus(=1-Pbid): gross = Pbid - Kask; fillable = min(Kalshi-ask vol, pmus-bid vol).
An OPPORTUNITY = a contiguous episode (per market+dir) where the best-dir net > a low MIN_C; we bucket each
episode by its PEAK net and record the fillable depth at that peak. Weather only (the only live category today),
~5 days. PRELIMINARY (activity-/cadence-limited like maker_feasibility); read-only.

  python scripts/edge_floor_measure.py --selftest
  python scripts/edge_floor_measure.py [--data-dir PATH] [--clip 1]
"""
import os, sys, argparse, collections, statistics

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from p_hedge_measure import load_ladders, best          # ladders loader + (best_price_c, vol) — reuse
from maker_feasibility import kalshi_fee_c, pmus_fee_c   # verified taker fee formulas — reuse

MIN_C = 0.5                  # an episode opens when the best-dir net clears this low threshold (captures the band)
BUCKETS = [(0.5, 1.0), (1.0, 1.5), (1.5, 2.0), (2.0, 2.5), (2.5, 4.0), (4.0, 99.0)]


def dir_net_and_depth(rec, direction):
    """(net_c, fillable_depth) for one taker direction at a ladder rec, or None if a needed touch is unquoted."""
    pb_p, pb_v = best(rec.get("pb")); pa_p, pa_v = best(rec.get("pa"))
    kb_p, kb_v = best(rec.get("kb")); ka_p, ka_v = best(rec.get("ka"))
    if direction == "PK":
        if pa_p is None or kb_p is None:
            return None
        gross = kb_p - pa_p
        fee = pmus_fee_c(pa_p / 100, taker=True) + kalshi_fee_c((100 - kb_p) / 100, taker=True)
        return gross - fee, min(pa_v, kb_v)
    if ka_p is None or pb_p is None:
        return None
    gross = pb_p - ka_p
    fee = kalshi_fee_c(ka_p / 100, taker=True) + pmus_fee_c((100 - pb_p) / 100, taker=True)
    return gross - fee, min(ka_v, pb_v)


def episodes(ladders):
    """Per market+dir, contiguous net>MIN_C runs -> one (peak_net_c, depth_at_peak) per episode."""
    out = []
    for m, obs in ladders.items():
        cur = {"PK": None, "KP": None}                  # dir -> {peak, depth} while in an episode, else None
        for _t, rec in obs:
            for d in ("PK", "KP"):
                r = dir_net_and_depth(rec, d)
                net = r[0] if r else None
                if net is not None and net > MIN_C:
                    if cur[d] is None or net > cur[d]["peak"]:
                        cur[d] = {"peak": net, "depth": r[1]} if cur[d] is None or net > cur[d]["peak"] else cur[d]
                    if cur[d] is None:
                        cur[d] = {"peak": net, "depth": r[1]}
                else:
                    if cur[d] is not None:
                        out.append((m, d, cur[d]["peak"], cur[d]["depth"]))
                        cur[d] = None
        for d in ("PK", "KP"):
            if cur[d] is not None:
                out.append((m, d, cur[d]["peak"], cur[d]["depth"]))
    return out


def report(args):
    P = print
    ladders = load_ladders(args.data_dir)
    eps = episodes(ladders)
    P("=" * 96)
    P("EDGE-FLOOR MEASUREMENT — is 2.0c too strict vs 1.5c? (weather taker arbs, from the ladders log)")
    P("=" * 96)
    P(f"weather markets: {len(ladders)} | episodes (peak net > {MIN_C}c): {len(eps)} | clip={args.clip}\n")
    P(f"  {'net band':>12} | {'episodes':>8} {'fillable':>8} {'%fill':>6} {'med-depth':>9} {'forgone/ctr':>11}")
    P("  " + "-" * 64)
    summary = {}
    for lo, hi in BUCKETS:
        b = [e for e in eps if lo <= e[2] < hi]
        fill = [e for e in b if e[3] >= args.clip]
        depths = [e[3] for e in b]
        forgone = sum(e[2] for e in fill)              # per-contract net we'd capture if filled (clip=1)
        label = f"[{lo:.1f},{hi:.1f})" if hi < 99 else f"[{lo:.1f}+   )"
        P(f"  {label:>12} | {len(b):>8} {len(fill):>8} {100*len(fill)/len(b) if b else 0:>5.0f}% "
          f"{statistics.median(depths) if depths else 0:>9.0f} {forgone:>10.1f}c")
        summary[label] = {"episodes": len(b), "fillable": len(fill), "forgone_c": round(forgone, 1)}
    band = [e for e in eps if 1.5 <= e[2] < 2.0]
    band_fill = [e for e in band if e[3] >= args.clip]
    cap = [e for e in eps if e[2] >= 2.0]
    cap_fill = [e for e in cap if e[3] >= args.clip]
    P("\n" + "-" * 96)
    P("THE 1.5c QUESTION — the [1.5, 2.0)c band is exactly what dropping the floor 2.0->1.5 would ADD:")
    P(f"  band episodes: {len(band)}; FILLABLE (pmus depth >= {args.clip}): {len(band_fill)} "
      f"({100*len(band_fill)/len(band) if band else 0:.0f}%); forgone net = {sum(e[2] for e in band_fill):.1f}c/contract total")
    P(f"  vs what we ALREADY capture (>=2.0c): {len(cap)} episodes, {len(cap_fill)} fillable, "
      f"{sum(e[2] for e in cap_fill):.1f}c/contract")
    if cap_fill:
        lift = 100 * len(band_fill) / len(cap_fill)
        P(f"  => 1.5c would add ~{len(band_fill)} fillable arbs (~+{lift:.0f}% by count) worth "
          f"~{sum(e[2] for e in band_fill):.1f}c/contract, IF those fills actually clear (median depth "
          f"{statistics.median([e[3] for e in band]) if band else 0:.0f}).")
    P("  READ: a LARGE fillable band -> 2c IS leaving money -> lower to 1.5c. A SMALL/thin band -> the floor")
    P("  isn't the constraint (liquidity is); 1.5c just adds fee-eaten/un-fillable arbs (0012/0014). Caveat:")
    P("  'fillable' = displayed depth; live FOK fills evaporate (the 94-fire finding), so this OVER-states fills.")


def _selftest():
    print("edge_floor_measure self-test")
    # PK: pmus ask 40, Kalshi bid 45 -> gross 5c; KP: Kalshi ask 47, pmus bid 42 -> gross -5c (no arb)
    rec = {"pb": [[42, 100.0]], "pa": [[40, 7.0]], "kb": [[45, 900.0]], "ka": [[47, 50.0]]}
    pk = dir_net_and_depth(rec, "PK")
    assert pk is not None and 2.0 < pk[0] < 2.2 and pk[1] == 7.0, pk          # gross 5c - ~2.9c taker fees = ~2.07c; depth=min(7,900)=7
    kp = dir_net_and_depth(rec, "KP")
    assert kp is not None and kp[0] < 0, kp                                   # pb 42 < ka 47 -> negative
    assert dir_net_and_depth({"pb": [], "pa": [], "kb": [[45, 9.0]], "ka": []}, "PK") is None  # pmus ask unquoted
    # episode: two snapshots in one PK episode -> ONE episode at the PEAK net (the wider one)
    rec2 = {"pb": [[42, 100.0]], "pa": [[38, 3.0]], "kb": [[45, 900.0]], "ka": [[47, 50.0]]}  # gross 7c (wider)
    eps = episodes({"m": [(1.0, rec), (2.0, rec2)]})
    pk_eps = [e for e in eps if e[1] == "PK"]
    assert len(pk_eps) == 1 and pk_eps[0][2] > 3.0 and pk_eps[0][3] == 3.0, pk_eps   # peak NET at rec2 (~4.1c) + its depth
    # a snapshot that drops below MIN_C closes the episode
    flat = {"pb": [[42, 1.0]], "pa": [[44, 1.0]], "kb": [[43, 1.0]], "ka": [[45, 1.0]]}  # PK gross 43-44<0
    eps2 = episodes({"m": [(1.0, rec), (2.0, flat), (3.0, rec)]})
    assert sum(1 for e in eps2 if e[1] == "PK") == 2, "two separate PK episodes split by the flat snapshot"
    print("  OK taker net/depth (PK/KP), unquoted-skip, episode peak+depth, episode split")
    print("all self-tests passed")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--data-dir", default=os.path.join(HERE, "..", "..", "data", "cross-arb"))
    ap.add_argument("--clip", type=int, default=1)
    args = ap.parse_args()
    if args.selftest:
        _selftest()
    else:
        report(args)
