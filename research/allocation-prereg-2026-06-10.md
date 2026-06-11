# PRE-REGISTRATION — confirmatory test of the clip/allocation rule (frozen 2026-06-10)

**Status:** BINDING once committed (decision [0014](../decisions/0014-preregistered-allocation-rule.md)).
**Purpose:** the 0012/0013 allocation result (+9% to +141% over FIFO OOS) came from a 0.86-day method
demo whose levers were *chosen after seeing that data*. To make the next test **confirmatory rather than
another tuning pass** ([L19]), this document freezes the rule, the dataset definition, the procedure,
and the pass/fail criteria **before** the multi-week post-0013 dataset exists. Anything not frozen here
that touches the result is exploratory and must say so.
**Audited pre-freeze:** [stats-ml-logic-reviewer, 2026-06-10](../tasks/_agent_bus/20260610-prereg/stats-ml-logic-reviewer.md)
(3 CRITICAL / 7 WARN / 5 INFO — all incorporated below; the C-fixes are why §3's inference is
fold-level, §3's folds are arrival-date one-market-one-fold, and §1 pins semantics + a code freeze).

## 1. Frozen decision rules

### H1 (primary) — edge floor + category-differentiated per-pair caps

**Funding semantics = the 0012-tested `reservation` rule** (minimal delta from validated code, audit C3):
candidates are considered in **arrival order**; a candidate is funded iff its booked per-contract edge
≥ τ **and** its category cap permits; size = min(depth-capped clip, cap, affordable); a candidate
unaffordable at its arrival is **permanently skipped** (no recycle-time reconsideration — that upgrade
is queued exploratory work, not this test).

| Constant | Frozen value | Rationale (recorded, not re-arguable later) |
|---|---|---|
| Edge floor τ | **2.0¢ booked per-contract** (≈3.5¢ touch) | the friction buffer: taker round-trip ≈3.5¢ at P≈0.5; floor=return lever (0012 F3) |
| Per-pair cap — weather | **20% of bankroll** | identity verified + ~1.2 d capital + a measured evening exit window |
| Per-pair cap — sports | **10% of bankroll** | MLB void tail (2-day vs 2-week reschedule divergence) with NO unwind rule yet: a voided 20% clip = −10–20% of bankroll |
| Per-pair cap — econ | **5% of bankroll** | settlement-cleanest but capital-dead (weeks–months to release; pmus book frozen at resolution) |
| Bankroll | **$500 = the VERDICT arm**; $2,000 = descriptive robustness only, with the pre-stated expectation that the effect attenuates as the bankroll unbinds (audit W5a) | matches 0012 tables; $2k probes the regime change without verdict freedom |
| Economics | `account_sim` per-contract cost, book-average profit, `void_haircut` ON (`void_mult=1`), `one_per_market`, `capturable()` with **edge_min=0, window_min=0, liq_floor=1, max_age=off, drop_restart=True, drop_flat=False** | identical economics to 0012; gate args spelled out because `capturable()` has no defaults for the positional args (audit I1) |
| Inherited numeric constants (frozen verbatim, audit I1) | `DEPTH_BOUNDARY_NET=0.005`; `MLB_POSTPONE_RATE=0.013`, `P_GAP=0.4`, `VOID_LOSS_FRAC=0.5`, `SPORTS_VOID_RATE=0.005`; `OFFSET_H=28` | the capital_sim "ESTIMATE — tune w/ data" constants may NOT be tuned on post-epoch data for this test; refreshing them from primary sources is a labelled sensitivity arm |
| `expected_lock_days` (H2's denominator) | **frozen NOW: weather 1.2 d; sports 15.0 d; econ = per-market days-to-release (calendar arithmetic, exogenous)** | `capital_velocity.py`'s lock-days are hardcoded estimates, not measurements (audit C3d) — freezing the priors now eliminates the same-data leak entirely (audit W1); any post-epoch *measured* lock-day update is a labelled sensitivity arm whose measurement procedure ships inside the §3.0 code freeze |

No other knob may be set per-run. The 0012 OOS cap sweep (5/10/20/40%) is **superseded** — the
category caps above are the rule; no cap sweep may be quoted as the result.

### H2 (secondary) — edge-RATE ordering

Identical to H1 in every constant and semantics, except candidates are **considered in descending
`booked_edge / expected_lock_days`** (¢ per dollar-day) within the same arrival-order reservation
framework's batch granularity as implemented at code freeze — the τ=2¢ absolute floor **stays** (it is
a friction bound, not a preference), so any H2−H1 delta is attributable to velocity-awareness alone.

H2 exists because a flat edge floor gets categories backwards (13¢ U-3 locking ~22 d = 0.6¢/$-day vs a
3¢ weather arb at 1.2 d = 2.5¢/$-day). H2 is adopted **only** under §4's gate. **Known design
limitation (audit W2): 3-day folds with booked-at-entry PnL and per-fold bankroll resets truncate
exactly the opportunity cost H2 prices — an H2 null here is uninformative about deployment-horizon
velocity effects and must NOT be quoted as falsifying velocity-ordering.** The §3.6 continuous arm
gives velocity its non-truncated descriptive look.

### 1b. CODE FREEZE (audit C3)

All machinery this prereg requires but which does not yet exist — per-category caps, H2's ordering
key, the fold harness, fold-level inference, the §3.2 friction formula, the §3.6 continuous arm —
must be **implemented, selftested, and committed BEFORE the §2 run trigger**. Analysis-code changes
after the trigger are treated identically to constant changes (forbidden; a bug fix follows §4's
bug rule). The frozen pipeline is the repo state at the commit that lands that machinery; cite its
hash in the results doc.

## 2. Frozen dataset definition

- **Epoch:** records with `t ≥ ECON_REMAP_DEPLOY_TS (= 1781082189, 2026-06-10 09:03 UTC)` only —
  the corrected-schema restart. Nothing pre-epoch enters the confirmatory analysis.
- **Source:** the daily `Kalshi/data/cross-arb/` mirror (copy-keep, sha256-verified).
- **Run trigger (audit W4/W6):** the confirmatory run executes on the **first daily pull after 21
  post-epoch event-days** (K ≥ 7 folds; earliest ~2026-07-01). Running at 14–20 days is permitted
  ONLY if collection halts before 21 (droplet decommission); below 14 days no confirmatory run.
  One interim *descriptive* peek is allowed at 7 days (labelled DESCRIPTIVE, no policy comparison
  published from it, and **it may not alter the run timing**).
- **Minimum power floor (scoped per audit I5):** ≥ **30 τ-clearing, `one_per_market`-deduped,
  post-epoch capturable candidates within the folded span** (gross arm; dropped remainder days and
  far-future-event candidates outside the folded arrival span excluded from the count). Below 30,
  the test downgrades to descriptive-only — thin data must not be laundered into a verdict.
- **Censoring:** `capturable()` with the §1 frozen gate args (restart-censored dropped;
  `ws_reconnect`/resync windows censored by `load()`). The `drop_flat` lens stays opt-in diagnostic
  ([L15]) and is reported alongside, never silently substituted.

## 3. Frozen procedure (execution order matters)

0. **Code freeze per §1b** (machinery committed + selftested before anything below runs).
1. **Upstream constants first:** re-run `shadow_fill` (leg-fail curve at sub-second resolution) on
   the post-epoch data; freeze its curve as constants before any allocation PnL is computed.
   (Lock-days are already frozen in §1 — no measurement step remains on the H2 path.)
2. **Friction-arm constant (one global scalar, audit W7):**
   `haircut = fill_fail(L*) × E_loss`, where **L\*** = median `latency_probe` RTT measured over the
   collection window, rounded to the nearest point of the post-epoch `shadow_fill` latency grid;
   **fill_fail(L\*)** = that curve's naked-leg rate; **E_loss = 5.0¢/contract, frozen** (unwind cost
   of the naked leg: re-cross a typical 1–3¢ spread + both venues' fees at mid-range prices —
   deliberately conservative). Applied via `run_policy`'s scalar haircut argument. Any per-category
   or px-based refinement is a labelled sensitivity arm inside the §1b code freeze.
3. **Folds (audit C2):** folds are contiguous blocks of **UTC ARRIVAL dates** (the date of a
   candidate's `open_t`) — the event-date partition is storage layout only. **K = floor(arrival-days
   / 3); the remainder is always dropped.** **Each market enters exactly ONE fold** — the fold
   containing its first post-epoch τ-clearing capturable arrival; its later episodes are not
   candidates in any other fold (extends `one_per_market` across folds). *No fitting occurs in any
   fold* — the rule is fully frozen — so the folds are K independent fresh-$500 replications
   measuring dispersion, not cross-validation.
4. **Per fold — five arms** (the 2×2 lever decomposition + H2, audit I2): {FIFO, floor-only (τ=2¢,
   no cap), cap-only (category caps, no floor), H1 (floor+cap), H2}, fresh bankroll each fold.
   A bundled lever that doesn't move the held-out metric is not part of the edge ([L19]).
5. **Inference (audit C1 — fold-level):** for each non-FIFO arm, ΔPnL_k = PnL_arm,k − PnL_FIFO,k
   per fold k. **Pooled estimate = the unweighted mean of ΔPnL_k** (each fold is an identical
   fresh-$500 3-day replication). **95% CI = t-interval on the K fold deltas, reported alongside
   the exact sign-flip permutation p-value.** Report per fold: PnL per arm, ΔPnL, funded pairs,
   deploy %, top-1 position share (H1 arm), category mix, per-event-cluster (city-date / game)
   concentration. The per-position resample and leave-one-position-out are **reported as
   concentration diagnostics only** — never pass/fail evidence.
6. **Continuous descriptive arm (audit W2):** one full-span single-$500-bankroll run of H1 vs H2
   reporting PnL per deployed-dollar-day — the non-truncated look at velocity. Descriptive only.
7. **Friction arm:** repeat steps 4–5 with the step-2 haircut ON. The confirmatory claim must
   survive this arm.

## 4. Pass/fail criteria (frozen)

**What "CONFIRMED" means (audit W3):** H1 captures more **booked edge** than FIFO under the frozen
expected-cost economics — an *allocation-efficiency* claim, **not realized PnL** (no settlement
outcomes enter; positions cannot lose by construction). Settlement-realization reconciliation
(`settle_recon`) is a separate open item and is required before any capital decision.

**H1 CONFIRMED** iff ALL of (verdict arm = $500, gross and friction both):
- pooled ΔPnL(H1 − FIFO): t-interval 95% CI **entirely above 0**, gross **and** friction arms;
- H1 > FIFO in ≥ ⌈2K/3⌉ folds, where **|ΔPnL_k| ≤ $0.01 counts as a non-win** (tie rule, W5c);
- **no single MARKET contributes > 33% of pooled ΔPnL(H1 − FIFO)** (market-level share gate,
  audit C2 — binding); per-fold top-1 position shares are reported descriptively (H1 arm);
- floor-only also beats FIFO (CI entirely above 0, gross arm) — the mechanism claim: the return
  lever is the floor; "H1 wins but not via the floor" is a FAIL.

**H2 ADOPTED over H1** iff H2 meets all H1 criteria AND pooled ΔPnL(H2 − H1) CI entirely above 0.
(An H2 null is recorded as "uninformative at this horizon" per §1's W2 note, not as a refutation.)

**Null-result asymmetry (audit W6):** a FAIL is *"not confirmed at this n"*, not *"the floor does
not work"*. Any null is reported with the **realized minimum detectable effect** — the smallest
pooled ΔPnL the K-fold CI could have excluded at the observed dispersion.

**Phantom adjudication (audit W5d):** a suspected phantom position is adjudicated by the raw-record
checklist (age, depth-ladder shape, censor marker, settlement identity — [L18]/[L20]/[L21]). If the
checklist concludes phantom, the **EXCLUDED variant is the verdict** (included variant reported for
audit); if adjudication is ambiguous, the position **stays IN**.

**Bug rule (audit W5h):** a "pipeline bug" = divergence from documented intended behavior
demonstrated by a **failing selftest written BEFORE the fix**; the post-fix full re-run is the
verdict **regardless of direction**, with pre-/post-fix deltas reported.

**On failure:** the allocation question REOPENS; any new rule informed by this dataset is
exploratory and must be re-pre-registered and tested on *subsequent* data. No re-cutting folds, no
post-hoc τ/cap/bankroll changes, no dropping folds.

## 5. What this prereg does NOT cover (stays exploratory)

- Maker-side execution (separate study; fee asymmetry per
  [fee-pin-2026-06-10.md](fee-pin-2026-06-10.md) — Kalshi weather maker $0 + pmus −0.0125 rebate).
- Correlated-exposure caps per event cluster (reported as a diagnostic here; capping is queued
  separately).
- Recycle-time re-scoring of still-open arbs (explicitly excluded from H1's semantics in §1);
  adverse-selection direction gates; fill-contingency pricing — all queued in
  [tasks/todo.md](../tasks/todo.md) explorations.
- Any intraday execution-latency policy (allocation is a bankroll decision over days; execution
  is 0010's domain).

## 6. Audit trail

- **Pre-freeze methodology audit:** [tasks/_agent_bus/20260610-prereg/stats-ml-logic-reviewer.md](../tasks/_agent_bus/20260610-prereg/stats-ml-logic-reviewer.md)
  — verdict "partially sound" on the draft; 3 CRITICAL (per-position bootstrap ill-posed → §3.5
  fold-level inference; event-date folds' multi-fold market duplication → §3.3 arrival-date
  one-market-one-fold + §4 market-share gate; unfrozen machinery/semantics → §1 pinned semantics +
  §1b code freeze + frozen lock-day priors), 7 WARN, 5 INFO — **all incorporated in this text**.
- Antecedents: [allocation-policy-2026-06-10.md](allocation-policy-2026-06-10.md) (the method demo
  + its 0013 correction), decisions [0012](../decisions/0012-clip-allocation-edge-floor-and-phantom-filter.md),
  [0013](../decisions/0013-econ-grid-step-twin-and-measurement-integrity.md), lessons [L15]/[L18]/[L19]/[L20].
