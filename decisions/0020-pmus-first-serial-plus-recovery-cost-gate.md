# 0020 — pmus-first serial execution + recovery-cost gate (naked-leg avoidance)

- **Date:** 2026-06-14
- **Status:** Accepted (owner-directed; from the FIRST live round-trip)
- **Deciders:** owner (live run + directive); Claude (finding + design + build)

## Context

The first live armed arb (owner overrode the auto-mode classifier's settlement-gate block; 1-contract /
max-1-entry / all categories / velocity-gate on) fired on a **settle-clean World Cup outcome**
(`atc-fwc-swe-tun-2026-06-14-swe`, dir KP, edge 4.8¢) and immediately hit a **NAKED LEG** — the exact gating
risk the project has flagged from the start. Forensics from the new execution log (`executions.jsonl`, 4
records) + the latency instrumentation (`2174234`):

- **Kalshi BUY YES @ 86¢ filled** in 220ms; **pmus BUY NO @ 8¢ did NOT fill** (1,533ms; rested
  `cumQuantity:0`, `ORD_REJECT_REASON_EXCHANGE_OPTION`). The W14 recovery cancelled the pmus rest + **SOLD
  the Kalshi YES @ 77¢** to flatten → **net −11.09¢, position flat.** Every safety + forensic layer worked.
- **The pmus 1.5s is a DELIBERATE block, not venue slowness.** The bot sends `synchronousExecution:true` +
  `maxBlockTime` (`exec.rs:421`) so the POST returns a real fill verdict (pmus's default is async — a bare
  `{id}` that fills later on the stream). A non-marketable order blocks the FULL window then returns resting.
  The window was 1s = `(leg_fill_timeout_ms 500 / 1000).max(1)`. A pmus order that DOES fill returns fast;
  1.5s is the no-fill worst case.
- **The −9¢ on the Kalshi leg was the SPREAD, not (only) a move.** We bought the 86¢ ask and flattened into
  the 77¢ bid — a ~9¢-wide spread on a **thin WC-outcome book** (its Kalshi book is empty on review =
  thin/intermittent). Recovery cost (9¢) ≈ **2× the edge** (4.8¢): one failed hedge eats two good arbs. (We
  logged the two fill prices but not the entry-side book — spread-vs-move can't be fully separated; see
  Consequences.)

Root cause of the loss MODE: the concurrent two-leg fire (`tokio::join!`, `exec.rs:524`) leaves the **fast
Kalshi leg naked for the entire slow pmus block** — the Kalshi YES sat exposed the whole ~1.5s, which is when
the spread cost the 9¢.

## Decision

Two changes, one lesson:

1. **pmus-first serial execution** (`exec.rs run_pair`). Fire the **pmus leg first** and await its
   synchronousExecution verdict; fire the Kalshi leg **only if pmus confirms FILLED**. If pmus does not fill,
   **abort — the Kalshi leg is never opened** ⇒ it is structurally impossible to be naked on the fast leg.
   Because the pmus order is **GTC**, the abort path must **cancel the resting pmus order** (a non-filled
   sync order rests and could fill later unhedged) — and must NOT trip the fail-close halt (no leg filled =
   clean abort, not a naked leg). Cost: +~1 block-second of latency and forgone edges that evaporate in that
   window — which were phantom anyway (the hedge wasn't executable).
2. **Recovery-cost gate** (`risk::evaluate`). Under pmus-first the only residual naked case is *pmus filled,
   Kalshi failed* → unwind the pmus leg → cost ≈ the **pmus bid↔ask spread** (`pm.yes_ask − pm.yes_bid`,
   side-invariant). Reject the arb when that spread exceeds the edge (tunable ratio, default 1.0 = spread ≤
   edge). Today's `depth_at_edge` validates only the side we TAKE, never the side we'd sell back into. The
   Kalshi spread is deliberately NOT gated — under pmus-first the Kalshi leg locks and is held to settlement,
   never unwound, so its (often wide) spread is paid once on entry and already in the edge.

## Alternatives considered

- **Keep concurrent fire + rely on recovery only.** Rejected: recovery on a thin book cost ~2× the edge; the
  W14 fail-close is a backstop, not a strategy. Avoiding the naked leg dominates surviving it.
- **Widen `leg_fill_timeout_ms` so pmus blocks/fills longer.** Rejected: it doesn't make a non-marketable
  phantom hedge fillable, and a longer block = a longer naked window on the fast leg under concurrent fire.
- **Gate on a deeper depth floor only.** Partial — depth ≠ spread; a book can be deep on the take side and
  wide to exit. The recovery-cost gate prices the actual unwind, which is what bit us.
- **Gate on `max(kalshi_spread, pmus_spread)`.** Rejected: the Kalshi leg never unwinds under pmus-first, so
  gating its spread would needlessly skip good arbs (e.g. this very WC pair — wide Kalshi book, but pmus-first
  holds Kalshi to settlement).

## Consequences

- **Enables** firing only arbs whose hedge is real (pmus-first proves it before opening the fast leg) and
  whose residual unwind is affordable (recovery-cost gate). Removes the dominant live-loss mode seen on trade
  #1: under pmus-first that trade ABORTS cleanly (pmus never filled → no Kalshi leg → no 9¢).
- **Latency trade-off:** entries become serial (~1 block-second slower) instead of concurrent (~86ms p50).
  Acceptable — fills are network-bound regardless and the forgone fast-evaporating edges are the phantom ones.
- **Creates an invariant:** *the Kalshi (fast) leg is never submitted before the pmus (blocking) leg returns
  FILLED.* The naked-leg recovery path (`bookkeeping.rs recover_naked_leg`) remains the backstop for the now-
  rarer residual (pmus filled, Kalshi then fails → unwind pmus). The abort path adds a second invariant: *a
  resting (unfilled) pmus order is always cancelled before the loop moves on* (GTC orders don't expire).
- **Revisit if** pmus's synchronousExecution semantics change (a fill that doesn't block, or async-only), or
  if a venue other than pmus becomes the slow/blocking leg. **Instrumentation follow-up:** snapshot bid/ask/
  depth at entry AND recovery so a naked leg's cost decomposes into spread-vs-move, not inferred ([L33]).
- Arming stays an owner action under [0006](0006-deploy-on-digitalocean-consult-first.md) /
  [0015](0015-owner-override-live-trading-phase.md); this was built from a real 1-contract live trade.
