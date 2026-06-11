# 0014 — Pre-register the allocation rule before the multi-week data arrives

- **Date:** 2026-06-10
- **Status:** Accepted
- **Deciders:** owner (queued + shaped the rule in the 2026-06-10 policy read), Claude (spec + freeze)

## Context

The 0012 allocation result (edge floor + per-pair cap beats FIFO; corrected by 0013 to **+9%…+141%
OOS**) is a *method demo*: levers chosen after seeing the same 0.86 days they were scored on, one
event cluster, paper-gross. Two phantoms ([L20] book-init, [L21] econ off-by-one) have already shown
how easily this dataset flatters a design. The multi-week post-0013 dataset now accumulating is the
first chance at a **confirmatory** test — but only if the rule and the analysis are frozen *before*
the data exists; otherwise it becomes another tuning pass ([L19]).

## Decision

Freeze, as of this commit, the allocation rule and its validation protocol in
[research/allocation-prereg-2026-06-10.md](../research/allocation-prereg-2026-06-10.md):

- **H1 (primary):** the 0012-tested `reservation` semantics (arrival-order, permanent skip) with
  τ = 2.0¢ booked edge floor + per-pair caps **differentiated by category tail** — weather 20%,
  sports 10% (no MLB unwind rule yet), econ 5% — over `account_sim`'s frozen economics;
  **$500 = the verdict arm** / $2,000 descriptive robustness.
- **H2 (secondary):** identical, ordered by `booked_edge / expected_lock_days`; lock-day priors
  **frozen in the prereg itself** (weather 1.2 d / sports 15 d / econ days-to-release —
  `capital_velocity.py`'s lock-days are hardcoded estimates, not measurements; audit C3d).
- **Dataset:** post-`ECON_REMAP_DEPLOY_TS` epoch only; trigger = first pull after **≥21 post-epoch
  event-days (K≥7 folds; 14–20 only if collection halts)**; ≥30 τ-clearing one-per-market
  candidates in the folded span or the test downgrades to descriptive.
- **Procedure:** contiguous 3-**arrival**-day folds, **one market in exactly one fold**, five-arm
  lever decomposition (2×2 + H2), **fold-level inference** (t-interval on fold ΔPnLs + exact
  sign-flip permutation; per-position resamples demoted to concentration diagnostics), a
  market-level ≤33% pooled-ΔPnL share gate, friction arm from upstream-frozen constants
  (post-epoch `shadow_fill` curve × frozen E_loss) — pass/fail criteria fixed in the prereg §4
  (CI entirely above 0, tie rule, phantom-verdict variant, selftest-first bug rule, MDE on null).

A stats-methodology audit of the prereg ran before freezing (path in prereg §6); its accepted
fixes are part of the frozen text.

## Alternatives considered

- **Just re-run 0012's scripts when data arrives** — that is the tuning-pass trap: the cap sweep
  (5/10/20/40%) would again pick the best row post-hoc; rejected per [L19].
- **Pre-register a single flat cap (~15%)** — ignores the category-shaped tail risk the project
  has measured (MLB void divergence, econ capital-death); the differentiated caps encode those
  *already-documented* findings rather than new tuning.
- **Wait and design the test with the data in hand** — maximally informed but unfalsifiable;
  the whole point is to buy a confirmatory bit with discipline.

## Consequences

- The next allocation analysis is **confirmatory or descriptive — never silently exploratory**;
  any rule change motivated by the new data requires a fresh prereg + subsequent data.
- The 0012 cap sweep is superseded; quoting a swept cap as "the result" is now a violation.
- If H1 fails its gates, the deployable-allocation claim reverts to OPEN (and that is a
  publishable outcome, not a failure of the project).
- Creates an invariant other docs depend on: post-epoch-only data for confirmatory claims
  (`analyze_persistence.load()` quarantine + `ECON_REMAP_DEPLOY_TS`).
