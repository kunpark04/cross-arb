# 0024 — partial-fill handling: flatten any non-clean-lock entry (pmus ignores FOK, runs IOC)

- **Date:** 2026-06-15
- **Status:** Accepted (from a live incident) — supersedes [0021](0021-fill-or-kill-orders-and-churn-cooldown.md)'s "FOK makes `filled:false` terminal" **for pmus** (which ignores FOK)
- **Deciders:** owner (caught the stray pmus position); Claude (diagnosis + fix + two coding-agent passes + one independent adversarial review)

## Context

[0021] made entry orders FILL-OR-KILL so `filled:false` is terminal (no late-fill naked); [0022] API-doc-verified
Kalshi's `fill_or_kill`. But **pmus does NOT honor FOK.** The owner caught a stray pmus position
(`aec-itfm-andchi-timbre`, M'Chich) the bot had no record of; the raw create-response showed the requested
`TIME_IN_FORCE_FILL_OR_KILL` returned as **`IMMEDIATE_OR_CANCEL`** with `ORDER_STATE_PARTIALLY_FILLED`,
**`cumQuantity:0.01`** of qty 5. The bot's bool fill detection (`cum >= qty`) read the partial as `filled:false` →
pmus-first abort → the 0.01-contract partial was left NAKED. The untracked-naked-position class reopened, through a
hole the doc-verification couldn't see. ([L36])

## Decision

A non-zero venue fill that is **not a clean both-FULL lock** is treated as a real position and FLATTENED:

1. The `Ack` carries the ACTUAL filled qty (`fill_qty`, from pmus `cumQuantity` / Kalshi `fill_count_fp`);
   `filled = fill_qty >= qty`. Fail-safe: unparseable → `0.0` (never fabricate a fill).
2. When an entry is not a clean both-full lock, **EVERY leg with `fill_qty>0` is unwound at its OWN `fill_qty`**
   (`OrderIntent.frac_qty` carries the exact, possibly-fractional SELL size — a `0.01` partial unwinds as `0.01`).
3. **ATOMIC recovery:** price all naked legs first; if any is unpriceable, HALT and fire nothing; else one SELL per
   naked leg — a half-recovery (flatten one, abandon the other) is structurally impossible.
4. **Fail-CLOSE preserved:** any unwind `Err` → halt / `CRITICAL`, never assume flat. The self-synthesized
   `HedgeNotFilled` (a leg deliberately never sent) is a KNOWN fate → auto-unwind, not halt.

Covers **both** legs (the Kalshi second leg can also partial at multi-contract if it likewise ignores FOK). Invariant:
an entry either becomes a clean full lock, or ALL non-zero fills are flattened to zero — no partial position persists.

## Alternatives considered

- **Rely on pmus FOK ([0021]'s assumption).** REFUTED — pmus ignores FOK (observed in the raw response).
- **Hedge the partial** (fire Kalshi for the integer part). Deferred — more complex, and fractional partials can't be
  hedged on integer-only Kalshi; flatten-all is the safe first fix.
- **Handle only the pmus (first) leg.** Rejected — the independent review caught that the Kalshi second leg can also
  partial at multi-contract; generalized to flatten both.

## Consequences

- Closes the partial-fill naked-position hole on BOTH venues. Reviews: 2 coding-agent passes + 1 independent
  adversarial → **SAFE_TO_ARM, 0 CRITICAL, 169 tests green**, clippy clean (`3f2c58a`).
- **Residual WARN (safe-degrading):** the Kalshi recovery SELL serializes a float `count`; if Kalshi rejects floats
  the recovery HALTS (fail-close), never nakeds. Verify Kalshi float-`count` acceptance + observe a real Kalshi FOK
  partial before relying on Kalshi-partial auto-recovery.
- Supersedes [0021]'s "FOK makes `filled:false` terminal" **for pmus**; the pmus-first + recovery machinery
  ([0020](0020-pmus-first-serial-plus-recovery-cost-gate.md)/[0021]) is otherwise intact. See [L36], [L37],
  docs/sessions.md 2026-06-15.
