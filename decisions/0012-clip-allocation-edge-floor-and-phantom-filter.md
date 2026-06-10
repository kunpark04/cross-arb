# 0012 — Clip-stage allocation = edge-floor + deploy-to-full cap (not FIFO); capturable() drops restart-censored phantoms

- **Date:** 2026-06-10
- **Status:** Accepted (allocation *direction* preliminary / method-demo; the `capturable()` phantom fix is firm).
  **Magnitudes corrected by [0013](0013-econ-grid-step-twin-and-measurement-integrity.md)**: the OOS
  rows below included ONE econ position that turned out to be a settlement-identity phantom (the
  off-by-one `≥T`↔`>T` pairing). Econ-quarantined + close-lag-corrected reruns give **+9% (5% cap) /
  +58% (10%) / +141% (20%) vs FIFO** — not +64%/+153%/+269%. Direction (floor earns, cap diversifies,
  FIFO loses) unchanged.
- **Deciders:** owner + Claude

## Context

The not-yet-built trade-selection ("clip") stage must allocate a fixed bankroll across arbs that arrive over
time and **lock to settlement** (no early exit). `account_sim.py` allocated **strictly FIFO by arrival**, which
pins the whole bankroll on whatever crosses first (at $500: one deep clip eats ~$500, funds 1 of 219). The owner
asked whether prioritising bigger arbs — specifically *"wait 1 s, sort that batch largest-first"* — would help.
Measured + out-of-sample-tested + audited in [research/allocation-policy-2026-06-10.md](../research/allocation-policy-2026-06-10.md)
(`scripts/alloc_policy_experiment.py`, `scripts/clip_threshold_test.py`; audit
`tasks/_agent_bus/20260610-0522/`). The investigation also surfaced a **book-initialization phantom** (a 37.7¢
ITF-tennis "arb" captured 1.5 s after a resubscribe, flat `c2==c1==c0` ladder, `censored="restart"`) that was
**75% of the in-sample headline** because the shared `capital_sim.capturable()` did not exclude restart-censored
episodes even though `analyze_persistence.summarize()` already defined them as not-capturable.

## Decision

1. **The clip stage allocates by a global edge floor + a per-pair cap, not FIFO and not a batch-sort.** Fund any
   pair clearing a hard **τ ≥ 2¢ floor** (never lower), sizing each up to a **clip cap chosen so the bankroll
   fully deploys without over-concentrating** (~10–20% of bankroll/pair on current data; top-1 ≤ ~25%).
   Evaluate at arrival and fire both legs immediately — allocation priority is a bankroll decision over days,
   **never** an execution delay.
2. **`capital_sim.capturable()` excludes restart-censored episodes by default** (`drop_restart=True`), matching
   `analyze_persistence`'s own CAPTURABLE definition. Quality gates live at the **shared chokepoint**, not in one
   report's metric ([L20]).

## Alternatives considered

- **FIFO by arrival** — worst when the bankroll binds (funds 1/219; ignores edge entirely). Rejected.
- **"Wait 1 s + sort the batch" (batch1s)** — captures only 3.6% of the FIFO→optimal gap (competing arbs span
  *days*, not seconds) and *adds* a ~39%-naked-leg latency tax (`shadow_fill`). Rejected.
- **Clip cap alone (no floor)** — −3% OOS; capping in FIFO order just diversifies into thin arbs. Kept only as
  **risk-control** (bounding per-pair exposure vs settlement-void/leg-fail), not as the return lever.
- **High τ + no cap (the "fitted" policy)** — high paper PnL but 93% one position; on OOS it was one lucky econ
  contract (drop it → below FIFO). Too fragile.
- **Dynamic edge-proportional cap (cap% = k·edge)** — sound (conviction sizing) but dilutes into dust without a
  floor; with a 2¢ floor it only *ties* the simple static rule. A prior, not settled by this data.
- **Leave `capturable()` as-is** — rejected: it admitted a phantom that was 75% of the in-sample headline.

## Consequences

- **Enables** a concrete clip-stage spec for when the trade layer is built (0010 / read-only→trade boundary):
  2¢ floor + deploy-to-full cap, fired at arrival.
- **Changes every allocation analysis** that calls `capturable()` (`account_sim`, `alloc_policy_experiment`,
  `clip_threshold_test`, `capital_sim`): restart-censored phantoms are now dropped (candidates 226→219;
  in-sample headline +7127%→+1744%; **OOS conclusions unchanged**). Pass `drop_restart=False` to opt back in.
- **Creates an invariant** other code/docs depend on: "capturable" = edge + persistence + depth + not-stale +
  **not restart-censored**, defined identically in `analyze_persistence` and `capital_sim`.
- **Forecloses nothing** the data could overturn: the allocation *magnitude* is a **method demo on 0.81 d / one
  event cluster**, not validated. Revisit τ/cap and static-vs-dynamic when weeks of multi-date data land.
- **Open hardening (deferred — needs a monitor-schema change):** the `k=0` fresh-subscribe tell is discarded
  (`build_episodes` keeps only `max(p,k)` age) and there is no flat-depth (`c2==c1==c0`) guard, so a phantom with
  a *fresh* book but no restart marker could still slip through. Log both venue ages + a flat-ladder flag.
- Reinforced by lessons [L18] (a too-good number is the asterisk), [L19] (quote the OOS non-tuned number),
  [L20] (gate at the shared chokepoint; instrumentation ≠ gating).
