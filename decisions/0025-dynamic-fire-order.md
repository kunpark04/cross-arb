# 0025 — dynamic fire-order: fire the THINNER leg first (extends 0020's pmus-first serial)

- **Date:** 2026-06-15
- **Status:** Accepted (owner-directed after three live pushes on execution order; adversarial-reviewed SAFE_TO_ARM)
- **Deciders:** owner (pushed *simultaneous* → redirected to dynamic); Claude (built it directly per [L38], reviewed it, fixed 2 economic WARNs + the live-exposed Kalshi-count bug [L39])

## Context

[0020] hardcoded **pmus-first** serial execution: fire the slow/uncertain pmus leg first, open the fast Kalshi
leg only if pmus filled — so the fast leg is never left naked. But 4 live Kalshi-409 fails (pietai, ×2 laxhigh,
sly-gal) showed Kalshi is **not** always deep: on a thin-Kalshi book, pmus-first commits the pmus leg, then the
Kalshi hedge 409s ("insufficient resting volume") → a naked pmus leg + an auto-flatten spread loss. The owner
pushed for **simultaneous** fire (×3); analysis refuted it (it would convert the COMMON pmus-thin clean abort
into a naked-Kalshi recovery loss). The real fix is to fire whichever leg is **thinner** first.

## Decision

Replace the hardcoded pmus-first with a **DYNAMIC order**: fire the leg whose book has less ask-volume FIRST
(`book::fire_pmus_first` / `_game`, reusing `depth_at_edge`'s per-leg ask ladders), so the thin leg's FOK-reject
is a **clean abort** (the deeper second leg is never sent — `HedgeNotFilled`), never a committed-then-naked leg.
The `PairAck` a/b SLOTS are preserved regardless of fire order, so all positional bookkeeping + the naked-leg
recovery (which flattens WHICHEVER leg ends naked, [0024]) are unchanged. `fire_pmus_first` is computed at the
signal under the same book lock as the depth, then threaded `Quote → spawn_submit → submit_pair → run_pair`.

## Alternatives considered

- **Simultaneous fire (the owner's repeated ask).** REFUTED: the dominant fade is pmus-thin (72 of 94 fires) —
  pmus having *no resting volume* is a LIQUIDITY problem no order-type fixes. Simultaneous would naked the Kalshi
  leg on every pmus-thin event (clean $0 abort → ~−2¢ recovery) and capture nothing extra. [L40]
- **Market orders instead of the FOK limit.** REFUTED: a market order walks the thin pmus book PAST the edge —
  a +3¢ arb becomes a −3¢ loss. The FOK-limit abort is the correct $0 outcome on an un-fillable book. [L40]
- **Keep pmus-first.** Optimal for the COMMON (pmus-thin) case but leaves the rarer thin-Kalshi case a naked
  pmus leg; dynamic generalizes it to a clean abort on EITHER thin side.

## Consequences

- **Live-verified:** a Kalshi-first naked leg (`miahigh`) auto-flattened cleanly (the dynamic order +
  both-venue recovery work end-to-end); the cap=2 scale-in add fired; 0 halts post-fix.
- `apply_second_leg_markup` now marks up the SECOND-fired leg (not unconditionally Kalshi); the recovery-cost
  gate now prices the FIRST-fired/thinner leg's spread (Kalshi's own book, incl. sports Kalshi-B, when Kalshi-first).
- **EXPOSED [0024]'s residual Kalshi-float-count WARN:** making the Kalshi leg recoverable triggered the recovery
  SELL's float `count` → Kalshi 400 → halt. Fixed (`634c5f1`, Kalshi count = `round() as u32`; [L39]).
- Known INFO (deferred): the thinness metric is TOTAL ask-volume, which mis-ranks vs at-touch (sports aborted
  fires had higher *displayed* depth than locked ones); harmless while recovery works — revisit before multi-contract.
- Commits `9c0c55d` (dynamic order, 182 tests, reviewed), on `e480f07`/`4f3959a`/`634c5f1`. Builds on
  [0020]/[0021]/[0024]; the recovery now flattens a naked leg on EITHER venue. See docs/sessions.md cont. 14.
