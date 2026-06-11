---
from: coding-agent
run_id: 20260611-residual-fix
timestamp: 2026-06-11T20:44:54Z
task_summary: Close the 4 order-path residuals (naked-leg recovery, pmus fill-detection fields, per-market tick+min-size, slug-identity comment) in bot-rs.
files_changed: [bot-rs/src/main.rs, bot-rs/src/exec.rs, bot-rs/src/discovery.rs, bot-rs/README.md]
files_created: []
context_loaded: [CLAUDE.md, bot-rs/README.md, bot-rs/src/main.rs, bot-rs/src/exec.rs, bot-rs/src/discovery.rs, bot-rs/src/types.rs, bot-rs/src/unwind.rs, bot-rs/src/book.rs, agent-memory/coding-agent/MEMORY.md, agent-memory/coding-agent/pattern_event_loop_inline_blocking_io.md, agent-memory/coding-agent/pattern_rest_envelope_probe.md, agent-memory/coding-agent/pattern_regex_port_quantifier_truncation.md, tasks/lessons.md]
libraries_consulted: [docs.polymarket.us (OpenAPI orders-schema.json)]
self_review_artifact: tasks/_agent_bus/20260611-residual-fix/code-logic-reviewer.md
---

## What I changed
- **FIX A (CRITICAL) — naked-leg auto-recovery** (`main.rs::recover_naked_leg` + `apply_outcome` Entry branch): a one-leg-filled entry now CANCELS the resting leg (`backend.cancel`, spawned best-effort) + FLATTENS the filled leg with a single marketable SELL read from its live book, records NO hedge, releases the reservation. Halt is the BACKSTOP only (unpriceable book / no leg metadata). Added a new single-leg primitive `ExecutionBackend::submit` (+ `LiveBackend::run_one`, `DryRunBackend::submit`) since the trait was pair-only.
- **FIX A — failed-flatten safety net** (`main.rs` `SubmitKind::Recovery` arm): the recovery SELL routes back as a dedicated `Recovery` outcome; if that SELL does NOT fill (leg still naked) it engages the kill-switch. This replaced a first design (simulated-sentinel padding into a `Unwind` ack) that the self-review caught silently skipping the halt — see the review artifact.
- **FIX B (CRITICAL) — pmus fill fields pinned** (`exec.rs::pmus_order_filled`): now keys on the OpenAPI-confirmed names — `cumQuantity>=qty`, `leavesQuantity==0 && cumQuantity>0`, summed `executions[].lastShares`, terminal `state`=ORDER_STATE_FILLED; unwraps a `{"order":{..}}` wrapper; `remainingQty` kept as a defensive alias. Absence -> `filled:false` (fail safe, unchanged).
- **FIX C (WARN) — per-market pmus tick + min-size** (`discovery.rs` `Pair.pm_min_tick/pm_min_qty` from `orderPriceMinTickSize`/`minimumTradeQty`; threaded to `main::LivePair`; enforced in `build_legs`): a pmus leg below `minimumTradeQty` skips the whole pair; the pmus leg price quantizes to the tick (`quantize_to_tick`). Kalshi untouched. Recovery-SELL quantization left as a precise TODO (fails safe to halt; see review).
- **FIX D (INFO) — slug-identity comment** at `exec.rs::pmus_base` + `discovery.rs::PM_MARKETS`: states gateway-catalog slug == api-order slug is VERIFIED live (2026-06-11), not assumed.
- README: naked-leg recovery moved from TODO→BUILT; test count 112→123.

## Why (non-obvious only)
- The pair-shaped `submit_pair`/`PairAck` outcome machinery had no single-leg path; the recovery flatten is intrinsically one order, so a dedicated `submit` primitive + a `Recovery` `SubmitKind` variant is cleaner (and safer) than padding the unused second leg.
- pmus `executions[]` IS in the OpenAPI schema's synchronous `CreateOrderResponse` ("Executions if synchronous execution was requested"), reconciling the live evidence with the schema; `lastShares` is a string, so `num_or_str` handles it.

## Docs cited
- polymarket.us → orders OpenAPI schema (Order.cumQuantity/leavesQuantity/quantity/state, CreateOrderResponse.{id,executions[]}, Execution.lastShares string, OrderState enum incl. ORDER_STATE_FILLED/PARTIALLY_FILLED) → https://docs.polymarket.us/api-reference/oapi-schemas/orders-schema.json
