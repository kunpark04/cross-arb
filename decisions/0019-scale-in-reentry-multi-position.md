# 0019 — Scale-in + re-entry capability (multi-position-per-slug)

- **Date:** 2026-06-14
- **Status:** Accepted (owner-directed; safe-by-default, OFF until armed)
- **Deciders:** owner (directive); Claude (design + build + independent review)

## Context

The owner directed adding two "adding" strategies to the live bot: **SCALE-IN** (pile more size onto a
still-live edge on a held bucket) and **RE-ENTRY** (take a new, separate arb on a bucket already held to
settlement). A read-only feasibility investigation (`docs/sessions.md` 2026-06-14 cont. 4;
`scripts/flip_add_backtest.py` + `scripts/bankroll_add_backtest.py` + two stats audits) found both **marginal
on current data** — true scale-in ≈ $0, re-entry ≈ $0 bankroll-netted (the base strategy already saturates both
$250 pools; **capital, not opportunity, is the binding constraint**). The owner directed building both anyway,
for **option value** — ready if capital or the opportunity set grows. The live bot's one-position-per-slug guard
(`bot-rs/src/main.rs:497`) explicitly BLOCKED re-entry, for a bookkeeping-safety reason: it prevented
exposure-reservation / unwind desync.

## Decision

Build both as a **multi-position-per-slug** capability gated behind separate `ENABLE_SCALE_IN` /
`ENABLE_REENTRY` flags (both **OFF by default**) + a `MAX_POSITIONS_PER_SLUG` cap (default **1**). The
load-bearing change is making exposure release **exact per-position** — each position stores its own `cost_per`,
and `subtract_exposure` is the exact inverse of `reserve_exposure` — so stacking positions on one slug never
desyncs. Design of record: [tasks/scale-in-reentry-design.md](../tasks/scale-in-reentry-design.md). Built in
`8164710`; the W-1 fix in `95cf753`.

## Alternatives considered

- **Don't build it (the analysis recommendation).** Marginal value + relaxes a safety guard. Overridden by the
  owner for option value; building it safe-by-default (OFF) costs nothing until armed.
- **A composite `(slug, id)` map key** instead of a `Vec` per slug — rejected: scatters a slug's positions across
  the map and splits the per-slug poll `prev` ownership, re-introducing the desync the design closes.
- **Re-entry only (skip scale-in)** — they share the same multi-position bookkeeping, so building both is the
  same change.

## Consequences

- **Enables** stacking multiple locked pairs on one bucket, up to the per-pair notional + per-slug count caps
  (the concentration controls). Correlated-settlement risk (N pairs lose together if a bucket's grading diverges)
  is bounded by those caps + priced per-pair via `void_haircut`.
- **Forecloses** nothing at defaults — both flags off + cap 1 ⇒ behavior is **BYTE-IDENTICAL** to the prior bot
  (smoke unchanged, the existing baseline green: 147 tests).
- **Creates an invariant:** *a filled leg is NEVER left without either a fired recovery or a halt.* An
  independent review found this could break (W-1: an unwind racing a half-filled add left a SILENT naked leg) and
  it was fixed — `flattening` now records its KIND so a recovery fails-CLOSED when an unwind holds the slot
  ([tasks/lessons.md](../tasks/lessons.md) L31).
- **Before arming** (`ENABLE_*` + cap ≥ 2 → real money): the standing demo round-trip + pmus POST-signing + a
  FINAL fresh independent review of the W-1 fix (it was self-written). This is the riskiest money-path change
  since the Dutch book; arming stays an owner action under [0006](0006-deploy-on-digitalocean-consult-first.md) /
  [0015](0015-owner-override-live-trading-phase.md).
