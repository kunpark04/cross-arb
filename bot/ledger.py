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
def kfee(p, n=1, taker=True, marginal=False):
    if not 0 < p < 1: return 0.0
    if marginal:                                          # at-scale per-contract rate (NO ceil): the right
        return (0.07 * p*(1-p)) * (1.0 if taker else 0.25)  # threshold for DETECTION (is it +EV at any size?)
    cents = math.ceil(0.07 * n * p*(1-p) * 100 - 1e-9)   # whole order up to next cent; eps guards float noise
    return (cents / 100.0) * (1.0 if taker else 0.25)
def pfee(p, n=1, taker=True):
    if not 0 < p < 1: return 0.0
    return (0.05 * n * p*(1-p)) * (1.0 if taker else 0.0)
FEE = {"P": pfee, "K": kfee}

def signal(px):
    """Best cross-venue arb from prices px={p_yb,p_ya,k_yb,k_ya}. Returns dict (with no_arb when none).
    dir 'P' = buy YES@P + NO@K ;  dir 'K' = buy YES@K + NO@P."""
    # reject an internally-crossed/locked venue book (yes_bid >= yes_ask) — almost always stale or
    # in-play data, and it manufactures a phantom 'edge'. Treat as no-arb (C3).
    if px["p_yb"] > px["p_ya"] or px["k_yb"] > px["k_ya"]:        # strictly crossed (locked bid==ask is ok)
        return {"dir": "P", "yes_ask": px["p_ya"], "no_ask": round(1 - px["k_yb"], 4),
                "net_edge": 0.0, "no_arb": True, "crossed": True}
    # DETECTION uses the at-scale MARGINAL Kalshi fee (no ceil): the question is "is this +EV at SOME size?"
    # — using the n=1 ceil fee over-charges ~0.25-0.9c and would drop a real arb that's only +EV at size.
    # The exact per-order ceil fee is applied later at booking (Ledger.enter), where the size is known.
    opts = []
    ay, an = px["p_ya"], 1-px["k_yb"]                     # YES@P + NO@K
    net = (1-(ay+an)) - pfee(ay) - kfee(an, marginal=True)
    opts.append(("P", ay, an, round(net, 4)))
    ay, an = px["k_ya"], 1-px["p_yb"]                     # YES@K + NO@P
    net = (1-(ay+an)) - kfee(ay, marginal=True) - pfee(an)
    opts.append(("K", ay, an, round(net, 4)))
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
            no_bid = 1 - yask
            yq, nq = self.pos[v]["YES"], self.pos[v]["NO"]
            total += yq*ybid - FEE[v](ybid, yq)
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
            no_bid = 1 - yask
            yq, nq = self.pos[v]["YES"], self.pos[v]["NO"]
            proceeds = yq*ybid - FEE[v](ybid, yq) + nq*no_bid - FEE[v](no_bid, nq)
            realized += proceeds
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
    EPS = 0.011  # 1 fee-cent tolerance for the ceil() in Kalshi fees
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
    print(f"  WRONG move (panic-unwind the first @2pm): realized {loss-0.94*100*0+bad.cash:+.2f}  -> book cash {bad.cash:+.2f} (a real loss)")
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
    flat = {"p_yb":0.59,"p_ya":0.61,"k_yb":0.59,"k_ya":0.61}        # venues agree -> no positive edge
    try:
        Ledger("g").enter(flat, 100); raise AssertionError("enter should have refused a no-edge book")
    except ValueError:
        print("  C2: enter() refuses a non-positive-edge book (ValueError); force=True overrides")
    crossed = {"p_yb":0.70,"p_ya":0.60,"k_yb":0.30,"k_ya":0.32}     # P internally crossed (bid > ask)
    assert signal(crossed).get("crossed") and signal(crossed).get("no_arb")
    print("  C3: signal() rejects an internally-crossed venue book as no-arb (was a phantom +edge)")

    print("\nAll invariants asserted OK (additive PnL + outcome-independence for locked books).")

if __name__ == "__main__":
    run()
