# Plan — 3-leg WC Dutch-book arb (parallel to the per-outcome binaries)

## Model
WC game = 3 outcomes (A/draw/B), each its own YES/NO market on BOTH venues. Dutch book:
buy YES on all 3, each on its cheapest venue. If sum of 3 cheapest YES-asks < $1 (net fees +
void_tail), LOCKED: exactly one outcome pays $1 -> profit = $1 - basket_cost.

## Hard constraint
DO NOT touch the 2-leg path. `submit_pair`/`PairAck`/`Position[2]`/`unwind`/`recover_naked_leg`/
weather/econ/sports/WC-per-outcome stay BYTE-UNCHANGED + all tests green. Build a PARALLEL 3-leg path.

## Key reuse insight
The `SoccerTriple` shares its 3 pmus slugs + 3 Kalshi tickers with the 3 per-outcome `Pair`s the WC
branch ALREADY emits (e50ecbb). Those books are already subscribed + maintained. The triple path just
READS the same books (keyed by slug/ticker) and evaluates the basket — no new subscription wiring.

## Steps
- [x] types.rs: `OutcomeQuote` (pm Book + k Book + tags), `SoccerTriple` (3 pmus slugs + 3 Kalshi
      tickers + per-outcome pm_min_tick/cluster/settle), `TriplePosition` ([PositionLeg;3]), `TripleAck`.
- [x] signal.rs: `dutch_book(&[OutcomeQuote;3])` -> `TripleSignal` (basket_cost = sum of 3 cheapest
      YES asks across venues; net = (1-basket) - 3 marginal fees - void_tail; arb iff net>0; track
      cheapest venue per outcome). Reuse pfee/kfee helpers.
- [x] exec.rs: `submit_triple(a,b,d) -> TripleAck { a,b,d }` trait method, fires 3 CONCURRENTLY
      (tokio::join!), `all_filled()`. Dry-run logs 3. submit_pair/PairAck UNCHANGED.
- [x] risk.rs: `evaluate_triple` — size = min(depth_A,depth_B,depth_D), caps/affordable/concurrency,
      cost_per = basket_cost, settle gate (WC=TAIL settle_clean).
- [x] main.rs: `build_triple` -> [OrderIntent;3] (each outcome's YES on cheapest venue, priced from
      books, tags A/D/B, coids xarb3-<game>-<tag>); routing in live loop; NAKED-PAIR RECOVERY (flatten
      every filled leg on partial fill; fail-close on recovery failure; idempotent recover3- coids).
- [x] unwind.rs: `triple_unwind_orders` (SELL each of 3 held legs).
- [x] smoke: a Dutch-book basket case (basket<$1 -> dry-run fires 3 YES legs).
- [x] tests: dutch_book math (sum<1=arb, sum>1=no, cheapest-venue selection); evaluate_triple min-of-3;
      naked-pair recovery (2-of-3 flattens 2, 1-of-3 flattens 1, recovery failure halts); 2-leg green.

## Self-review focus (money path)
(a) 2-leg path byte-unchanged + tests pass; (b) naked-pair recovery flattens EVERY filled leg + fails
closed (a silent un-flattened 2-of-3 is the worst bug); (c) basket fires only net>0 AND all 3 have
depth; (d) dry-run honored; (e) idempotent coids.
