"""scripts/flip_add_backtest.py — READ-ONLY feasibility backtest for TWO UNBUILT strategy extensions.

The live bot (bot-rs) takes ONE position per market and HOLDS to settlement (re-entry is blocked while a
position is held, bot-rs/src/main.rs:497-505). This measures whether building either extension would add
profit on the historical persistence data — it is a go/no-go on BUILDING, not tuning anything live.

  (1) FLIP (close + reverse): a filled arb (buy YES cheap-venue + NO dear-venue, edge tau1) later has its
      cross-venue BASIS flip — the cheap/dear venues swap and a BIGGER reverse arb (edge tau2 > tau1)
      appears. CLOSE the original (sell both legs at the flip-time bids, banking the MTM overshoot) AND
      enter the reverse arb to settlement.  A locked pair is OUTCOME-neutral but NOT BASIS-neutral ([L30]).
  (2) ADD (scale-in): a filled arb later has a BIGGER SAME-DIRECTION arb appear while still held -> add a
      second locked pair (more size) at the better edge, both held to settlement.

WHAT IT REPORTS (per category weather/sports/econ/soccer3=WC + overall):
  (1) FLIP BASE RATE  — of filled arbs (capturable OPENs, AFTER capital_sim's phantom filters), the
      fraction that later FLIP to a BIGGER reverse arb before settlement. The GATING number: ~0 => moot.
  (2) FLIP PnL        — close+reverse vs HOLD, per flipped arb + aggregate. The close mark comes from the
      REAL flip-time touch prices in the transition record's `px`, priced through the same fee model.
  (3) ADD PnL         — markets that later offered a bigger same-direction arb while held; the incremental
      net edge of the add (sized by crossable depth c2, per-market cap), vs single-entry, PLUS the
      correlated-settlement concentration it creates (N pairs on one bucket all lose if grading diverges).
      Three audit-required disclosures (stats-ml-logic-reviewer 20260614-flip-add) so the figure is never
      quoted bare: (3a) a HOLD-WINDOW SWEEP — the add count/PnL across assumed first-pair settlement holds
      {4,8,12,24,28,48}h (an add only counts if the bigger arb lands before the first pair settles, so a
      longer hold inflates it); (3b) the [L20] flat-ladder PHANTOM LENS on the driving WIDEN (the base
      cohort gets it via capturable(); the WIDEN record bypasses that chokepoint) — PnL before/after + the
      dropped count; (3c) the TRUE-ADD vs RE-ENTRY split against the REAL episode OPEN/CLOSE intervals — a
      genuine scale-in (base edge still open) vs a separate later arb on a still-held bucket (re-entry, which
      the live one-position-per-slug guard blocks and which concentrates correlated settlement risk).

METHODOLOGY (this project has been bitten — tasks/lessons.md L19/L20/L21/L28/L30):
  • REUSES the proven harness — load/build_episodes/category + capturable/one_per_market/settle_t/
    void_haircut + the ledger fee model (signal/pfee/kfee, and the Ledger accounting core for the
    YES/NO weather+econ close-mark). capturable() applies the [L20] restart/flat phantom filters and the
    c2>=1 floor; load() quarantines the [L21] off-by-one econ phantom. A fresh harness that re-admitted
    these inflated the last backtest 57% ([L28]) — so nothing here re-derives a "clean" cohort.
  • FIELD SHAPES verified against REAL records before computing ([L28]): a FLIP is a logged transition
    whose `px` carries the per-venue touches. TWO shapes exist and are NOT interchangeable:
      weather/econ (MarketTracker)  px = {p_yb,p_ya,k_yb,k_ya}, dir in {"P","K"}  (SINGLE letter — [L28] trap)
      sports      (GameTracker)     px = {pm_b,pm_a,ka,kb},     dir in {"PK","KP"}
    Any touch may be None (one-sided book) — None is handled, never assumed present.
  • HONEST ([L15]/[L19]): reports the FUNNEL (filled -> flipped -> profitable-to-flip), the effective-n,
    and labels everything PAPER/GROSS. FLIP is gross of latency / slippage / the ~17-55% naked-leg risk on
    the EXTRA fills (4 for a flip vs 0 for holding) and lands in the fat-fast ~66%-toxic regime — stated,
    not buried. tau is NOT oracle-tuned on the reported data.

  python scripts/flip_add_backtest.py --selftest
  python scripts/flip_add_backtest.py [--edge-min 0.0] [--window-min 0] [--max-clip 1000] [--add-cap-frac 0.20]
"""
import os, sys, argparse
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bot"))
from analyze_persistence import load, build_episodes, category, _pct
from capital_sim import capturable, one_per_market, settle_t, void_haircut, DEPTH_BOUNDARY_NET
from ledger import signal, pfee, kfee, Ledger
from monitor import game_edge

# A FLIP's `px` shape tells us which tracker produced it -> which close-mark model to use.
def px_is_game(px):
    """sports GameTracker px = {pm_b,pm_a,ka,kb}; weather/econ MarketTracker px = {p_yb,p_ya,k_yb,k_ya}."""
    return px is not None and "pm_a" in px


# ============================================================================================
# CLOSE-MARK  — what selling BOTH legs of the held arb at the flip-time bids realizes, net of taker fees.
# Computed from the REAL touch prices in the flip record's `px`, NOT assumed. Returns the close P&L per
# contract = (sell proceeds) - (entry cost) - (entry fees) - (close fees), or None if a leg is unsellable
# (a touch needed to sell is missing at flip time -> the close can't be priced; we do NOT fabricate it).
# ============================================================================================
def close_pnl_weather(entry_px, flip_px, direction):
    """YES/NO arb (dir 'P' = YES@P + NO@K ; 'K' = YES@K + NO@P). Reuse the Ledger accounting core so fees
    + signs match the bot exactly: enter at entry_px, then unwind_all at flip_px. Returns per-contract
    realized cash (negative entry cost folded in) or None if the entry or the close leg is unpriceable."""
    L = Ledger("flip")
    try:
        L.enter(entry_px, 1, direction=direction)          # books the original pair (raises if no +edge)
    except (ValueError, KeyError, TypeError):
        return None
    # both legs must be sellable at the flip touches: dir P sells YES@P (needs p_yb) + NO@K (needs k_ya);
    # dir K sells YES@K (needs k_yb) + NO@P (needs p_ya). A missing touch => can't close => skip (no fabrication).
    need = ("p_yb", "k_ya") if direction == "P" else ("k_yb", "p_ya")
    if any(flip_px.get(k) is None for k in need):
        return None
    L.unwind_all(flip_px)                                  # sells both legs at the flip bids, pays taker fees
    return L.cash                                          # cash = -entry_cost -entry_fees +sale -sale_fees


def close_pnl_game(entry_px, flip_px, direction):
    """2-outcome game arb (dir 'PK' = back A@P + B@K ; 'KP' = back A@K + B@P), priced the GameTracker way:
    A is the pm-YES side, B is the pm-NO side. ENTRY pays the two backed asks; CLOSE sells each backed leg
    at the flip-time bid for that leg. Per-contract realized cash, or None if unpriceable.

    Leg prices (pm YES = team A; Kalshi single-team YES asks ka/kb):
      back A@P -> pay pm_a (ask) ; sell back at pm_b (bid)
      back A@K -> pay ka (yes ask); sell back at the Kalshi A YES bid — NOT in `px` (only ka/kb asks logged)
      back B@P -> pay (1-pm_b)   ; sell back at (1-pm_a)  [pm-NO = 1 - pm-YES]
      back B@K -> pay kb (yes ask); sell back at the Kalshi B YES bid — NOT in `px`
    The Kalshi YES BID is not in the transition `px` (only the two team YES ASKs are logged), so a leg
    backed on Kalshi cannot be marked-to-close from this record. We therefore price the close ONLY when
    both backed legs are on pm — which never happens (a cross-venue arb always has one Kalshi leg). So
    close_pnl_game returns None: the sports close mark is NOT computable from the logged fields, and we
    say so rather than inventing a Kalshi bid (the [L28]/[L18] discipline — no convenient proxy)."""
    return None


def reverse_net_edge(flip_px):
    """Net edge of the NEW (reverse) arb at flip time, from the same signal()/game_edge the monitor logs.
    The FLIP record already carries net_edge, but we recompute from px so the funnel is self-consistent and
    robust to any record without a stored net. Returns (dir, net) or (None, None) if unpriceable."""
    if px_is_game(flip_px):
        g = game_edge(flip_px.get("pm_b"), flip_px.get("pm_a"), flip_px.get("ka"), flip_px.get("kb"))
        return (g["dir"], g["net"]) if g else (None, None)
    s = signal(flip_px)                                   # signal() returns net_edge (game_edge returns net)
    return s["dir"], s["net_edge"]


# ============================================================================================
# FLIP DETECTION — reconstruct, per market, each filled arb's episode and whether it FLIPs to a BIGGER
# reverse arb. We key on the monitor's own logged FLIP transition (the project's definition of a flip,
# debounced live) — NOT a hand-rolled reversal heuristic. A same-direction WIDEN must NOT count.
# ============================================================================================
def flip_events(records, episodes, edge_min, window_min, tau_gain, settle_offset_h=28.0):
    """For each capturable filled arb (a one-per-market episode passing capital_sim.capturable), find the
    FIRST logged FLIP DURING THE HELD POSITION'S LIFETIME — [open_t, settlement] — that is a bigger arb in
    the OPPOSITE direction to the held position: rdir != dir0 AND (reverse net - original net) >= tau_gain.
    Returns dicts: {market, cat, dir0, net0, flip_t, dir1, net1, entry_px, flip_px, close_pnl, has_close}.

    WINDOW = [open_t, settle_t]: under the hold-to-settlement base case the pair is entered at the first
    capturable open and HELD TO SETTLEMENT — a logged CLOSE means the EDGE STATE left the book, NOT that
    the locked pair was sold. So a flip is actionable anywhere up to settlement, not only inside the first
    edge episode (scoping to [open_t, episode close_t] missed 65/68 logged flips — they land in LATER edge
    episodes of the same still-held market). entry_px = the OPEN record's px; flip_px = the FLIP record's
    px (the real flip-time touches). close_pnl = close_pnl_weather/game(entry_px, flip_px, dir0)."""
    filled = {e["market"]: e for e in
              one_per_market(capturable(episodes, edge_min, window_min))}     # phantom-filtered filled arbs
    # index OPEN (px + ORIGINAL dir) + FLIP records per market from the raw transition stream. NOTE: the
    # episode's `dir` is MUTATED to the post-flip direction by build_episodes, so the ENTRY direction must
    # come from the OPEN record, not ep["dir"] — using ep["dir"] would mark the close on the wrong leg.
    open_rec, flips = {}, {}
    for r in records:
        m, lab = r["market"], r["transition"]
        if lab == "OPEN":
            open_rec.setdefault(m, []).append((r["t"], r.get("px"), r.get("dir")))
        elif lab == "FLIP":
            flips.setdefault(m, []).append((r["t"], r.get("px"), r.get("dir"), r.get("net_edge")))

    out = []
    for m, ep in filled.items():
        st = settle_t(m, ep["open_t"], settle_offset_h)        # held to settlement, not to edge-close
        cand = sorted([f for f in flips.get(m, []) if ep["open_t"] <= f[0] <= st],
                      key=lambda f: f[0])                      # earliest flip first (px dict isn't comparable)
        if not cand:
            continue
        # entry px + ORIGINAL dir = the OPEN record that started THIS episode (closest open at/before open_t).
        opx, dir0 = None, None
        for ot, p, od in open_rec.get(m, []):
            if ot <= ep["open_t"] + 1e-6:
                opx, dir0 = p, od
        if opx is None or dir0 is None:
            continue
        net0 = ep["open_net"]
        chosen = None
        for ft, fpx, fdir, fnet in cand:
            if fpx is None:
                continue
            rdir, rnet = reverse_net_edge(fpx)
            if rdir is None or rnet is None:
                continue
            # A genuine FLIP requires the bigger arb to be in the OPPOSITE direction to the HELD position
            # (rdir != dir0) — a cross-over you'd close-and-reverse into. A logged FLIP record is a reversal
            # vs the THEN-CURRENT edge state, but over a multi-flip held position the book can swing back to
            # the entry direction, so rdir can equal dir0 — that is a same-direction bigger arb (the ADD
            # path), NOT a flip. Without this guard 4/14 'flips' were already-aligned same-direction events.
            if rdir != dir0 and rnet - net0 >= tau_gain:   # OPPOSITE-direction BIGGER arb = the flip trigger
                chosen = (ft, fpx, rdir, rnet)
                break
        if chosen is None:
            continue
        ft, fpx, rdir, rnet = chosen
        cp = (close_pnl_game(opx, fpx, dir0) if px_is_game(fpx)
              else close_pnl_weather(opx, fpx, dir0))      # close on the ORIGINAL entry direction
        out.append({"market": m, "cat": ep["cat"], "dir0": dir0, "net0": net0,
                    "flip_t": ft, "dir1": rdir, "net1": rnet, "entry_px": opx, "flip_px": fpx,
                    "close_pnl": cp, "has_close": cp is not None})
    return out


# ============================================================================================
# ADD DETECTION — markets where, while the first arb is held, a BIGGER SAME-DIRECTION arb appears (a WIDEN
# past the original net by >= tau_gain). The add is a SECOND locked pair sized by the widen's crossable
# depth, capped at add_cap_frac of the per-market clip. Reports incremental edge vs single-entry + the
# correlated concentration (how many pairs end up on one settlement bucket).
#
# Three audit-required disclosures are computed PER add (stats-ml-logic-reviewer 20260614-flip-add):
#   • drop_flat_widen — the [L20] phantom lens applied to the WIDEN that drives each add. A flat-ladder
#     widen (c2==c1==c0>0) is the book-init phantom fingerprint; capturable() gates the BASE episode on it
#     but never sees the WIDEN record, so 27% of the unfiltered add PnL rode on flat-ladder widens. The
#     CAUSAL lens drops the add when its FIRST qualifying widen is a phantom (NOT falling through to a
#     later clean widen — that later widen is a separate future event; picking it would be [L19] oracle
#     look-ahead). This matches capturable(drop_flat)'s "drop the opportunity, don't substitute" discipline.
#   • true_add vs re_entry — classified against the REAL episode OPEN/CLOSE intervals (build_episodes), NOT
#     a proxy: a TRUE scale-in piles onto a still-OPEN base edge episode; a RE-ENTRY is a separate later arb
#     on a bucket merely still HELD to settlement (the one-position-per-slug guard, bot-rs main.rs:497, blocks
#     these and they concentrate correlated settlement risk). 256/348 are re-entry at the 28h hold.
#   • a flat_widen flag and is_true_add flag travel on each add dict for per-class / before-after reporting.
# ============================================================================================
def _widen_is_flat(depth):
    """A WIDEN's depth ladder is the [L20] book-init phantom fingerprint when c2==c1==c0 with depth>0 (one
    resting level mirrored down the book). depth may be None (one-sided/unmeasured) -> not flat."""
    d = depth or {}
    return bool(d) and d.get("c2", 0) == d.get("c1", -1) == d.get("c0", -2) and d.get("c2", 0) > 0


def add_events(records, episodes, edge_min, window_min, max_clip, add_cap_frac, tau_gain,
               settle_offset_h=28.0, drop_flat_widen=False):
    """Per capturable filled arb, find the FIRST (causal) same-direction WIDEN DURING THE HELD POSITION'S
    LIFETIME ([open_t, settlement], same window rationale as flip_events) whose net exceeds the original by
    >= tau_gain. Returns {market, cat, dir, net0, c2_0, net_add, c2_add, size0, size_add, inc_pnl, void,
    flat_widen, is_true_add} — size_add capped at add_cap_frac*size0; inc_pnl = size_add*(book-avg add edge
    - void). drop_flat_widen=True skips an add whose first qualifying widen is a flat-ladder phantom ([L20]
    lens on the add leg). is_true_add = the base edge episode was still OPEN at the widen time (real scale-in)
    vs a re-entry (separate later arb on a still-held bucket)."""
    filled = {e["market"]: e for e in one_per_market(capturable(episodes, edge_min, window_min))}
    # REAL episode intervals per market (from build_episodes) — the base episode whose open_t matches the
    # filled rep's open_t defines [open, close] for the true-add/re-entry classification (not the settlement
    # proxy: a logged CLOSE means the EDGE left the book = the scale-in window ended, even if held longer).
    ep_intervals = {}
    for e in episodes:
        ep_intervals.setdefault(e["market"], []).append((e["open_t"], e["close_t"]))
    # ORIGINAL entry dir per market from the OPEN record (ep["dir"] is mutated post-flip by build_episodes).
    open_dir, widens = {}, {}
    for r in records:
        if r["transition"] == "OPEN":
            open_dir.setdefault(r["market"], r.get("dir"))     # first OPEN's dir = the entry direction
        elif r["transition"] == "WIDEN":
            widens.setdefault(r["market"], []).append(
                (r["t"], r.get("net_edge"), (r.get("depth") or {}).get("c2", 0), r.get("dir"), r.get("depth")))
    out, dropped_flat = [], 0
    for m, ep in filled.items():
        d0 = open_dir.get(m, ep["dir"])
        st = settle_t(m, ep["open_t"], settle_offset_h)        # held to settlement, not to edge-close
        # key the sort EXPLICITLY on t (w[0]): the tuples carry a depth dict in the last slot, so a bare
        # sorted() raises TypeError the instant two widens share a timestamp (audit INFO-3 — hit live).
        cand = sorted([w for w in widens.get(m, [])
                       if ep["open_t"] <= w[0] <= st and w[3] == d0                   # SAME direction only
                       and w[1] is not None and w[1] - ep["open_net"] >= tau_gain],
                      key=lambda w: w[0])
        if not cand:
            continue
        # the FIRST qualifying widen (causal — what you'd actually act on), NOT the max over the day
        # (picking the peak is an oracle, [L19]).
        bt, bnet, bc2, _, bdepth = cand[0]
        flat = _widen_is_flat(bdepth)
        size0 = min(ep["open_c2"], max_clip)
        size_add = min(bc2, max_clip, int(round(add_cap_frac * size0)))             # per-pair add cap
        if size_add <= 0:
            continue
        if drop_flat_widen and flat:           # [L20] phantom lens: drop the add (don't substitute a later clean widen)
            dropped_flat += 1
            continue
        # TRUE-ADD vs RE-ENTRY: is the BASE edge episode (the one that started at ep["open_t"]) still OPEN
        # at the widen time? Classify against the real interval, not the settlement proxy.
        base = next(((ot, ct) for ot, ct in ep_intervals.get(m, []) if abs(ot - ep["open_t"]) < 1e-6), None)
        is_true_add = bool(base) and base[0] <= bt <= base[1]
        avg_edge = max(DEPTH_BOUNDARY_NET, (bnet + DEPTH_BOUNDARY_NET) / 2.0)        # walk-the-book decay (same as capital_sim)
        vh = void_haircut(m)
        inc = size_add * max(0.0, avg_edge - vh)
        out.append({"market": m, "cat": ep["cat"], "dir": d0, "net0": ep["open_net"],
                    "c2_0": ep["open_c2"], "net_add": bnet, "c2_add": bc2,
                    "size0": size0, "size_add": size_add, "inc_pnl": inc, "void": vh,
                    "flat_widen": flat, "is_true_add": is_true_add})
    return out, dropped_flat


# ============================================================================================
# REPORT
# ============================================================================================
CATS = ("weather", "sports", "econ", "soccer3", "other")
ADD_DEFAULT_OFFSET_H = 28.0                          # the prior single-value hold (kept as the headline row)
ADD_SWEEP_OFFSETS_H = (4, 8, 12, 24, 28, 48)         # hold-window sweep (audit WARN-1): an add only counts if
                                                     # the bigger arb appears before the FIRST pair settles

def report(records, episodes, edge_min, window_min, max_clip, add_cap_frac, tau_gain):
    out = []; P = out.append
    if not records:
        return "no transitions in the archive yet — nothing to backtest."
    t0, t1 = records[0]["t"], records[-1]["t"]
    span_d = max((t1 - t0) / 86400.0, 1e-9)

    filled = one_per_market(capturable(episodes, edge_min, window_min))
    by_cat_filled = {}
    for e in filled:
        by_cat_filled.setdefault(e["cat"], []).append(e)

    flips = flip_events(records, episodes, edge_min, window_min, tau_gain)
    adds, _ = add_events(records, episodes, edge_min, window_min, max_clip, add_cap_frac, tau_gain)
    flips_by_cat = {}
    for f in flips:
        flips_by_cat.setdefault(f["cat"], []).append(f)
    adds_by_cat = {}
    for a in adds:
        adds_by_cat.setdefault(a["cat"], []).append(a)

    P("=" * 92)
    P(f"FLIP / ADD FEASIBILITY BACKTEST   (span {span_d:.2f} d; PAPER/GROSS; filled = capturable OPENs, "
      f"phantom-filtered, one/market)")
    P(f"  trigger: a reverse/widen arb must beat the original net by >= {tau_gain*100:.1f}c (tau_gain) to count")
    if span_d < 4:
        P("  *** < 4 days of data: every rate/PnL below is PRELIMINARY (method demo, not validation). ***")

    # ---- (1) FLIP BASE RATE — the gating number ----
    P("")
    P("(1) FLIP BASE RATE  — of filled arbs, the fraction that later FLIP to a BIGGER reverse arb")
    P(f"  {'category':10} {'filled':>7} {'flipped':>8} {'rate':>7}   note")
    for c in CATS:
        nf = len(by_cat_filled.get(c, []))
        nflip = len(flips_by_cat.get(c, []))
        if nf == 0 and nflip == 0:
            continue
        rate = (nflip / nf * 100.0) if nf else 0.0
        note = "" if nf >= 20 else "effective-n TINY"
        P(f"  {c:10} {nf:>7} {nflip:>8} {rate:>6.1f}%   {note}")
    tot_f, tot_fl = len(filled), len(flips)
    P(f"  {'OVERALL':10} {tot_f:>7} {tot_fl:>8} {(tot_fl/tot_f*100 if tot_f else 0):>6.1f}%")
    if tot_fl == 0:
        P("  -> ZERO filled arbs flip to a bigger reverse arb. The FLIP idea is MOOT on this data "
          "(weather/econ NEVER flip — confirmed below).")

    # weather/econ structural note: do they EVER log a FLIP at all?
    n_we_flip_raw = sum(1 for r in records if r["transition"] == "FLIP"
                        and category(r["market"]) in ("weather", "econ"))
    P(f"  [raw FLIP transitions logged for weather+econ, ANY size: {n_we_flip_raw}  "
      f"(the cross-venue basis on a 1:1 bucket rarely/never crosses — matches the prior 0/14 station-days)]")

    # ---- (2) FLIP PnL — close+reverse vs HOLD ----
    P("")
    P("(2) FLIP PnL  — per flipped arb: HOLD = original net - void ; FLIP = close P&L (sell both legs at the")
    P("    real flip-time bids) + reverse-arb net - void, both to settlement. Per-CONTRACT (unit size), GROSS.")
    priced = [f for f in flips if f["has_close"]]
    if not flips:
        P("  (no flips — nothing to price)")
    else:
        P(f"  flips found {len(flips)};  with a FULLY computable close mark {len(priced)}  "
          f"[{len(flips)-len(priced)} sports flips have NO logged Kalshi YES bid -> close unpriceable, see CAVEATS]")
        # Fully-priced flips (weather/econ only): HOLD = original net - void ; FLIP = close P&L + reverse net - void.
        for c in CATS:
            fs = [f for f in flips_by_cat.get(c, []) if f["has_close"]]
            if not fs:
                continue
            hold = [f["net0"] - void_haircut(f["market"]) for f in fs]
            flip = [f["close_pnl"] + max(0.0, f["net1"] - void_haircut(f["market"])) for f in fs]
            P(f"  {c:8} n={len(fs):<3}  HOLD avg {sum(hold)/len(hold)*100:+.2f}c   "
              f"FLIP avg {sum(flip)/len(flip)*100:+.2f}c   delta {(sum(flip)-sum(hold))/len(fs)*100:+.2f}c/arb (fully priced)")
        if priced:
            hold = [f["net0"] - void_haircut(f["market"]) for f in priced]
            flip = [f["close_pnl"] + max(0.0, f["net1"] - void_haircut(f["market"])) for f in priced]
            P(f"  {'ALL':8} n={len(priced):<3}  HOLD avg {sum(hold)/len(hold)*100:+.2f}c   "
              f"FLIP avg {sum(flip)/len(flip)*100:+.2f}c   "
              f"delta {(sum(flip)-sum(hold))/len(priced)*100:+.2f}c/arb (aggregate {(sum(flip)-sum(hold))*100:+.2f}c at unit size)")
        # PARTIAL view for the unpriceable (sports) flips: the close P&L is an UNKNOWN POSITIVE term (you
        # only close into a favourable basis), so FLIP-total >= reverse-arb net. Reporting the reverse leg vs
        # HOLD lower-bounds the flip benefit WITHOUT fabricating the Kalshi-leg close mark.
        unp = [f for f in flips if not f["has_close"]]
        if unp:
            P(f"  unpriceable-close flips (sports, n={len(unp)}): close P&L is an unknown POSITIVE term -> only")
            P(f"    the reverse-arb leg is quantified (a LOWER bound on the flip's total benefit):")
            for c in CATS:
                fs = [f for f in flips_by_cat.get(c, []) if not f["has_close"]]
                if not fs:
                    continue
                hold = [f["net0"] - void_haircut(f["market"]) for f in fs]              # what holding the original banks
                rev = [max(0.0, f["net1"] - void_haircut(f["market"])) for f in fs]     # reverse leg alone (close excluded)
                P(f"    {c:8} n={len(fs):<3}  original-net (HOLD) avg {sum(hold)/len(hold)*100:+.2f}c   "
                  f"reverse-arb net avg {sum(rev)/len(rev)*100:+.2f}c   "
                  f"reverse-minus-hold {(sum(rev)-sum(hold))/len(fs)*100:+.2f}c/arb  (+ unknown close gain)")

    # ---- (3) ADD PnL — with the THREE audit-required disclosures so the figure is never quoted bare ----
    P("")
    P("(3) ADD PnL  — markets that later offered a BIGGER SAME-DIRECTION arb while held (a scale-in)")
    P(f"    (settle proxy +{ADD_DEFAULT_OFFSET_H:.0f}h headline; phantom-lensed; the hold-window sweep + true-add/re-entry split disclose its sensitivity)")
    if not adds:
        P("  (no qualifying same-direction widen beyond tau_gain — no add opportunities)")
    else:
        for c in CATS:
            az = adds_by_cat.get(c, [])
            if not az:
                continue
            inc = sum(a["inc_pnl"] for a in az)
            sz = sum(a["size_add"] for a in az)
            P(f"  {c:8} n={len(az):<3}  add size {sz:>6.0f} contracts (cap {add_cap_frac*100:.0f}%/pair)   "
              f"incremental ${inc:,.2f} PAPER-GROSS")
        inc = sum(a["inc_pnl"] for a in adds); sz = sum(a["size_add"] for a in adds)
        P(f"  {'ALL':8} n={len(adds):<3}  add size {sz:>6.0f}   incremental ${inc:,.2f} PAPER-GROSS over single-entry")

        # ---- (3a) HOLD-WINDOW SWEEP — an add only counts if the bigger arb appears BEFORE the first pair
        #      settles, so a longer assumed hold inflates the count. Sweep the settlement proxy; quote a
        #      RANGE, never the single 28h point (audit WARN-1). ----
        P("")
        P("  (3a) HOLD-WINDOW SWEEP  — add count + PnL per assumed hold (the FIRST-position settlement proxy).")
        P("       The TRUE-ADD column is the genuine scale-in count (base edge still open); RE-ENTRY is the rest.")
        P(f"       {'hold':>6}  {'adds':>5}  {'PnL$':>9}  {'trueadd':>8}  {'reentry':>8}   "
          + "  ".join(f"{c[:4]:>8}" for c in CATS if c != "other"))
        for off in ADD_SWEEP_OFFSETS_H:
            sw, _ = add_events(records, episodes, edge_min, window_min, max_clip, add_cap_frac, tau_gain, settle_offset_h=off)
            n = len(sw); pnl = sum(a["inc_pnl"] for a in sw)
            n_true = sum(1 for a in sw if a["is_true_add"])
            cat_pnl = {c: sum(a["inc_pnl"] for a in sw if a["cat"] == c) for c in CATS}
            tag = "  <- headline" if abs(off - ADD_DEFAULT_OFFSET_H) < 1e-9 else ""
            P(f"       {off:>4.0f}h  {n:>5}  {pnl:>9,.2f}  {n_true:>8}  {n-n_true:>8}   "
              + "  ".join(f"{cat_pnl[c]:>8,.2f}" for c in CATS if c != "other") + tag)
        P("       NOTE: TRUE-ADD is ~INVARIANT to the hold (a real scale-in needs the base edge still OPEN, which")
        P("       the settlement proxy doesn't touch); the whole hold-window LEVER moves only the RE-ENTRY count.")
        P("       REALISTIC per-category settle (which sweep row each maps to):")
        P("         weather ~1.2d after the daily high locks (~6PM ET) -> between the 24h and 48h rows")
        P("         sports  at game-end (hours after open)             -> the 4h-12h rows")
        P("         econ    at the release print (the far endDate)     -> the 48h row (or beyond)")
        P("       -> the add PnL is hold-conditional; quote the RANGE across plausible holds, not one value.")

        # ---- (3b) PHANTOM LENS on the add path — the [L20] flat-ladder drop applied to the WIDEN leg
        #      (the base cohort gets it via capturable(); the WIDEN record bypasses that chokepoint). ----
        P("")
        P("  (3b) PHANTOM LENS  — [L20] flat-ladder (c2==c1==c0) drop on the add's driving WIDEN, per the audit")
        lensed, dropped = add_events(records, episodes, edge_min, window_min, max_clip, add_cap_frac, tau_gain,
                                     drop_flat_widen=True)
        pnl_before = sum(a["inc_pnl"] for a in adds)
        pnl_after = sum(a["inc_pnl"] for a in lensed)
        n_flat = sum(1 for a in adds if a["flat_widen"])           # adds whose chosen widen IS a flat phantom
        pnl_flat = sum(a["inc_pnl"] for a in adds if a["flat_widen"])
        P(f"       BEFORE lens : n={len(adds):<3}  ${pnl_before:,.2f}")
        P(f"       AFTER  lens : n={len(lensed):<3}  ${pnl_after:,.2f}   "
          f"(dropped {len(adds)-len(lensed)} adds = ${pnl_before-pnl_after:,.2f}; "
          f"{100*(pnl_before-pnl_after)/pnl_before if pnl_before else 0:.1f}% of the headline rode on flat-ladder widens)")
        P(f"       proof the lens fired: {n_flat} of {len(adds)} counted adds had a FLAT-LADDER driving widen "
          f"(${pnl_flat:,.2f}).")
        for c in CATS:
            bz = [a for a in adds if a["cat"] == c]
            az = [a for a in lensed if a["cat"] == c]
            if not bz:
                continue
            P(f"         {c:8} before ${sum(a['inc_pnl'] for a in bz):>8,.2f} (n={len(bz)})  ->  "
              f"after ${sum(a['inc_pnl'] for a in az):>8,.2f} (n={len(az)})")
        P("       -> the quotable add figure is the LENSED one (same phantom discipline the base cohort gets).")

        # ---- (3c) TRUE-ADD vs RE-ENTRY split (the decision-useful one) — classified against the REAL
        #      episode OPEN/CLOSE, not a proxy (audit). TRUE scale-in piles onto a still-OPEN edge; re-entry
        #      is a separate later arb on a still-held bucket (blocked by the one-position-per-slug guard,
        #      bot-rs main.rs:497, and it concentrates correlated settlement risk). ----
        P("")
        P("  (3c) TRUE-ADD vs RE-ENTRY  — base edge episode still OPEN at the widen (TRUE scale-in) vs already")
        P("       CLOSED (RE-ENTRY: a separate later arb on a still-held bucket; the per-slug guard blocks it).")
        true_a = [a for a in adds if a["is_true_add"]]
        reentry = [a for a in adds if not a["is_true_add"]]
        P(f"       {'class':<10} {'n':>4} {'PnL$':>9}   " + "  ".join(f"{c[:4]:>8}" for c in CATS if c != "other"))
        for label, grp in (("TRUE-ADD", true_a), ("RE-ENTRY", reentry)):
            cat_pnl = {c: sum(a["inc_pnl"] for a in grp if a["cat"] == c) for c in CATS}
            P(f"       {label:<10} {len(grp):>4} {sum(a['inc_pnl'] for a in grp):>9,.2f}   "
              + "  ".join(f"{cat_pnl[c]:>8,.2f}" for c in CATS if c != "other"))
        P(f"       -> only {len(true_a)}/{len(adds)} counted adds are GENUINE scale-in (the owner's feature); "
          f"{len(reentry)}/{len(adds)} are re-entry that the live one-position-per-slug guard already blocks.")
        # the cleanest single number for the FEATURE: a genuine scale-in that ALSO survives the [L20] phantom
        # lens (its driving widen is not a flat-ladder book-init artifact). Both disciplines applied at once.
        true_clean = [a for a in true_a if not a["flat_widen"]]
        P(f"       BOTH lenses (genuine scale-in AND a non-phantom driving widen): {len(true_clean)}/{len(adds)} adds, "
          f"${sum(a['inc_pnl'] for a in true_clean):,.2f} paper-gross — the honest size of the actual feature "
          f"({len(true_a)-len(true_clean)} of the {len(true_a)} scale-ins ride a flat-ladder phantom widen).")

        # correlated-settlement concentration: how many markets share one settlement bucket-date-cat cluster
        clusters = {}
        for a in adds:
            clusters.setdefault((a["cat"]), 0)
            clusters[(a["cat"])] += 1
        P("")
        P(f"  CONCENTRATION: the add stacks a 2nd pair on the SAME bucket -> N pairs lose together if that "
          f"bucket's grading diverges.")
        P(f"    add markets per category: " + "  ".join(f"{c}={n}" for c, n in sorted(clusters.items())))

    # ---- caveats ----
    P("")
    P("CAVEATS (do not read past these):")
    P("  • PAPER/GROSS. FLIP is 4 fills (close 2 + reverse 2) vs 0 for holding -> 4x the ~17-55% naked-leg")
    P("    risk, and the flip IS the fat-fast ~66%-toxic regime (a real cross-over moves fast). None modelled.")
    P("  • SPORTS close mark is UNPRICEABLE from the logged fields: the transition `px` carries only the two")
    P("    Kalshi team YES *asks* (ka/kb), never the Kalshi YES *bid* needed to sell a Kalshi-backed leg. So a")
    P("    sports FLIP's close P&L is omitted (NOT fabricated) — only the reverse-arb leg is quantified there.")
    P("  • the FLIP reverse-leg benefit is a SELECTION-BIASED LOWER bound: net1 is a max-order-statistic over a")
    P("    noisy basis (we only observe crossings that GREW past tau_gain), so even the 'reverse leg only' figure")
    P("    is optimistic — the same best-order-statistic family as the project's seed-city lesson ([L19]).")
    P("  • WEATHER + ECON NEVER FLIP in this data (a 1:1 bucket's basis doesn't cross) -> the FLIP idea has no")
    P("    population outside sports. Of the sports flips, the ORIGINAL legs are sub-cent (it's the entry that's")
    P("    tiny); the REVERSE arbs that follow are larger (mostly >2c) but their close-mark is unpriceable.")
    P("  • the ADD figure is a HOLD-CONDITIONAL RANGE, not a point: it moves ~$99(lensed)..$135 across the hold")
    P("    sweep (3a) and the phantom-lens (3b), and only ~1/4 of counted adds are genuine scale-in (3c). Quote")
    P("    the LENSED figure as a range with the hold + true-add caveats; never the bare 28h number.")
    P(f"  • effective-n is TINY per category (~5 independent event-dates carry essentially all the add PnL).")
    P(f"    tau_gain={tau_gain*100:.1f}c is a fixed trigger, NOT oracle-tuned on these results. At this span this is a")
    P("    METHOD DEMO; a 'flips/adds are too rare or too hold-sensitive to matter' read is a valid outcome ([L19]).")
    P("=" * 92)
    return "\n".join(out)


# ============================================================================================
# SELF-TEST — synthetic transitions proving flip detection + close-mark + add logic, INCLUDING a
# same-direction widening that must NOT count as a flip.
# ============================================================================================
def _selftest():
    print("flip-add backtest self-test")

    # close_pnl_weather: hand-built flip. Entry dir P at known px, then a flip px; the close must equal the
    # Ledger's enter+unwind cash exactly (reuses the project's accounting core, so fees/signs are pinned).
    entry = {"p_yb": 0.59, "p_ya": 0.61, "k_yb": 0.67, "k_ya": 0.70}     # dir P: YES@P .61 + NO@K (1-.67)=.33 -> cost .94
    flip = {"p_yb": 0.70, "p_ya": 0.72, "k_yb": 0.60, "k_ya": 0.62}      # basis flipped: P dear, K cheap
    cp = close_pnl_weather(entry, flip, "P")
    # cross-check against a fresh Ledger doing the same two operations
    L = Ledger("x"); L.enter(entry, 1, direction="P"); L.unwind_all(flip)
    assert cp is not None and abs(cp - L.cash) < 1e-12, (cp, L.cash)
    # economic sanity: entry cost ~.94; after the favourable flip, selling YES@P at .70 + NO@K at (1-.62)=.38
    # = 1.08 gross proceeds -> a positive close P&L net of fees. NOTE the close P&L (~+7.8c here) is GROSS-of-
    # the-n=1-ceil-fee: at unit size each Kalshi leg pays a full ~2c ceil ([L10]) -> 4 ceil'd legs eat ~6c, so
    # the +14c raw basis overshoot nets ~+7.8c. (At real clip size the per-contract ceil shrinks toward the
    # marginal fee — the unit-size number here is the CONSERVATIVE floor, not a bug.)
    assert 0.07 < cp < 0.09, ("favourable flip banks the basis overshoot net of n=1 ceil fees", cp)
    # a flip that leaves the position unsellable (missing the touch we'd sell into) -> None, never fabricated
    assert close_pnl_weather(entry, {"p_yb": None, "p_ya": 0.72, "k_yb": 0.60, "k_ya": 0.62}, "P") is None

    # reverse_net_edge: weather signal + sports game_edge both route correctly by px shape
    rdir, rnet = reverse_net_edge(flip)
    assert rdir == "K" and rnet > 0, (rdir, rnet)         # after the flip, K is the cheap-YES side
    g = {"pm_b": 0.48, "pm_a": 0.50, "ka": 0.55, "kb": 0.45}   # game px -> PK back A@P .50 + B@K .45 = .95
    gdir, gnet = reverse_net_edge(g)
    assert gdir == "PK" and gnet > 0, (gdir, gnet)
    assert px_is_game(g) and not px_is_game(flip)

    # close_pnl_game is intentionally None (Kalshi YES bid not in the logged px) — assert we DON'T fabricate
    assert close_pnl_game(g, g, "PK") is None

    # --- flip_events on hand-built records: a weather market OPENs (dir P), then FLIPs to a BIGGER reverse;
    #     a SECOND market only WIDENS same-direction and must NOT be counted as a flip. ---
    M = "tc-temp-laxhigh-2026-06-10-gte73"      # flips
    W = "tc-temp-nychigh-2026-06-10-gte80"      # only widens (same dir) -> NOT a flip
    def tr(t, m, lab, d, net, px, c2=50):
        return {"t": t, "market": m, "transition": lab, "dir": d, "net_edge": net,
                "depth": {"c2": c2, "c1": c2, "c0": c2}, "px": px}
    recs = [
        tr(0, M, "OPEN", "P", 0.02, entry),                     # filled arb, dir P, net 2c
        tr(5, M, "FLIP", "K", 0.06, flip),                      # flips to dir K, reverse net ~6c (>2c+gain)
        tr(6, M, "CLOSE", "K", -0.01, flip),
        tr(0, W, "OPEN", "P", 0.02, entry),                     # filled arb, dir P
        tr(4, W, "WIDEN", "P", 0.08, entry),                    # SAME direction widen -> an ADD, NOT a flip
        tr(6, W, "CLOSE", "P", -0.01, entry),
    ]
    eps = build_episodes(recs, [], close_lag=0)
    fe = flip_events(recs, eps, edge_min=0.0, window_min=0, tau_gain=0.01)
    assert len(fe) == 1 and fe[0]["market"] == M, [f["market"] for f in fe]   # ONLY M flips; W excluded
    assert fe[0]["dir0"] == "P" and fe[0]["dir1"] == "K" and fe[0]["has_close"]
    assert abs(fe[0]["close_pnl"] - cp) < 1e-12                 # same entry/flip px -> same close as the direct call
    assert 0.07 < fe[0]["close_pnl"] < 0.09                     # favourable flip close banked (net of n=1 ceil fees)

    # a flip whose reverse arb is NOT bigger than the original by tau_gain must be excluded
    recs_small = [tr(0, M, "OPEN", "P", 0.02, entry), tr(5, M, "FLIP", "K", 0.025, flip),
                  tr(6, M, "CLOSE", "K", -0.01, flip)]
    eps_s = build_episodes(recs_small, [], close_lag=0)
    # reverse_net_edge recomputes from px (the flip px implies a ~6c reverse), so to truly test the gate we
    # gate on the RECOMPUTED reverse vs net0: with a high tau_gain even a real reverse is rejected.
    assert flip_events(recs_small, eps_s, 0.0, 0, tau_gain=0.20) == []         # 20c gain unreachable -> excluded

    # a logged FLIP whose RECOMPUTED reverse direction equals the ENTRY direction must NOT count as a flip
    # (it's a same-direction bigger arb = the ADD path). Over a multi-flip held position the book can swing
    # back to the entry dir; the rdir!=dir0 guard excludes it. Entry dir P; flip px also reads dir P (cheaper).
    same_dir_px = {"p_yb": 0.54, "p_ya": 0.56, "k_yb": 0.70, "k_ya": 0.72}     # signal() -> dir P, bigger net
    assert reverse_net_edge(same_dir_px)[0] == "P"
    recs_sd = [tr(0, M, "OPEN", "P", 0.02, entry), tr(5, M, "FLIP", "P", 0.10, same_dir_px),
               tr(6, M, "CLOSE", "P", -0.01, same_dir_px)]
    eps_sd = build_episodes(recs_sd, [], close_lag=0)
    assert flip_events(recs_sd, eps_sd, 0.0, 0, tau_gain=0.01) == []           # same-dir reverse -> NOT a flip

    # --- add_events: W's same-direction widen to 8c IS an add; M (which flipped) has no same-dir widen ---
    ae, n_dropped = add_events(recs, eps, edge_min=0.0, window_min=0, max_clip=1000, add_cap_frac=0.20, tau_gain=0.01)
    assert len(ae) == 1 and ae[0]["market"] == W, [a["market"] for a in ae]
    assert ae[0]["dir"] == "P" and ae[0]["net_add"] == 0.08
    assert ae[0]["size_add"] == round(0.20 * min(50, 1000)) == 10              # 20% of size0=50 -> 10
    assert ae[0]["inc_pnl"] > 0 and n_dropped == 0

    # a widen in the OPPOSITE direction must NOT be an add (it's the flip path)
    recs_opp = [tr(0, W, "OPEN", "P", 0.02, entry), tr(4, W, "WIDEN", "K", 0.08, flip),
                tr(6, W, "CLOSE", "P", -0.01, entry)]
    eps_o = build_episodes(recs_opp, [], close_lag=0)
    assert add_events(recs_opp, eps_o, 0.0, 0, 1000, 0.20, 0.01)[0] == []      # opposite-dir widen excluded

    # --- TRUE-ADD vs RE-ENTRY classification against the REAL episode interval (the decision-useful split) ---
    # W's widen at t=4 fires WHILE W's base episode (OPEN t0 .. CLOSE t6) is OPEN -> TRUE scale-in.
    assert ae[0]["is_true_add"] is True, ae[0]                                 # base episode still open at the widen
    # RE-ENTRY: the same-direction bigger arb appears AFTER the base edge episode CLOSED but the bucket is
    # still HELD to settlement. Base episode OPEN t0 .. CLOSE t5; a NEW edge episode (re-OPEN t100) carries a
    # bigger same-dir arb at t101 — counted as an add only under hold-to-settlement, but it is NOT a scale-in.
    RM = "tc-temp-laxhigh-2026-06-10-gte73"      # date 06-10 -> settle proxy +28h >> t101, so still "held"
    bigger = {"p_yb": 0.54, "p_ya": 0.56, "k_yb": 0.70, "k_ya": 0.72}         # dir P, net ~8c (bigger than 2c)
    recs_re = [tr(0, RM, "OPEN", "P", 0.02, entry), tr(5, RM, "CLOSE", "P", -0.01, entry),   # base edge closes at t5
               tr(100, RM, "OPEN", "P", 0.02, entry), tr(101, RM, "WIDEN", "P", 0.08, bigger),  # later separate arb
               tr(105, RM, "CLOSE", "P", -0.01, entry)]
    eps_re = build_episodes(recs_re, [], close_lag=0)
    re_add, _ = add_events(recs_re, eps_re, 0.0, 0, 1000, 0.20, 0.01)
    assert len(re_add) == 1 and re_add[0]["market"] == RM, re_add              # the widen IS counted as an add
    assert re_add[0]["is_true_add"] is False, re_add[0]                        # but it's RE-ENTRY (base episode closed at t5 < t101)

    # --- PHANTOM LENS: a FLAT-LADDER (c2==c1==c0) driving widen is dropped when drop_flat_widen=True ---
    # tr() writes depth c2==c1==c0 (a flat ladder) -> W's widen IS a flat phantom. Lens off: counted; lens on: dropped.
    assert _widen_is_flat({"c2": 50, "c1": 50, "c0": 50}) and not _widen_is_flat({"c2": 50, "c1": 40, "c0": 30})
    assert not _widen_is_flat(None) and not _widen_is_flat({"c2": 0, "c1": 0, "c0": 0})   # None / zero-depth not flat
    assert ae[0]["flat_widen"] is True, ae[0]                                  # the synthetic widen is flat-laddered
    lensed, dropped = add_events(recs, eps, 0.0, 0, 1000, 0.20, 0.01, drop_flat_widen=True)
    assert lensed == [] and dropped == 1, (lensed, dropped)                    # the only add was flat -> dropped, none remain
    # a NON-flat driving widen survives the lens (depth ladder not all-equal)
    recs_nf = [tr(0, W, "OPEN", "P", 0.02, entry, c2=50),
               {"t": 4, "market": W, "transition": "WIDEN", "dir": "P", "net_edge": 0.08,
                "depth": {"c2": 80, "c1": 60, "c0": 40}, "px": entry},                      # sloped ladder = real book
               tr(6, W, "CLOSE", "P", -0.01, entry)]
    eps_nf = build_episodes(recs_nf, [], close_lag=0)
    nf_add, nf_dropped = add_events(recs_nf, eps_nf, 0.0, 0, 1000, 0.20, 0.01, drop_flat_widen=True)
    assert len(nf_add) == 1 and nf_dropped == 0 and nf_add[0]["flat_widen"] is False, (nf_add, nf_dropped)

    # report renders end-to-end on the synthetic set, INCLUDING the three audit disclosures
    txt = report(sorted(recs, key=lambda r: r["t"]), eps, 0.0, 0, 1000, 0.20, 0.01)
    assert "FLIP BASE RATE" in txt and "ADD PnL" in txt and "CAVEATS" in txt
    assert "HOLD-WINDOW SWEEP" in txt and "PHANTOM LENS" in txt and "TRUE-ADD vs RE-ENTRY" in txt
    print("  OK — close-mark == Ledger cash; flip detected & same-dir widen excluded; add detected & "
          "opposite-dir widen excluded; tau_gain gate; sports close = None (not fabricated)")
    print("  OK — TRUE-ADD vs RE-ENTRY classified on the real episode interval; flat-ladder phantom lens drops "
          "a flat-driving widen; all three disclosures render")
    print("self-test passed.")


# ============================================================================================
if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="cross-arb FLIP/ADD feasibility backtest (read-only)")
    ap.add_argument("--selftest", action="store_true", help="offline synthetic verification")
    ap.add_argument("--data-dir", default=os.path.join(os.path.dirname(__file__), "..", "..", "data", "cross-arb"))
    ap.add_argument("--edge-min", type=float, default=0.0, help="min net edge fraction for a filled arb (default 0 = every +arb)")
    ap.add_argument("--window-min", type=float, default=0, help="min episode duration s for a filled arb (default 0 = any)")
    ap.add_argument("--max-clip", type=int, default=1000, help="max contracts per pair (depth-capped below this)")
    ap.add_argument("--add-cap-frac", type=float, default=0.20, help="per-pair add size cap as a fraction of the base clip")
    ap.add_argument("--tau-gain", type=float, default=0.01, help="min net-edge GAIN (fraction) for a reverse/widen to trigger flip/add")
    a = ap.parse_args()
    if a.selftest:
        _selftest(); sys.exit(0)
    import glob
    data_dir = os.path.abspath(a.data_dir)
    if not glob.glob(os.path.join(data_dir, "transitions-*.jsonl*")):
        print(f"no data at {data_dir} — run `pwsh deploy/pull-data.ps1` first, or `--selftest`."); sys.exit(0)
    recs, sessions = load(data_dir)
    print(f"loaded {len(recs)} transitions + {len(sessions)} censoring events from {data_dir}\n")
    print(report(recs, build_episodes(recs, sessions), a.edge_min, a.window_min, a.max_clip, a.add_cap_frac, a.tau_gain))
