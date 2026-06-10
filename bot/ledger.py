"""Cross-venue position ledger + PnL simulator for ONE binary market with IDENTICAL settlement on
both venues (P = polymarket.us, K = Kalshi). This is the bot's accounting core.

Model
-----
A market has two complementary outcomes YES / NO; exactly one pays $1 at settlement, on BOTH venues
(identical settlement is the load-bearing assumption). Prices are quoted as YES bid/ask per venue;
NO ask = 1 - YES bid, NO bid = 1 - YES ask.

A cross-venue arb ENTRY buys YES on one venue + NO on the other (a complementary pair). If the pair
costs < $1, the difference is locked. Holdings are tracked per venue as separate YES / NO quantities
(true for polymarket.us tokens; Kalshi nets, but the hold-to-settlement total is identical).

Key invariants this file demonstrates + asserts:
  (1) ADDITIVE: settlement PnL of any set of held arb pairs == sum of each entry's net edge.
  (2) OUTCOME-INDEPENDENT: for a balanced book, settlement PnL is the same whether YES or NO wins.
  (3) A locked arb can be mark-to-market NEGATIVE mid-life yet settle POSITIVE -> never unwind at a
      MTM loss; hold + layer. A genuine FLIP makes early rotation profitable (proceeds > $1 > cost).
"""
import math

# ---- fee models (USD). n = order size in contracts. Kalshi rounds the WHOLE order up to the next cent
#      (summing per-contract ceils over-charges, e.g. 100@0.5 -> $2.00 vs the correct $1.75); polymarket
#      fee is linear (no ceil), so per-order = n x per-contract. n=1 = the conservative per-unit fee that
#      edge DETECTION (signal) uses; pass the real size where the order size is known (ledger booking).
#      PINNED COEFFICIENTS (re-verify against the live fee schedules before any sizing decision):
#        Kalshi taker ceil(0.07·N·P(1−P)), maker ceil(0.0175·N·P(1−P)) — the ceil applies to EACH SIDE'S
#        OWN formula (research/kalshi-venue-audit.md §2.1 [VERIFIED]; pre-fix this file took 0.25× of the
#        ceiled taker fee, a non-cent amount that understates the maker fee). Some series carry a
#        fee_multiplier != 1 (audit §2.1, INFERRED) — not modeled; all tracked series were multiplier 1.
#        pmus taker 0.05·N·P(1−P), maker rebate −0.0125 modeled as 0 (conservative)
#        (research/us-legal-overlap-audit.md, live API pull).
def kfee(p, n=1, taker=True, marginal=False):
    if not 0 < p < 1: return 0.0
    rate = 0.07 if taker else 0.0175                     # maker = 25% of the taker RATE (venue-audit §2.1)
    if marginal:                                         # at-scale per-contract rate (NO ceil): the right
        return rate * p*(1-p)                            # threshold for DETECTION (is it +EV at any size?)
    cents = math.ceil(rate * n * p*(1-p) * 100 - 1e-9)   # whole order up to next cent; eps guards float noise
    return cents / 100.0
def pfee(p, n=1, taker=True):
    if not 0 < p < 1: return 0.0
    return (0.05 * n * p*(1-p)) * (1.0 if taker else 0.0)
FEE = {"P": pfee, "K": kfee}

def signal(px):
    """Best cross-venue arb from px={p_yb,p_ya,k_yb,k_ya} (any quote may be None for a one-sided book).
    Each direction is evaluated on ONLY the two quotes it needs — 'P' = YES@P(p_ya)+NO@K(1-k_yb),
    'K' = YES@K(k_ya)+NO@P(1-p_yb) — so a one-sided book never kills the direction that doesn't use the
    missing quote. A strictly-crossed venue (bid>ask, when both touches present) is stale: every direction
    touching it is skipped. DETECTION uses the at-scale MARGINAL Kalshi fee (no ceil) so nothing +EV-at-size
    is dropped; the exact per-order ceil fee is applied at booking (Ledger.enter)."""
    p_yb, p_ya, k_yb, k_ya = px.get("p_yb"), px.get("p_ya"), px.get("k_yb"), px.get("k_ya")
    p_x = p_yb is not None and p_ya is not None and p_yb > p_ya   # venue crossed (only when both touches present)
    k_x = k_yb is not None and k_ya is not None and k_yb > k_ya
    opts = []
    if p_ya is not None and k_yb is not None and not (p_x or k_x):   # dir P: YES@P + NO@K
        ay, an = p_ya, 1 - k_yb
        opts.append(("P", ay, an, round((1-(ay+an)) - pfee(ay) - kfee(an, marginal=True), 4)))
    if k_ya is not None and p_yb is not None and not (p_x or k_x):   # dir K: YES@K + NO@P
        ay, an = k_ya, 1 - p_yb
        opts.append(("K", ay, an, round((1-(ay+an)) - kfee(ay, marginal=True) - pfee(an), 4)))
    if not opts:                                                    # nothing priceable (one-sided both ways, or crossed)
        return {"dir": "P", "yes_ask": p_ya, "no_ask": (round(1-k_yb, 4) if k_yb is not None else None),
                "net_edge": 0.0, "no_arb": True, "crossed": p_x or k_x}
    best = max(opts, key=lambda o: o[3])
    return {"dir": best[0], "yes_ask": best[1], "no_ask": best[2], "net_edge": best[3]} if best[3] > 0 else \
           {"dir": best[0], "yes_ask": best[1], "no_ask": best[2], "net_edge": best[3], "no_arb": True}


class Ledger:
    def __init__(self, name="market"):
        self.name = name
        self.pos = {"P": {"YES": 0.0, "NO": 0.0}, "K": {"YES": 0.0, "NO": 0.0}}
        self.cash = 0.0              # cumulative signed cash flow (start 0)
        self.cost_basis = 0.0        # total $ paid in (capital deployed, gross of later unwinds)
        self.entries = []

    # buy `size` of an arb pair. dir 'P' = YES@P + NO@K ; 'K' = YES@K + NO@P
    def enter(self, px, size, direction=None, t=None, force=False):
        s = signal(px)
        d = direction or s["dir"]
        if (s.get("crossed") or s.get("no_arb")) and not force:   # C3: honor the crossed/stale/no-arb rejection
            raise ValueError(f"refusing entry on crossed/stale/no-arb book "    # (signal saw no priceable +edge)
                             f"(crossed={s.get('crossed')}, no_arb={s.get('no_arb')}); pass force=True to override")
        need = ("p_ya", "k_yb") if d == "P" else ("k_ya", "p_yb")  # the two touches this direction must price
        miss = [k for k in need if px.get(k) is None]
        if miss and not force:                                    # unpriceable dir -> clean refusal, not a raw TypeError
            raise ValueError(f"refusing entry: dir {d} unpriceable on this book (missing {miss}); force=True to override")
        if d == "P":
            vy, ay, vn, an = "P", px["p_ya"], "K", 1-px["k_yb"]
        else:
            vy, ay, vn, an = "K", px["k_ya"], "P", 1-px["p_yb"]
        feeY, feeN = FEE[vy](ay, size), FEE[vn](an, size)      # per-ORDER fees (the size is known here)
        cost = ay + an
        net_edge = (1-cost)*size - feeY - feeN
        if net_edge <= 0 and not force:                        # C2: never silently book a guaranteed loss
            raise ValueError(f"refusing non-positive-edge entry (dir {d}, net {net_edge:+.3f}); "
                             f"pass force=True to book it anyway")
        self.pos[vy]["YES"] += size
        self.pos[vn]["NO"] += size
        out = cost*size + feeY + feeN
        self.cash -= out
        self.cost_basis += out
        e = {"t": t, "dir": d, "size": size, "cost": round(cost, 3),
             "yes_leg": f"{vy}@{ay:.2f}", "no_leg": f"{vn}@{an:.2f}", "net_edge": round(net_edge, 3)}
        self.entries.append(e)
        return e

    def mtm(self, px):
        """Total PnL if we liquidated the whole book NOW (sell every leg at its bid, pay taker fees)."""
        total = self.cash
        for v in ("P", "K"):
            ybid = px["p_yb"] if v == "P" else px["k_yb"]
            yask = px["p_ya"] if v == "P" else px["k_ya"]
            yq, nq = self.pos[v]["YES"], self.pos[v]["NO"]
            if yq and ybid is not None:                    # un-quoted leg = unsellable now (0 proceeds), never a crash
                total += yq*ybid - FEE[v](ybid, yq)
            if nq and yask is not None:                    # NO bid = 1 - YES ask; absent if the venue is one-sided
                no_bid = 1 - yask
                total += nq*no_bid - FEE[v](no_bid, nq)
        return round(total, 3)

    def settlement_pnl(self, outcome):
        """Realized PnL if held to settlement and `outcome` (YES/NO) wins. Pure (no mutation)."""
        payout = sum(self.pos[v]["YES"] for v in ("P", "K")) if outcome == "YES" \
            else sum(self.pos[v]["NO"] for v in ("P", "K"))
        return round(self.cash + payout, 3)

    def unwind_all(self, px):
        """Rotation: sell the entire book at current bids (pay fees), realize cash, go flat."""
        realized = 0.0
        for v in ("P", "K"):
            ybid = px["p_yb"] if v == "P" else px["k_yb"]
            yask = px["p_ya"] if v == "P" else px["k_ya"]
            yq, nq = self.pos[v]["YES"], self.pos[v]["NO"]
            if yq and ybid is not None:                    # one-sided book -> that leg can't be sold (0 proceeds)
                realized += yq*ybid - FEE[v](ybid, yq)
            if nq and yask is not None:
                realized += nq*(1 - yask) - FEE[v](1 - yask, nq)
            self.pos[v]["YES"] = 0.0; self.pos[v]["NO"] = 0.0
        self.cash += realized
        return round(realized, 3)

    def redeem_poly_sets(self):
        """polymarket.us only: merge complete YES+NO sets back to $1 each, freeing capital early."""
        m = min(self.pos["P"]["YES"], self.pos["P"]["NO"])
        self.pos["P"]["YES"] -= m; self.pos["P"]["NO"] -= m
        self.cash += m
        return m

    @property
    def sum_net_edges(self):
        return round(sum(e["net_edge"] for e in self.entries), 3)

    def book(self):
        return (f"P[YES {self.pos['P']['YES']:.0f} NO {self.pos['P']['NO']:.0f}]  "
                f"K[YES {self.pos['K']['YES']:.0f} NO {self.pos['K']['NO']:.0f}]  "
                f"cash {self.cash:+.2f}  capital_deployed {self.cost_basis:.2f}")


# =====================================================================================
# Scenario harness — runs the exact cases from the discussion and self-verifies invariants.
# =====================================================================================
def _show(L, px=None):
    line = "    book: " + L.book()
    if px: line += f"  | MTM-now {L.mtm(px):+.2f}"
    line += f"  | settle YES {L.settlement_pnl('YES'):+.2f} / NO {L.settlement_pnl('NO'):+.2f}"
    print(line)

def run():
    EPS = 0.011  # rounding tolerance: each entry stores net_edge round()ed to 3dp (the ceil cancels exactly
                 # on both sides of the additive identity, so this guards 3dp rounding, NOT fee-ceil drift)
    print("="*92)
    print("S1 — SAME direction re-entry (edge reappears the same way): purely additive")
    print("="*92)
    L = Ledger("S1")
    t1 = {"p_yb":0.59,"p_ya":0.61,"k_yb":0.67,"k_ya":0.70}
    t2 = {"p_yb":0.60,"p_ya":0.62,"k_yb":0.68,"k_ya":0.71}
    print("  9am:", L.enter(t1, 100, t="9am")); _show(L, t1)
    print("  2pm:", L.enter(t2, 100, t="2pm")); _show(L, t2)
    assert abs(L.settlement_pnl("YES")-L.settlement_pnl("NO"))<EPS, "outcome-invariance"
    assert abs(L.settlement_pnl("YES")-L.sum_net_edges)<EPS, "additive identity"
    print(f"  -> hold both to settlement = sum of edges = {L.sum_net_edges:+.2f}; same whichever outcome wins.")

    print("\n"+"="*92)
    print("S2 — FLIPPED re-entry, LAYER (hold both): still additive; book becomes riskless sets")
    print("="*92)
    L = Ledger("S2")
    t1 = {"p_yb":0.59,"p_ya":0.61,"k_yb":0.67,"k_ya":0.70}     # Dir P (YES@P)
    t2 = {"p_yb":0.70,"p_ya":0.72,"k_yb":0.60,"k_ya":0.62}     # flipped -> Dir K (YES@K)
    print("  9am:", L.enter(t1, 100, t="9am")); _show(L, t1)
    print("  2pm:", L.enter(t2, 100, t="2pm")); _show(L, t2)
    assert abs(L.settlement_pnl("YES")-L.settlement_pnl("NO"))<EPS
    assert abs(L.settlement_pnl("YES")-L.sum_net_edges)<EPS
    print(f"  -> P holds YES+NO and K holds YES+NO = complete sets. PnL = {L.sum_net_edges:+.2f} = edge1+edge2, no cash-out needed.")

    print("\n"+"="*92)
    print("S3 — FLIPPED re-entry, ROTATE the first instead (recycle capital): beats holding when flip is real")
    print("="*92)
    Lhold = Ledger("S3-hold"); Lhold.enter(t1,100,t="9am"); Lhold.enter(t2,100,t="2pm")
    Lrot = Ledger("S3-rotate"); Lrot.enter(t1,100,t="9am")
    realized = Lrot.unwind_all(t2)                 # sell the first position into the reversal
    print(f"  unwind first @2pm prices -> realized cash {realized:+.2f} (paid 0.94/contract; reversal pushed both legs in our favor)")
    Lrot.enter(t2,100,t="2pm")                      # then enter the new direction fresh
    print(f"  HOLD-both total  : {Lhold.settlement_pnl('YES'):+.2f}   capital_deployed {Lhold.cost_basis:.2f}")
    print(f"  ROTATE+re-enter  : {Lrot.settlement_pnl('YES'):+.2f}   capital_deployed {Lrot.cost_basis:.2f} (but first tranche's capital was freed at 2pm)")
    print("  -> rotation realizes the first tranche EARLY and frees its capital; total PnL >= hold when the flip exceeds round-trip cost.")

    print("\n"+"="*92)
    print("S4 — SAME direction re-entry, but the FIRST is mark-to-market NEGATIVE at 2pm")
    print("="*92)
    L = Ledger("S4")
    t1 = {"p_yb":0.59,"p_ya":0.61,"k_yb":0.67,"k_ya":0.70}     # Dir P, paid 0.94
    t2 = {"p_yb":0.40,"p_ya":0.42,"k_yb":0.48,"k_ya":0.51}     # YES less likely; still Dir P arb
    L.enter(t1,100,t="9am")
    print(f"  first tranche MTM @2pm: {L.mtm(t2):+.2f}  (UNDERWATER — selling now would LOCK a loss)")
    print(f"  but held-to-settlement value of first tranche is still +{L.entries[0]['net_edge']:.2f}")
    L.enter(t2,100,t="2pm")
    bad = Ledger("S4-mistake"); bad.enter(t1,100); loss = bad.unwind_all(t2)
    print(f"  WRONG move (panic-unwind the first @2pm): sale proceeds {loss:+.2f}  -> book cash {bad.cash:+.2f} (a real loss)")
    print(f"  RIGHT move (hold first + layer second): settle = {L.settlement_pnl('YES'):+.2f} = +edge1 +edge2")
    assert abs(L.settlement_pnl("YES")-L.sum_net_edges)<EPS
    print("  -> a MTM-negative locked arb is a paper number; hold to settlement (guaranteed +) and layer the new one.")

    print("\n"+"="*92)
    print("S5 — CAVEAT: leg risk. Only ONE leg fills -> directional, NOT a locked arb -> can settle negative")
    print("="*92)
    L = Ledger("S5")
    px = {"p_yb":0.59,"p_ya":0.61,"k_yb":0.67,"k_ya":0.70}
    # simulate filling only the YES@P leg (hedge leg missed)
    L.pos["P"]["YES"] += 100; out = px["p_ya"]*100 + pfee(px["p_ya"], 100); L.cash -= out; L.cost_basis += out
    print(f"  filled only YES@P (no hedge). book: {L.book()}")
    print(f"  settle if YES wins: {L.settlement_pnl('YES'):+.2f}   settle if NO wins: {L.settlement_pnl('NO'):+.2f}")
    print("  -> outcome-DEPENDENT now (can lose ~$61 if NO wins). This is where 'negative' is real money,")
    print("     and a later opportunity is used to REPAIR/exit the naked leg, not to stack.")

    print("\n"+"="*92)
    print("S6 — review fixes: per-ORDER fee (C1), entry guard (C2), crossed-book rejection (C3)")
    print("="*92)
    assert abs(kfee(0.5, 100) - 1.75) < 1e-9 and abs(100*kfee(0.5, 1) - 2.00) < 1e-9
    print(f"  C1: kfee(.5, n=100) = ${kfee(0.5,100):.2f}/order   vs   100x per-contract = ${100*kfee(0.5,1):.2f} (old over-charge)")
    assert abs(kfee(0.5, marginal=True) - 0.0175) < 1e-9   # DETECTION = at-scale rate (no ceil) -> no over-filter
    print(f"  C1b: detection marginal fee = {kfee(0.5,marginal=True)*100:.2f}c  (vs n=1 ceil {kfee(0.5,1)*100:.2f}c -> would drop sub-{(kfee(0.5,1)-kfee(0.5,marginal=True))*100:.2f}c arbs)")
    assert abs(kfee(0.5, 100, taker=False) - 0.44) < 1e-9   # maker = ceil(0.0175·N·P(1−P)) per venue-audit §2.1
    assert abs(kfee(0.5, marginal=True, taker=False) - 0.004375) < 1e-9   # (NOT 0.25x the ceiled taker fee)
    print(f"  C1c: maker fee/order(100@.5) = ${kfee(0.5,100,taker=False):.2f} (ceil of the MAKER formula, a whole cent)")
    flat = {"p_yb":0.59,"p_ya":0.61,"k_yb":0.59,"k_ya":0.61}        # venues agree -> no positive edge
    try:
        Ledger("g").enter(flat, 100); raise AssertionError("enter should have refused a no-edge book")
    except ValueError:
        print("  C2: enter() refuses a non-positive-edge book (ValueError); force=True overrides")
    crossed = {"p_yb":0.70,"p_ya":0.60,"k_yb":0.30,"k_ya":0.32}     # P internally crossed (bid > ask)
    assert signal(crossed).get("crossed") and signal(crossed).get("no_arb")
    print("  C3: signal() rejects an internally-crossed venue book as no-arb (was a phantom +edge)")
    stale = {"p_yb":0.30,"p_ya":0.32,"k_yb":0.70,"k_ya":0.60}       # K crossed w/ STALE-HIGH bid -> 1-k_yb=0.30 looks cheap
    assert signal(stale).get("crossed") and signal(stale).get("no_arb")
    try:
        Ledger("x").enter(stale, 100); raise AssertionError("enter should refuse a crossed/stale book")
    except ValueError:
        print("  C3b: enter() now ALSO refuses the crossed/stale book (was a phantom +edge via 1-stale_bid; force=True overrides)")
    L1 = Ledger("os"); L1.enter({"p_yb":0.59,"p_ya":0.61,"k_yb":0.67,"k_ya":0.70}, 100)  # dir P -> K holds 100 NO
    assert L1.mtm({"p_yb":0.59,"p_ya":0.61,"k_yb":None,"k_ya":0.70}) is not None          # one-sided K book: no crash
    assert L1.unwind_all({"p_yb":0.59,"p_ya":None,"k_yb":None,"k_ya":0.70}) is not None    # missing touches -> unsellable, not TypeError
    print("  WARN-fix: mtm()/unwind_all() treat an un-quoted leg as unsellable (no TypeError on one-sided books)")

    print("\nAll invariants asserted OK (additive PnL + outcome-independence for locked books).")

if __name__ == "__main__":
    run()
