# 0022 — lower the LIVE bot edge floor to 1.5¢ (owner risk-appetite; research prereg τ=2¢ untouched) + Kalshi FOK API-doc-verified

- **Date:** 2026-06-15
- **Status:** Accepted (owner-directed; **a-priori** risk-appetite, NOT data-tuned)
- **Deciders:** owner (requested 1.5¢); Claude (EV + governance + scope workflow with an adversarial reviewer; Kalshi FOK API-doc verification)

## Context

The NYC-high-temp June-15 diagnostic ([L35], docs/sessions.md 2026-06-15) established that the live temp arbs gate
on the **EDGE**, not staleness or empty books: the live buckets net **+1.0…+1.1¢** after fees, below the 2¢ floor,
and the eye-catching ~12¢ YES-price "gaps" are mostly the **pmus bid-ask spread** (a 80–81° bucket shows pmus YES
19¢ but its NO side costs 91¢ — a 10¢ internal spread vs Kalshi's 1¢). The owner asked to revisit the 2¢ floor,
suggesting **1.5¢**. The 2¢ value is **pre-registered** ([0014](0014-preregistered-allocation-rule.md), the [L19]
anti-in-sample-tuning discipline), so lowering it is not purely mechanical. An adversarial workflow (EV / governance
/ scope, plus a skeptical money-path reviewer) verified the change **before** it was made.

## Decision

1. **Lower the LIVE bot's `EDGE_FLOOR_CENTS` compiled default `2.0 → 1.5`¢** (`bot-rs/src/config.rs:80`). The field is
   a NET-of-both-leg-fees floor (`pricing.rs::realized_net_dollars` subtracts each leg's marginal taker fee *before*
   the floor), so 1.5¢ is a 1.5¢-**net** bar. Already env-overridable (`EDGE_FLOOR_CENTS`); this just moves the
   standing default. **Minimal scope — one literal**; the test fixtures hardcode their own 2.0 independently of
   `from_env`, so **0 tests break** (163 green).
2. **The 0014 pre-registered τ=2.0¢ stays the RESEARCH/confirmatory-path value** (`account_sim`/allocation prereg) —
   **UNTOUCHED.** Only the live-bot operational parameter moves. The descriptive mirror
   `scripts/backtest_current_strategy.py:52` is left at 2.0 (sync separately if wanted — **never** by editing the
   prereg τ).
3. **Record the Kalshi FOK API-doc verification** (the change's safety rests on FOK actually killing unfilled legs):
   `docs.kalshi.com` confirms `POST /trade-api/v2/portfolio/orders` takes `time_in_force` as an **OPTIONAL** enum
   `{fill_or_kill, good_till_canceled, immediate_or_cancel}`; `"fill_or_kill"` is valid and an **unknown value 400s**
   (it is NOT silently rested as GTC). This pins 0021's previously-uncited assertion ("Kalshi's `fill_or_kill` is
   confirmed") to its source (now in the `exec.rs` comment).

## Alternatives considered

- **Keep 2.0¢ (the prereg value).** It's the *research* prereg; the live bot is the owner's operational call under
  [0015](0015-owner-override-live-trading-phase.md), and the 0.5¢ buffer cut is immaterial under the current regime
  (below).
- **Lower to ~1.0¢ to actually catch the NYC +1¢ buckets.** Rejected: at ~1¢ the execution buffer ≈ the residual
  unwind cost (no margin), it's a bigger prereg deviation, and the +1¢ buckets stay below 1.5¢ anyway. 1.5¢ is a
  measured step, not a chase of one snapshot.
- **Env-override only (`EDGE_FLOOR_CENTS=1.5`), leave the default at 2.0.** The owner wants 1.5¢ as the standing live
  value, so move the compiled default (and its `.env.example`/README docs).
- **Couple the floor change with arming/relying on FOK in one step.** Rejected (adversary's top caveat): floor
  reduction and FOK verification are independent decisions — verify the Kalshi FOK behavior first (done, via the API
  docs), *then* lower the floor.

## Consequences

- **Widens the funnel, doesn't unlock the motivating arbs.** The live bot now fires arbs in the **[1.5, 2.0)¢ NET**
  band (was ≥2.0¢). The NYC June-15 buckets (+1.0–1.1¢) **still don't clear** — this is a band-widening, not a fix for
  those specific buckets.
- **EV basis (workflow):** ~**+1.0–1.3¢/fired arb** at a 1.5¢ floor; residual naked-unwind cost ~**0.3¢** (the
  recovery-cost gate bounds the pmus-unwind spread ≤ edge); break-even miss-rate ~**97%** vs the measured 15–30%. The
  0.5¢ buffer cut is immaterial because FOK + pmus-first ([0020](0020-pmus-first-serial-plus-recovery-cost-gate.md))
  + the recovery-gate already collapsed ~4 of 5 naked paths; the surviving path's cost is gate-bounded by the edge.
- **CONDITIONAL safety — the recovery-cost gate must stay ON.** The bounded-unwind guarantee depends on
  `MAX_RECOVERY_SPREAD_RATIO=1.0` (prod default). Disabling it removes the ≤edge spread bound (the original −11¢
  naked-unwind mode). Do not disable it while running at 1.5¢.
- **Governance (workflow):** governance-clean as an **a-priori** owner risk-appetite call — widen the funnel in the
  1-contract staged rollout. It would only become an [L19] violation if **back-justified from the accumulating
  post-0013 dataset** ("1.5 looked better in the data"). It was not; it must not be.
- **Still unverified live:** (a) the opportunity DISTRIBUTION at 1.5–2.0¢ is unmeasured (the §2 leg-fill data is one
  sports-heavy ~16h day, preliminary) — re-confirm after the multi-week post-redeploy pull; (b) FOK is API-doc-verified
  on Kalshi but a **live demo-sandbox kill-confirm is still the gold standard** ([L17]/[L32]) for both the Kalshi
  `fill_or_kill` behavior and the pmus `TIME_IN_FORCE_FILL_OR_KILL` enum (0021's demo gate) — the 0-fires FOK run
  never exercised either. Re-arming stays the owner's gated action ([0006](0006-deploy-on-digitalocean-consult-first.md)
  / 0015). See [L35], docs/sessions.md 2026-06-15.
