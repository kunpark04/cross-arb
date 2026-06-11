---
from: D-transport-auth (adversarial engine review)
run_id: 20260611-engine-review
timestamp: 2026-06-11T13:00:00Z
scope: [bot-rs/src/venue.rs (WS streams/reconnect/subscribe), bot-rs/src/auth.rs (RSA-PSS + Ed25519), bot-rs/src/exec.rs (dry-run + live POST)]
ports_consulted: [bot/monitor.py, bot/kalshi_book.py, scripts/probe_kalshi_ws.py, scripts/probe_pmus_ws_auth.py]
prior_review: tasks/_agent_bus/20260611-0940/{code-logic-reviewer,coding-agent}.md (accepted items not re-flagged)
critical: 2
warn: 5
info: 4
clippy: clean for the 3 in-scope modules (5 warnings exist but all in matcher.rs/risk.rs/unwind.rs — out of scope, pre-existing style: map_or→is_some_and, too_many_arguments)
---

Adversarial review of the I/O transport + auth + exec layer. Each finding verified against the actual code (file:line + quote). The prior 0940 self-review's `await_holding_lock` and "mutex never held across .await" claims were independently re-verified and HOLD — see the INFO "sound module" note; the findings below are what 0940 did NOT cover (it reviewed an earlier shape; the `apply_kalshi_sub_update` select-arm, the panic-supervision gap, the pmus-prune reconnect race, and the JSON `format!` bodies are new/unexamined surface).

---

### [CRITICAL] main.rs:221-244 / venue.rs:277,431 — A panicking stream task dies silently → the half-dead collector the reconnect loop is meant to prevent
**Problem:** All four async tasks are spawned fire-and-forget — the `JoinHandle`s are dropped, never joined or `abort`-watched:
```rust
tokio::spawn(venue::kalshi_stream(creds.clone(), k_tracked.clone(), kalshi_books.clone(), tx.clone(), k_subs_rx));
tokio::spawn(venue::pmus_stream(creds.clone(), pm_tracked.clone(), tx.clone(), pm_subs_rx));
```
The supervised reconnect loop inside each stream only covers WS-level events (clean close, read error, seq gap) — it does NOT cover a **panic** of the task itself. And the streams panic readily: every book/tracked access is `.lock().unwrap()` (venue.rs:305, 306, 366, 376, 454). `std::sync::Mutex::lock()` returns `Err` if the mutex is **poisoned** — i.e. any OTHER thread panicked while holding that lock (e.g. the `run_live` loop or `refresh_loop`, both of which also `.lock().unwrap()` the SAME `kalshi_books`/`k_tracked` Arcs). When that happens the stream's `.unwrap()` panics, the task unwinds, and because (a) its `JoinHandle` was dropped and (b) the OTHER stream still holds a `tx` clone keeping `rx` open, the `run_live` `rx.recv()` arm never returns `None` → the main loop runs forever consuming the surviving venue while the dead venue's book is FROZEN. That is exactly the "half-dead collector no alarm catches" the module docstring (venue.rs:13-15) promises to prevent. monitor.py's run loop is single-process and re-raises; the spawned-task port lost the supervision.
**Fix:** Capture the handles and supervise: either (a) wrap each stream body so a panic is caught and the task restarts (e.g. spawn a thin supervisor that `JoinHandle::await`s and respawns on `Err(JoinError)` with backoff), or (b) at minimum `tokio::select!` the main loop over the join handles and treat any task completion/panic as a fatal stop (`break` + non-zero exit) so a dead collector halts trading instead of trading a frozen book. Also prefer `lock().unwrap_or_else(|e| e.into_inner())` (recover the poisoned guard) on the book/tracked locks so one panic doesn't cascade into permanent venue death.

### [CRITICAL] exec.rs:164-177,183-202 — Live order JSON is built with `format!` string-splicing; an unescaped `"` or `\` in market/coid yields a malformed or injected order body
**Problem:** Both payload builders splice untrusted strings straight into a JSON literal with no escaping:
```rust
format!(
  "{{\"action\":\"{}\",\"side\":\"{}\",\"ticker\":\"{}\",\"count\":{},\"type\":\"limit\",\"yes_price\":{},\"client_order_id\":\"{}\"}}",
  action, side, intent.market, intent.qty, intent.price_cents, intent.client_order_id
)
```
`intent.market` is a venue-supplied ticker/slug from `discovery` (the pmus catalog / Kalshi REST — values the bot does not control), and `client_order_id` is `format!("xarb-{}-{}", pair.slug, leg.tag)` (main.rs:747) which embeds that same slug. If any slug/ticker ever contains a `"` or `\` (a backslash, a quote, a control char — entirely possible in a third-party catalog title-derived slug, and certainly possible if discovery is ever widened), the resulting body is either invalid JSON (venue 400s — a silent failed leg, leaving the hedge naked) or, worse, structurally altered (a crafted slug `x","count":9999,"x":"` changes `count`/price). Even absent malice this is a latent malformed-order bug on the real-money path. The dry-run/test slugs are all clean so no test catches it. Note the dry-run path (exec.rs:67) only `println!`s, so the discrepancy is invisible until live.
**Fix:** Build the body with `serde_json` (already a dependency), never `format!`:
```rust
serde_json::json!({"action":action,"side":side,"ticker":intent.market,"count":intent.qty,
                   "type":"limit","yes_price":intent.price_cents,"client_order_id":intent.client_order_id}).to_string()
```
serde escapes quotes/backslashes/control chars correctly. Same for `build_pmus_payload`. (Keeps numbers as numbers, which `json!` does; the current `format!` already emits bare `count`/`yes_price` which is correct, so `json!` is behavior-preserving for clean input and safe for dirty input.)

### [WARN] venue.rs:405-422 / 326 — `apply_kalshi_sub_update` ignores `ws.send` errors AND drops the SubUpdate when sid isn't known yet → an add silently lost until the next reconnect
**Problem:** Two robustness gaps on the in-place add/delete path:
1. `let _ = ws.send(Message::text(frame)).await;` (line 419) — a send error is discarded. If the socket is half-broken, the add/delete is lost but the stream keeps looping on `ws.next()` (which will eventually error and reconnect) — meanwhile the new market is unsubscribed and untraded, with no log.
2. When `sid` is `None` (ack not yet seen), `apply_kalshi_sub_update` returns early (line 410-412) and the `SubUpdate` is **consumed and discarded** (it arrived via `subs.recv()` at line 323, already taken off the channel). The doc claims "the next reconnect subscribes the current tracked set" — which is true ONLY because `refresh_loop` mutated `k_tracked` before sending (main.rs:583-588). But there is no reconnect *forced* here, so until the connection happens to cycle for an unrelated reason, the just-added market never streams. Early in a connection's life (before the first `subscribed` ack) this is a real, if narrow, window where discovery adds vanish.
**Fix:** (1) Log on send failure and set a flag to break→reconnect (a failed control send means the socket is suspect). (2) When `sid` is None, don't silently drop — either buffer the pending add and flush it when the ack arrives, or force a reconnect (`break 'read`) so the tracked set (already updated) is re-subscribed promptly, matching monitor.py's "sid not known → cycle" rather than "sid not known → wait indefinitely."

### [WARN] venue.rs:359-361 — sid captured from the FIRST `subscribed`|`ok` ack only; a re-subscribe/second-sid ack can't update it, and a missing `msg.sid` leaves sid `None` forever (no fallback)
**Problem:**
```rust
if sid.is_none() && matches!(typ, "subscribed" | "ok") {
    sid = msg.get("sid").and_then(Value::as_u64);
}
```
If the `subscribed` ack arrives without a parseable `msg.sid` (a shape drift, an `ok` that carries no sid, or sid as a string), `sid` stays `None` for the ENTIRE connection — so `update_subscription` add/delete can never fire (apply_kalshi_sub_update bails at line 410), and every discovery add for that whole connection silently waits for a reconnect (compounds the prior WARN). The probe (probe_kalshi_ws.py:211) reads `sub1["msg"]["sid"]` and aborts if absent — the bot has no equivalent guard, it just degrades silently. Also `sid.is_none()` means a legitimately-changed sid (if the venue ever re-acks) is ignored — minor, but the "first ack wins forever" assumption is unverified for the reconnect-mid-stream case.
**Fix:** If a `subscribed` ack is seen but `sid` is still `None` after parsing, log a WARN (the no-gap add path is now disabled for this connection — operators must know). Optionally accept a string sid (`.as_u64().or_else(|| as_str parse)`). At minimum, surface "running without a captured sid → adds will wait for reconnect" rather than failing mute.

### [WARN] auth.rs:20-26 / venue.rs:243-249 — `now_ms()` / `now_ms_for_sign()` return `0` on a pre-epoch clock instead of failing; a skewed clock silently produces signatures the venue rejects
**Problem:** Both timestamp sources swallow the error:
```rust
SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_millis()).unwrap_or(0)
```
`duration_since` only errs if the system clock is before 1970 (rare but possible on a misconfigured VM / RTC-battery-dead box). On that path the function returns `0`, the canonical string becomes `"0GET/trade-api/ws/v2"`, the signature is valid-but-stale, and BOTH venues reject it (Kalshi ≤30s skew, pmus ≤5s per the auth brief) — every handshake and every order 401s with no indication WHY (the reconnect loop just logs "connect failed" and retries forever against a clock that will never satisfy the skew bound). More broadly: there is no clock-skew preflight. A few-second NTP drift on the droplet silently kills pmus (5s window) while Kalshi (30s) still works — a confusing one-venue outage.
**Fix:** Don't paper over the error — on `Err`, either panic with a clear message (a wrong clock is unrecoverable for signing) or propagate. Add a one-time startup skew check (the venues' acks / a `Date` header vs local time) and log loudly if local clock is >~3s off, since pmus's 5s bound is tight. At minimum log when the timestamp falls back to 0.

### [WARN] exec.rs:289-301,57 — `submit_pair` returns a `PairAck` but the caller's naked-leg handling is a documented stage-2 gap; a one-legged live fill is currently unflattened
**Problem:** The pair-shaped `PairAck{a,b}` correctly preserves both legs' outcomes (good — `join!` not `try_join!`). But the only consumer (main.rs:365) acts on `both_filled()` and otherwise does nothing:
```rust
if ack.both_filled() { ... track_position ... }
// A partial/failed fill is NOT tracked here — naked-leg handling is the leg-fill-timeout path (stage-2 legs.rs)
```
So if leg A fills and leg B is `Rejected`/`RateLimited`, the bot holds a **naked directional position** with NO record (not in `positions`, so the postponement-unwind poll can't see it either) and NO cancel/unwind attempt. `cancel()` is also hardwired to error (exec.rs:302-307, "needs venue order_id (stage-2 leg tracking)"), so even a manual unwind has no path. The whole strategy's core risk (both-legs-don't-offset) has its mitigation stubbed. This can't fire in THIS build (KeysUnavailable), but it is the single highest-consequence open item the instant keys are present — and it's only a code comment, not a gate. The leg_fill_timeout_ms config (500) exists but nothing reads it on this path.
**Fix:** Out of pure transport scope, but flag LOUDLY: before any real-money arm, the `!both_filled()` branch must (a) record the filled leg as a naked position and (b) immediately attempt a marketable unwind of it (the unwind machinery exists — `unwind_orders`/`handle_unwind`). Until then, consider failing closed: if `!both_filled()` and the filled leg is real (not simulated), log CRITICAL and engage the kill-switch rather than continue silently.

### [WARN] exec.rs:222-228,179-202 — pmus POST-body signing is UNVERIFIED and "fails safe" only as a 401; nothing prevents repeated mis-signed live attempts
**Problem:** The pmus order signs only `{ts}POST{path}` and sends the body, with a TODO acknowledging body-inclusion is unconfirmed (auth brief flag). The "fail safe" claim is "if pmus requires the body in the signature this will 401, surfaced as Rejected." That's true for a SINGLE attempt, but: (1) a 401 on leg B after leg A (Kalshi) fills = a naked leg (see prior WARN) every single time a pmus leg is attempted, if body-signing is in fact required; (2) there is no guard that distinguishes "pmus signing unverified → don't attempt a pmus LIVE leg yet" from "attempt and hope" — the build_pmus_payload doc says "treated as needing live verification" but no code enforces it. On the prod path this means the first real pmus arb systematically legs out.
**Fix:** Gate the pmus LIVE leg behind an explicit `PMUS_POST_SIGNING_VERIFIED=yes` env (default off) so the bot refuses to fire a pmus order until the owner has confirmed the scheme on the demo/live endpoint — converting a silent naked-leg-generator into a loud, deliberate unlock. (Kalshi-only weather/econ pairs where BOTH legs are Kalshi+pmus still need this; the safe interim is dry-run until verified, which is the current default — but the gate makes it explicit rather than relying on KeysUnavailable.)

### [INFO] venue.rs:276 + 305-387 — VERIFIED SOUND: no std::sync::Mutex guard is held across any `.await` (the `await_holding_lock` allow is justified)
**Problem (none — confirming the claim):** I independently traced every `books.lock()`/`tracked.lock()` in `kalshi_stream`: line 305 `books.lock().unwrap().clear()` (temporary, dropped at `;`), line 306/454 `tracked.lock().unwrap().iter().cloned().collect()` (temporary, dropped at `;`), lines 366-371 and 376-380 each `drop(bk)` BEFORE the `tx.send`. The only `.await` points (`ws.send`, `ws.next`, `subs.recv`, `apply_kalshi_sub_update`, `sleep`) hold NO lock — `apply_kalshi_sub_update` (line 326) takes no lock at all. So the `#[allow(clippy::await_holding_lock)]` is safe and the deadlock risk the prompt asked about does not exist in the current code. Keep the allow but the comment "held only across in-memory merges, never .await" is accurate. (`tx` is an `UnboundedSender` — `send` is non-blocking/non-async, so even the post-drop sends can't stall.)
**Fix:** None. Noted so a future refactor that moves a lock across an await is caught — consider replacing the file-level `#[allow]` with a tighter scope or a `parking_lot` mutex if the BTreeMap merges ever grow.

### [INFO] venue.rs:135-139 — SeqTracker advances `expected` even on a gap, so a single dropped frame is reported once then RESYNCS to the new stream (matches Python, but means a gap → one cycle, not a storm)
**Problem (clarification, not a bug):** `check` sets `self.expected = Some(seq + 1)` unconditionally, so after a gap at seq S it expects S+1 and the NEXT frame passes. This MATCHES kalshi_book.py:73-76 and the test (venue.rs:611-616). The consequence worth noting: on a gap the stream emits ONE `SeqGap`, sets `clean=false`, and `break`s to reconnect (line 349-354) — so the resync-in-tracker never actually matters live (the connection is torn down on the first gap). The behavior is correct; just confirming the gap → single-cycle semantics (no duplicate frames replayed, book cleared on reconnect at line 305) are intact.
**Fix:** None. Optionally drop the resync line since the connection always cycles on gap (it's dead code live), but harmless and keeps parity with the Python unit test.

### [INFO] exec.rs:269-285 — `run_pair` current-thread fallback is unreachable under `#[tokio::main]` (features=["full"] → multi-thread); `block_in_place` is correct there
**Problem (confirming the design):** `Handle::try_current()` succeeds inside `run_live` (called from `#[tokio::main]`, which with `tokio features=["full"]` is the multi-thread runtime — Cargo.toml:16), so `block_in_place(|| handle.block_on(fut))` is taken and is VALID (block_in_place requires multi-thread, which holds). The `new_current_thread` fallback (line 277) is only hit in unit tests (no ambient runtime) — correct. The one latent footgun: if anyone ever switches to `#[tokio::main(flavor = "current_thread")]`, `block_in_place` will PANIC at runtime ("can call blocking only when running on the multi-threaded runtime"), turning every live submit into a panic. Not a bug today; a tripwire for a future change.
**Fix:** None required. Optionally add a debug_assert or a comment at the `#[tokio::main]` attribute (main.rs:29) that the multi-thread flavor is load-bearing for `exec::run_pair::block_in_place`.

### [INFO] venue.rs:333-336,485-488 — Binary frames are lossily UTF-8-decoded; a non-text data frame would be silently corrupted rather than skipped
**Problem:** `Ok(Message::Binary(b)) => String::from_utf8_lossy(&b).into_owned()` then JSON-parsed. Both venues serve TEXT JSON over WS (verified by the probes, which only ever `recv()` text), so this branch shouldn't fire — but IF a venue ever sent a binary control/compressed frame, lossy decoding could produce a string that either fails JSON parse (silently `continue`d at line 346/496) or, in a pathological case, parses to something unintended. Low risk (no evidence either venue sends binary), but it's an untested path treated as if it were text.
**Fix:** None urgent. Consider logging an unexpected Binary frame (it would signal a protocol assumption broke) rather than transparently treating it as text.

---

## Summary
- **2 CRITICAL:** (1) spawned stream tasks are unsupervised — a lock-poison/panic kills a collector silently while the loop keeps trading a frozen book (the exact half-dead failure the reconnect loop claims to prevent; reconnect covers WS events, not task panics); (2) live order bodies built via `format!` string-splicing with no JSON escaping — a `"`/`\` in a venue-supplied slug/ticker yields a malformed-or-injected order (latent naked leg / wrong count), invisible because dry-run only prints and test slugs are clean.
- **Top WARNs:** in-place Kalshi adds are silently lost when `sid` is unknown or `ws.send` errs (no force-reconnect, no log); `now_ms` returns 0 on a bad clock → silent universal 401 with no skew preflight (pmus's 5s bound is tight); naked-leg handling on a one-legged live fill is a stubbed comment, not a gate (highest-consequence open item once keys exist); pmus POST-body signing unverified with no explicit gate (systematic pmus leg-out if body-signing is actually required).
- **Sound (verified, not just trusted):** mutex-never-across-await holds (traced all 4 locks; `await_holding_lock` allow justified); `join!`-not-`try_join!` preserves both legs; seq-gap → single cycle with book-clear-on-reconnect; pmus prune→reconnect is consistent (pm_tracked pruned before re-subscribe); RSA-PSS salt=32 + Ed25519 canonical string match the Python byte-for-byte; signed path == request path for both demo/prod.
- **clippy:** clean for venue.rs / auth.rs / exec.rs (the 5 warnings are all pre-existing style nits in matcher/risk/unwind, out of scope).
