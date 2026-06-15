# 0021 — fill-or-kill orders (terminal fill verdict) + churn cooldown — the 2026-06-15 naked-leg incident fix

- **Date:** 2026-06-15
- **Status:** Accepted (from a LIVE incident); the pmus FOK enum is DEMO-gated before any live re-arm
- **Deciders:** owner (caught the discrepancy reconciling his accounts); Claude (diagnosis + fix + adversarial review)

## Context

The first multi-fire live run (5-position measurement run, [0020](0020-pmus-first-serial-plus-recovery-cost-gate.md)
pmus-first) churned ONE UFC fight (`KXUFCFIGHT-26JUN14TOPGAE` / `aec-ufc-jusgae-ilitop`) **4× in ~45s** and left
**UNTRACKED naked positions** — the owner caught it reconciling his Kalshi + polymarket.us accounts against the
bot's exec log. Root cause (diagnosed + adversarially verified): **both legs rested as GTC limit orders**, so the
bot's `filled:false` read was a **SNAPSHOT, not a terminal state** — a resting order fills SECONDS LATER, after the
read. The bot then acted on the stale read (cancelled the resting leg / aborted / recovered, all *assuming flat*)
while the order filled late → an untracked naked directional position. Every symptom was that one flaw: the Kalshi
-GAE @51 "cancel failed" (it had filled), the pmus Topuria @0.40 "missed" (it had filled), and the 4× churn (no
cooldown) + the duplicate-`client_order_id` 409s. The fight then settled, so the nakeds resolved directionally —
bounded to a few cents by the 1-contract caps. (Also: dry-run/smoke records polluted the live exec log, forcing a
`mode=live` filter during reconciliation.)

## Decision

1. **FILL-OR-KILL on every entry BUY** (`exec.rs` build_kalshi_payload + build_pmus_payload). An unfilled entry
   order is now KILLED at the venue, never rests, so `filled:false` is **terminal** — no late fill, no cancel
   race, on BOTH venues. Recovery/unwind SELLs stay GTC (they flatten a KNOWN leg; a rested SELL at worst triggers
   a needless halt, never a naked position). This is the structural fix.
2. **Per-slug entry cooldown** (`ENTRY_COOLDOWN_S`, default 30s; FRESH-entries-only so it never blocks a
   `qualifying_add` scale-in). Stops the churn + the duplicate-coid 409.
3. **Ambiguous-leg fail-close** (`recover_naked_leg`): under FOK a clean miss is `Ok(not-filled)`, so an `Err` on
   the unfilled leg means a 4xx/transport leg whose fate is UNKNOWN (may have landed+filled) → HALT rather than
   flatten the known leg (which would un-hedge a possible LOCK). A FOK-killed-order 404 cancel stays benign.
4. **Separate the dry-run exec log** (`executions.dryrun.jsonl`) so paper runs can't pollute the real-money
   reconciliation. Live path unchanged.

## Alternatives considered

- **Verify the cancel + reconcile (the diagnosis's first DIM-2 proposal).** Rejected as the PRIMARY fix: under FOK
  a cancel-404 is the BENIGN normal case (the order was killed), so halting on it would halt every recovery. FOK
  at the source is cleaner; the Err-fail-close is the right residual backstop.
- **IOC instead of FOK.** FOK (all-or-nothing) is strictly safer for a single-clip hedge leg — an IOC partial
  would itself be a naked-leg source. (qty=1 today, so they coincide; FOK is future-proof.)
- **A leg-fill-timeout that cancels + unwinds a resting order** — that's exactly the cancel-races-the-late-fill
  pattern that caused the incident. Killing at the venue (FOK) removes the race instead of racing it.

## Consequences

- **Closes the untracked-naked-position class at the source:** a resting entry leg can no longer fill after the
  bot read it not-filled. The recovery/abort cancel becomes a benign no-op (the FOK order is already gone).
- **Creates an invariant:** an entry leg is FOK (terminal) — `filled:false` means the order is dead, never
  resting. Recovery/unwind SELLs remain GTC by design.
- **Gate before re-arming:** the pmus `TIME_IN_FORCE_FILL_OR_KILL` enum is NOT yet primary-source-confirmed
  (Kalshi's `fill_or_kill` is) — **DEMO-verify** (a FOK BUY that can't fill must return 2xx-no-fill, the "clean
  miss" the recovery assumes, not a 4xx reject). SAFE-FAIL if wrong (pmus-first → a 400 aborts the entry, no naked
  leg — an availability stop). Independent adversarial review (`fadd544` + `e8a250e`): **0 CRITICAL, 3 WARNs fixed.**
- Re-arming stays an owner action under [0006](0006-deploy-on-digitalocean-consult-first.md) /
  [0015](0015-owner-override-live-trading-phase.md); this incident validated the staged-rollout caution that
  bounded the loss to cents. See [L34], docs/sessions.md 2026-06-15.
