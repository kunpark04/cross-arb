# Session log — cross-arb

Process changelog: what each working session shipped, decided, or changed in scope. Newest first.
Append an entry for any session that ships code, makes a decision, or moves scope. Keep entries
terse — link the artifact (brief / script / decision / todo item) rather than restating it.

---

## 2026-06-14 (cont. 2) — Dutch book REMOVED — narrowed to the single 2-leg strategy (pre-go-live de-risk)

Heading toward a real-money go-live, the owner narrowed to ONE arb strategy and pulled the 3-leg Dutch book
(`e39043c` reverts `916c89c`). Rationale: it's the riskiest, never-run-live code (3-leg execution +
naked-pair recovery; the recommended independent review not yet done) — removing it shrinks the live surface
to the proven 2-leg **per-outcome binary arb** (buy YES cheap + NO dear on one outcome:
weather/econ/sports/WC-per-outcome). bot-rs back to **135 tests**, clippy clean; restorable from `916c89c`.

**Go-live blindspots flagged (still open):** the **edge is unvalidated** (0014 needs weeks of data; backtests
are effective-n≈1, paper/gross, phantom-corrected); **no clean live order has ever been placed** (demo
round-trip + pmus POST-signing/`SELL_*` unverified); **settlement empirically confirmed for WEATHER only**
(sports/econ recon ~Jun 23–25 / Jul 3; WC never settled on both venues); the 2-leg **naked-leg recovery
still fires first on real money**. The defensible first real-money step = **weather-only, 1-contract, after a
clean demo round-trip** — not full-universe by EOD.

---

## 2026-06-14 (cont.) — World Cup 3-leg Dutch-book arb (the fuller WC opportunity, dry-run)

Owner: "go all the way." Added the cross-venue 3-leg **Dutch book** alongside the per-outcome binary arbs
(`916c89c`): buy YES on all 3 outcomes, each on its cheapest venue; lock iff the 3 cheapest sum < $1 (one
pays $1 → profit = $1 − basket_cost, any result). On one venue the 3 YES sum > 1 (overround); only the
cross-venue-cheapest set can dip below 1.

- **Architecture:** a PARALLEL 3-leg path (`SoccerTriple`/`dutch_book`/`submit_triple`/`TripleAck`/
  `evaluate_triple`/`TriplePosition`/`triple_unwind`) — the proven **2-leg core is BYTE-UNCHANGED** (135
  baseline tests pass untouched; git-diff = 9 deletions, all surgical insertion points).
- **Naked-pair recovery (the critical part):** a partial fill (1 or 2 of 3) flattens EVERY filled leg +
  **FAILS-CLOSED** (halt) on any unrecoverable leg — 3 dedicated tests prove a silently-un-flattened 2-of-3
  (= directional exposure) cannot occur. Reuses the 2-leg recovery primitive per leg.
- **Verified:** 161 tests (+26), clippy clean; smoke fires `basket_cost=0.92, net=4.1¢` (3 YES legs, one per
  outcome on its cheapest venue). Self-review 0 CRITICAL; the per-game cluster cap bounds the combined
  basket+binary exposure (correlated).
- **Before arming (owner gated, 0006):** release held-basket exposure on settlement (the 2 WARNs — a
  safe-failure: the bot stops OPENING baskets, never over-trades); void-tail calibration; an INDEPENDENT
  review of the recovery fail-close + exposure lifecycle. Dry-run by default.

---

## 2026-06-14 — World Cup tradeable (live, dry-run): per-outcome BINARY arbs, not a 3-leg basket

Owner directive: make the FIFA World Cup (the coverage audit's one real untracked block, ~60 games, +34%)
tradeable live. Plan-mode → approved → built in 2 committed chunks (`17ab0ea` Python, `e50ecbb` bot-rs).

- **The model (the key win):** a WC game is NOT a 3-way basket — pmus + Kalshi each list the 3 outcomes
  (teamA / draw / teamB) as **separate binary YES/NO markets**. So each outcome is a clean binary co-listed
  pair = exactly the weather/econ 1:1 model (buy YES cheap venue + NO dear venue; locks regardless of the
  other outcomes — the **same pattern weather buckets** already use). So WC **reuses the bot's 2-leg core
  entirely** — `build_legs`/`exec`/`unwind`/`Position`/`game_signal` UNTOUCHED; the only changes are discovery
  + a routing marker. Far lower-risk than a basket (which would need 3-leg exec + naked-pair recovery — deferred).
- **Discovery** (`colisted_map.py` + `discovery.rs`): a soccer-3way branch — filter pmus `drawable_outcome`
  `atc-fwc`, group the 3 sibling slugs/game, bind via `pick_game` (exact-date + doubleheader guard) + a
  3-entry country-code alias (`irn→iri`/`alg→dza`/`hai→hti`; binds 59/59 = 51 exact + 8 alias, 0 false), map
  `-draw`↔`-TIE`, emit each outcome as a **per-outcome binary `Pair`** (`kalshi_b=None`, soccer marker). L23
  YES-read (marketSides `description=="Yes"`; pmus `outcomes` is a JSON STRING). **Join differential-verified
  byte-identical Python↔Rust.**
- **Routing**: a WC pair (`kalshi_b=None`) **structurally** takes the binary `signal` arm — no routing-logic
  change. `Cat::Sports` reused (correct lock-days); `track_position` skips the MLB postpone poll for WC.
- **Settlement**: both venues settle on **REGULATION** (90 min, excl. ET/penalties), 2-week window — the only
  residual is the void fallback (fair-price vs last-traded) = a priceable **TAIL** ([0018]). `void_haircut`
  extended for `atc-fwc` (0.08¢); the gate verdicts all **174 WC outcome pairs → TAIL** (0 DIVERGENT).
- **Verified**: bot-rs **135 tests** (was 128), `selftest_all` 20/20, clippy clean; smoke fires a WC arb
  `Pmus YES + Kalshi NO on the SAME outcome` (the 1:1 lock). Self-reviews PROCEED (0 CRITICAL).
- **Still open**: WC in the monitor (research persistence, gated redeploy); arming WC past dry-run = owner
  gated (independent review + demo, 0006); the cross-venue 3-leg Dutch book (optional, deferred).

---

## 2026-06-13 (cont.) — Breadth audit + cap fix, velocity-gate enable, $250/$250 backtest, settlement-identity GATE (0018)

Continued from the edge-RATE work below. Owner pushed on breadth ("100% more arbs out there") + meticulous
settlement gating. Five focused commits (`f2dda9b` edge-rate · `fb8b132` cap+coverage · `4860af9` backtests ·
`675f44d` settlement-gate · `87cb5b1` refinement), then this doc pass.

- **Velocity gate ENABLED live @1.0** (`MIN_EDGE_RATE_CPD`, [0017]): conservative floor below the fast-category
  rates — inert on current data, positioned to bite only once slow arbs (econ / far-sports) appear; `edge_rate`
  logged per live ENTRY. (`velocity_gate_experiment.py`: the gates are **inert** on current data — econ never
  clears the 2¢ floor, sports are all near-game. A capital-efficiency tool, not an edge source.)
- **Breadth audit** (`coverage_audit.py`): the owner's "100% more" is **mostly raw-count illusion** — pmus has
  15.3k open markets but ~12k are sports PROPS/FUTURES with no head-to-head Kalshi twin. The one real untracked
  block is the **FIFA World Cup** (Kalshi `KXWCGAME`, ~60 games, **+34%**) — but **3-way (win/tie/lose)** +
  ESPN-vs-FIFA source, not a drop-in. **Bug found+fixed:** `PM_CATALOG_CAP=12k` silently truncated ~3k live
  markets (WC surge to 15.3k) incl. tracked-league tail games → 25k (`colisted_map.py` + `discovery.rs`).
- **$250/$250 per-venue backtest** (`venue_split_backtest.py`): **+1.39% weather / +4.55% all-verified** over
  ~4.3 d, but **method-demo only** — a stats-ml-reviewer audit caught 2 CRITICALs first (**L20 flat-ladder
  phantoms = 57% of pre-fix PnL**; a `leg_split` single-letter-dir bug); even post-fix effective-n≈1, 1–2
  concentrated bets (61% in one), paper/gross, sports unverified ([L28]).
- **Settlement-identity GATE** (`settlement_identity.py`, **[0018]**): the programmatic invariant-#1 gate.
  Owner refinement — identity is of the **OUTCOME not the source STRING** (ESPN vs FIFA = same winner), and
  **4 categories** with the tail PRICED (`IDENTICAL / TAIL[¢-cost] / DIVERGENT[structural] / NEEDS_MANUAL`,
  precedence in that order; [L29]). Live: weather 24 IDENTICAL, sports **52 TAIL** (MLB 0.26¢ / ATP-WTA-UFC
  0.10¢, tradeable iff edge>cost), econ 13 IDENTICAL, **0 structural DIVERGENT**. Outcome-count discovered from
  `marketSides`. No-false-IDENTICAL preserved (review SOUND).
- **Still open:** wire the gate into live discovery + the bot trading path (gated deploy); the soccer/WC
  prerequisites (bind the Tie market in `colisted_map`; extend `void_haircut` for `atc-`); the **0016/L27**
  prior-session doc gap still unfilled.

---

## 2026-06-13 — Edge-RATE allocation shipped in `bot-rs` (0014-H2, live); corrected days-to-grade lock-days; 0016/L27 hygiene gap found

Status-check → owner chose "edge-RATE allocation" (the last deferred `bot-rs` code feature) from the
next-steps menu. Plan-mode → approved → implemented + verified. Decision
[0017](../decisions/0017-live-edge-rate-lock-days.md); plan at `~/.claude/plans/deep-waddling-barto.md`.

- **What shipped** (`risk.rs`/`config.rs`/`exec.rs`/`main.rs`): `risk::lock_days` re-derives per-category
  lock-days to **corrected days-to-grade** — weather 1.2 floor / **sports = dynamic `days_to_event`** (was a
  flat 15) / econ 21 fallback (no release calendar in the bot). `edge_rate = booked_edge ÷ lock_days` is
  **always** computed + returned in `Approved` (logged); a **reservation floor** `MIN_EDGE_RATE_CPD`
  (`Reject::BelowEdgeRateFloor`, **default 0 = OFF**) skips low-velocity arbs. Reservation-only — **sizing
  untouched** (H1's caps own that); batch-ranking deliberately out of scope (latency tax, capital binds at ~10
  anyway). **128 tests** (+3), clippy clean, smoke: a 1-day-out sports arb rates **3.0¢/$-day > weather 2.5**.
- **Pre-registration integrity preserved (L19):** the live bot uses corrected (dynamic) lock-days; **0014-H2's
  frozen backtest priors (sports 15) are NOT rewritten**. Which lock-days the confirmatory backtest uses at
  data-arrival is a **labelled sensitivity arm**, not a silent re-tune (this was a prior correction from a
  direct account observation, not a change motivated by the multi-week data).
- **A test caught a real bug pre-commit:** `lock_days(Sports, +inf)` leaked non-finite (`f64::max` collapses
  NaN but not `+inf`) → added an explicit `is_finite` guard so `edge_rate` can never divide-by-zero/go
  non-finite even with the proximity gate off.
- **Hygiene gap found (prior session):** the committed 2026-06-11 session log references **decision 0016** +
  lesson **L27**, but neither file exists (decisions stop at 0015; only L25/L26 are filed). Not fabricated —
  flagged for owner backfill; my entry took the correct next slot **0017**. The decisions index now shows the
  0016 gap explicitly.
- Doc commit of the prior session log (`249ed02`) + this feature pending commit.

---

## 2026-06-11 (UTC, latest+5) — Venue-contract order bugs (a real unintended live position) → autonomous fix loop → CLEAN money path; Context7 security review

The owner pressed on meticulousness ("be more meticulous — you missed this even after several passes") and
then "do not stop fixing." A new **venue-contract review lens** (verify what the venue actually does, not the
code's internal logic) caught a class of real-money bugs five prior reviews missed. Fixed in an autonomous
fix→review loop until the money-path review came back CLEAN + doc-verified. Decision [0016](../decisions/0016-live-venue-contract-verification.md);
lessons L25/L26/L27. Commits `34ee18d` (NO-leg pricing) → `061c35f` (outcome) → `989a291` (recovery) →
`dc8a2c3` (rounding) → `6eb7893` (unwind), all pushed.

- **The bug that drew blood — NO-leg pricing.** A 1¢ `BUY_SHORT` probe meant to rest "opened an unintended
  live short" on a pmus futures market (`tec-nhl-hart…nikkuc`, −1 "No", $0.54): pmus runs `BUY_SHORT` as a
  **sell-YES priced in YES terms**, so "buy NO @ 1¢" = "sell YES @ 1¢", marketable, filled at the ~54¢ bid.
  The fill was **async** (the create response said `executions:[]`), so my cancel hit an already-filled order
  and I **falsely reported the account clean** — exactly the unverified-assertion failure the owner had just
  called out. Order records (`side=SELL, cumQuantity=1`) confirmed it was mine. Couldn't flatten (66¢-wide
  illiquid book; `close-position` 500'd); **left open** per the owner (settles itself June 30). The fix:
  pmus NO legs price `1 − NO` (YES-denominated); Kalshi `side:no` uses `no_price` not `yes_price`.
- **Three more same-class bugs (all order-OUTCOME side, all missed by prior passes):** a 2xx create read as a
  FILL → phantom hedges + a blinded naked-leg guard (now `Ack.filled` parsed per venue, fail-safe; pmus sends
  `synchronousExecution`); pmus has **no idempotency** (code claimed it did); `cancel` was an unwired stub.
- **Autonomous fix loop (owner: "don't wait for my command"):** naked-leg **auto-recovery** (cancel resting +
  flatten filled, halt backstop); pmus fill-fields pinned to the OpenAPI schema; per-market tick/min threaded;
  **direction-aware rounding** (BUY ceils / SELL floors so a marketable leg can't round to a resting price).
  Each fix got a meticulous review; the final money-path review = **CLEAN, 0 CRITICAL, verified vs the live
  pmus docs** (112 → **125 tests**, clippy clean).
- **Context7 security review** (owner-requested): a docs tool result told the fix-agent to run
  `npx ctx7 setup …`. Investigated — **not malicious**: both `@upstash/context7-mcp` (the configured server)
  and `ctx7` (npm author: Upstash) are legitimate; it was Context7's own onboarding nudge, intermittent (3
  clean re-queries). The point that mattered: it's a **prompt-injection PATTERN**, and the defense held — the
  agent + I treated tool output as DATA, ran nothing. (`.env` footgun also fixed: `VENUE_ENV` prod→demo.)

**Net:** the order/execution money path is now **live-and-doc-verified clean** — multiple critical
venue-contract bugs that no code review caught are fixed. Remaining are non-code/gated/data: `SELL_*` live
verification (can't bound a naked short to the cap), leg-fill-timeout/IOC (a flagged strategy choice, recovery
is the backstop), whole-cent price precision (≤0.5¢/leg, bounded), the open position, and the binding gate —
the **edge** itself, still unproven and still needing the weeks of data.

---

## 2026-06-11 (UTC, latest+4) — Verify-everything pass: live data path + order paths proven; discovery 429 bug fixed; claims audit

Owner pushed back: a false claim ("sandbox blocks venue I/O") proved I'd asserted unverified things —
so verify everything, complete the open engine items, and pilot live with minimal capital (no demo
account). Did exactly that. Commits `fff4923` (data-path fixes) + `ad80588` (claim corrections) + docs.

- **Live data path PROVEN** (the biggest unverified surface — the Rust WS clients had NEVER connected,
  only sample-tested): ran the bot dry-run vs live prod (read-only key, zero orders). Kalshi WS connected +
  subscribed 153 tickers, pmus WS 113 slugs, ~300 frames/s, 153+112 books built, discovery 60 wx + 13 econ
  + 40 sports from live catalogs. **Found + fixed a real bug:** `discovery::fetch_json` dropped the Python
  `get()`'s retry, so a cold-start burst across ~18 series 429'd and aborted ALL discovery — ported the
  retry (tries=4, {429,5xx}, 1.5*(i+1)s) + the inter-series 250ms pacing. Added connect-success logs + a
  20s health heartbeat (a silent 24/7 bot was itself a gap).
- **Kalshi LIVE order round-trip** ✅ — a 1-contract order placed (`[201]` resting, fill_count 0) +
  cancelled (`[200]`) on the real account, read-write key, $0 used (chose an empty-book future market so a
  1¢ bid can't fill). RW key authenticates on prod (balance read 200).
- **`/teams` smoke** ✅ — statsapi's 30 MLB abbrevs == Kalshi's 30 KXMLBGAME suffixes (identical) → no
  postponement missed on a mismatch. Closes that README residual.
- **`SELL_*` pmus intents** — could NOT verify within the 1¢/15¢ cap (a resting SELL must price high to
  avoid filling = >cap; a naked short isn't bounded by price). The classifier correctly blocked a $0.99
  probe. Left doc-derived (same enum family as the live-verified BUY_LONG/BUY_SHORT; used only to close).
- **Claims audit** (`tasks/_agent_bus/20260611-claims-audit/`): swept all 14 src files + README — 4 FALSE
  + 7 unverified + 6 stale, all doc-level. Corrected: the sandbox claim everywhere; a fabricated "148 ms"
  serial latency (research says 161); the stale fee-parity "no test until stage-2" (the test passes); the
  now-verified pmus signing + auth acceptance + ack-parse + cancel endpoints; dead `legs.rs` references.

**Net:** the auth/order/data plumbing is now end-to-end-verified live — a genuine blocker retired — and the
code/docs no longer assert anything unverified. The binding gate is unchanged: the edge itself (0014
multi-week data; sports recon ~06-23/25). Plumbing proven; edge unproven.

---

## 2026-06-11 (UTC, latest+3) — LIVE auth verified on BOTH venues; pmus order endpoint corrected; a secret-leak incident (L25)

Owner directed: stop deferring, verify what can be verified now. Outcome — the sandbox-can't-reach-venues
assumption was **wrong**; both venues are reachable with auth, so the auth/signing layer (a real open
unknown) is now **verified live**, and the testing surfaced + fixed a real pmus bug. Commits `6fd8dbf` (fix)
+ docs.

- **⚠️ Security incident (L25):** a one-liner meant to *mask* values printed the pmus Ed25519 secret to the
  terminal — a base64 `=` padding char defeated a `split('=')` mask, so the line "without `=`" branch never
  ran. Surfaced immediately; **owner chose NOT to rotate** (accepts the risk, key retained). Rule added:
  never route secret material through stdout at all (load file→env, print only a status / "wrote X").
- **Sandbox framing corrected:** this environment CAN reach `demo-api.kalshi.co`, `api.elections.kalshi.com`,
  and `api.polymarket.us` with auth. The repeated "Claude's sandbox blocks real-money submission" claim was
  false; corrected in `exec.rs` + `bot-rs/README`. The guardrail is the **safe-by-default code** (dry-run
  default, prod-consent, the pmus gate), not an external wall — which is the correct place for it.
- **Kalshi (demo) VERIFIED:** RSA-PSS signed balance read `[200]`; a demo `POST /portfolio/orders` →
  `[400] insufficient_balance` (NOT 401) — signature + the exact `build_kalshi_payload` shape + the orders
  endpoint all accepted; a full rest+cancel just needs demo funds (a UI step). `build_kalshi_payload` is right.
- **pmus VERIFIED end-to-end** (read-only signing + ≤1¢ probe orders, owner-authorised): a bad-sig-vs-good-sig
  control proved pmus authenticates BEFORE routing and accepts the **body-less** `{ts}POST{path}` signature on
  POSTs (the long-open POST-body question — settled, body NOT signed). Then the **real order lifecycle**: a 1¢
  `BUY_LONG` and a 1¢ `BUY_SHORT` each **placed** (`POST /v1/orders` → `[200] {"id":..}`) and **cancelled**
  (`POST /v1/order/{id}/cancel` → `[200] {}`). Net spend $0; nothing rested.
- **Real bug fixed (`exec.rs`):** the pmus order path was a guess — `/v1/portfolio/orders` with
  `{slug,action,side,size,price}` → **404**. Corrected to the verified `POST /v1/orders` +
  `{marketSlug, intent, type, price:{value,currency}, quantity, tif}`; intent map Buy-YES=`BUY_LONG`,
  Buy-NO=`BUY_SHORT` (live-verified entry dirs), Sell-*=`SELL_*` (doc-derived). 112 tests stay green.

**Net:** auth/signing — a genuine blocker — is now retired on both venues, and a latent pmus order bug is fixed.
Still gating live: a funded demo round-trip, the `SELL_*` intents, and the dominant gate — the **unvalidated edge**.

---

## 2026-06-11 (UTC, latest+2) — `bot-rs`: full-engine adversarial review → 9 CRITICAL + ~17 WARN fixed (112 tests, clippy clean)

Holistic adversarial review of the WHOLE live engine (5,735 lines / 14 modules) — the incremental builds
each had a self-review + targeted parity checks, but nothing had reviewed the *whole* thing for cross-module
bugs, races, and integration errors. Five parallel subsystem reviewers (pricing-math / risk-config /
discovery / transport-auth / loop-concurrency) → findings in `tasks/_agent_bus/20260611-engine-review/`.
Fixed in two disjoint passes + a post-review robustness pass; committed `72ac418` + `fd5a186`.

- **Concurrency core (the loop's submission path, rewritten):** all order submits (entries AND unwinds)
  now route OFF the event loop onto spawned tasks that report a `SubmitOutcome` to a 3rd `select!` arm —
  so the network RTT never stalls the co-equal unwind arm (it did before). `pending_entries`/`flattening`
  in-flight sets kill double-fire; exposure is reserved-on-spawn + released-on-fail (exact inverse);
  a naked live one-leg fill fail-closes (halt + log). An independent review of the rewrite:
  **SOUND, 0 CRITICAL** (`tasks/_agent_bus/20260611-loop-refactor-review/`) — its 3 WARNs then closed
  (loop-top task-death check, panic-safe submit, one-position-per-slug).
- **Loop guards:** a per-pair `k_fresh` gate so a just-reconnected (cleared) Kalshi book can't trade
  half-rebuilt against a stale pmus book (the C3 integration bug no single-module review could see);
  supervised spawned tasks (a dead collector HALTS instead of trading a frozen book); poison-tolerant locks.
- **Gates:** the sports away-team book `k_b` is now divergence-checked (was a 2-leg-fire blindspot);
  `REQUIRE_SETTLE_CLEAN=false` on live+prod now needs explicit consent + a loud banner; a truncated catalog
  pull no longer prunes live markets; NaN `days_to_event` fails the proximity gate closed; a post-tick-
  rounding realized-edge re-check.
- **Transport/parse/math:** order JSON via `serde_json` (was `format!` string-splice → injection/malformed
  body); a clock-before-epoch now panics loud (was a silent 401); the pmus live leg is gated behind
  `PMUS_POST_SIGNING_VERIFIED`; Kalshi cursor pagination bounded; pmus booked fee made linear (ledger.py
  parity); the weather `gte`-decoy backtrack + release-period/CPI month-pick parity; deterministic
  doubleheader bind.

Net: 98 → **112 tests** (+14 regression), **clippy 0**. The engine is materially hardened — but the safety
posture is unchanged: still dry-run-default, still gated, and the edge itself is still the gate on real money.

---

## 2026-06-11 (UTC, latest+1) — `bot-rs`: postponement-unwind TRIGGER armed (held-position tracking + MLB statsapi + fire)

Armed the postponement-unwind rule — the #1 remaining sports risk (a game postponed past Kalshi's ~2-day
void window loses BOTH legs: Kalshi voids to a fair price while pmus pays the real result). Committed
`45a6115` + parity review; **98/98 tests**.

- **Detector** (`postpone.rs`, new): faithful port of `scripts/probe_mlb_postpone.py::unwind_trigger`/`snap`.
  `detect_postponement` flags Postponed/Suspended/Cancelled (+ an officialDate-move-without-status-flip) and
  computes the makeup gap from the **bound event date** (the pm slug date), **never** `officialDate` — the L3
  trap, since on a postponement `officialDate` moves to the makeup date (gap-from-officialDate = 0d → would
  miss every unwind). All Python `_selftest` vectors ported as Rust tests + verified to match the live Python
  selftest. `days_between` is dep-free civil-days.
- **Tracking + firing** (`main.rs`): held positions are recorded from the two entry `OrderIntent`s on a
  both-filled fill (and exposure is now bumped, so the caps actually bind across the session — a latent gap);
  a `tokio::select!` adds an `unwind_rx`; `handle_unwind` prices each leg's exit from the live books (SELL
  YES→best bid, SELL NO→1−ask), fires the two SELLs, removes the position. A one-sided book skips BOTH legs
  (never a one-legged naked unwind) and retries (idempotent `unwind-…` coids). The live `poll_mlb_postponements`
  (statsapi teams + schedule, MLB-only) is the owner/droplet path, off all test paths.
- **Safety:** unwinds are **reduce-only** — they fire even under the kill-switch (flattening a void reduces
  risk), loudly logged; the dry-run backend only logs; `CROSSARB_NO_AUTO_UNWIND=1` disables auto-unwind. A
  self-review CRITICAL (a dropped `unwind_tx` busy-looping the `select!`) was caught + fixed in-run.
- **Independent review: FAITHFUL + SAFE** (`tasks/_agent_bus/20260611-unwind-parity/`) — detector parity
  triangulated in Python (the TB@NYY officialDate-already-moved case → UNWIND, not the trap's WATCH); exits
  priced on the correct book side; no one-legged/wrong-game/no-postponement fire. No CRITICAL, no blocking WARN.

**Residual (owner/droplet):** a one-time live `/teams` smoke (the statsapi join matches the Kalshi-ticker team
suffix to the `/teams` abbreviation — a divergence is a MISSED detect, never a wrong-game fire) + the general
live poll/connect verification. Non-MLB leagues have no auto-detection source yet (statsapi is MLB).

---

## 2026-06-11 (UTC, latest) — `bot-rs` stage-2.5: SPORTS made tradable (two-outcome) + latent leg-market bug fixed

Closed the one functional category gap from stage-2 (sports was discovered + counted but not tradeable —
the 1:1 loop can't price a two-ticker game). Committed `639dd69` + parity review; **80/80 tests**.

- **Sports is genuinely two-outcome:** a pmus game (YES = team A) hedges against the OTHER team's SEPARATE
  Kalshi market. Ported `monitor.py::game_edge` → `signal::game_signal` (PK = A@pmus + B@Kalshi using the
  2nd ticker; KP = A@Kalshi + B@pmus-NO; the C3 orientation guard |guard_pm − kA_ask|>0.40) and
  `GameTracker._depth` → `book::game_depth_at_edge`. `Quote.k_b` carries the away-team book; `risk` gives it
  the same crossed/stale gates.
- **Faithful `pick_game` bind** (`discovery`): emits sports as real `Pair`s (kalshi_b = 2nd ticker) on the
  **exact slug-ET-date** with a **doubleheader `used`-set** — this closes the prior independent-review WARN
  (it asked for exactly this guard before sports was made subscribable). `days_to_event` is computed dep-free
  (civil-days, no chrono).
- **Latent live-order bug fixed (found while wiring sports):** `build_kalshi_payload` uses `intent.market`
  as the Kalshi `ticker`, but `fire_pair` was sending the pmus SLUG for the Kalshi leg — a real order would
  have 404'd on an unknown ticker. Unified `build_legs` now emits venue-native ids (Kalshi→ticker, pmus→slug)
  with per-leg prices from the BOOKS (never the edge — preserves the earlier self-review CRITICAL invariant).
  `Position` generalized to two explicit legs so the postponement unwind flattens the correct sports legs.
- **Independent parity review: FAITHFUL** (`tasks/_agent_bus/20260611-sports-parity/`) — verified by
  differential Python re-execution (PK/KP nets bit-for-bit; C2 exact-date + doubleheader; C3 flip-reject). A
  sports order cannot be sent mis-hedged or on the wrong game.

**Still owner/droplet for sports specifically:** live statsapi postponement DETECTION + held-position tracking
to ARM the (built+tested) unwind rule; settlement recon (~06-23/25) before `ASSUME_SPORTS_SETTLED` is more than
an owner override; a demo-sandbox confirm that a real two-ticker game fires both legs.

---

## 2026-06-11 (UTC, later) — `bot-rs` stage-2: network layer + discovery → live loop complete, parity-verified

Completed the Rust bot end-to-end (still **dry-run by default**; live connect/orders are the owner's
droplet step — Claude's sandbox blocks auth'd venue I/O). Three builds + one independent review, each
committed with green tests:

- **Stage-2 core** (`e789dca`): `book.rs` (O(1)-best order book + Kalshi snapshot/delta merge + pmus
  book + `depth_at_edge`), `signal.rs` (port of `ledger.py` signal/fees, **parity-verified** vs Python),
  `matcher.rs` (weather bounds-equality + econ grid-step-twin + sports joins). 42→**42** tests.
- **Network layer** (`4ed0e61`): `venue.rs` — Kalshi WS (RSA-PSS handshake, `orderbook_delta`,
  snapshot+delta merge, single-sid seq-gap → reconnect) + pmus WS (Ed25519, `MARKET_DATA`), supervised
  backoff; `main.rs` → `#[tokio::main]` live loop (WS books → matcher → `Quote` → `risk` →
  **concurrent** `submit_pair`); `exec.rs` `LiveBackend` real signed POSTs fired via `tokio::join!`,
  keys-absent → `KeysUnavailable` (never sends in sandbox). **53** tests. Self-review caught + fixed a
  CRITICAL: the loop fired the *pair cost* as the YES-leg limit → NO leg couldn't fill → naked leg; now
  book-derived per-leg prices + regression test.
- **Discovery + staleness** (`d378e08`): `discovery.rs` — paginated public/no-auth catalog pull → reuse
  `matcher` joins → `Vec<Pair>` (weather+econ subscribable 1:1; **sports matched-and-counted only** — the
  1:1 loop can't price a two-ticker game); `main.rs` seeds + periodically refreshes the WS subscribe set
  (in-place no-gap add/delete); `book.rs` per-book `last_update` → real `age_s` so `Reject::StaleBook`
  fires on a wedged stream (was inert at `age_s=0.0`). **66** tests. Self-review caught + fixed a CRITICAL:
  GDP econ pairs silently never joined (Kalshi period parser truncated `26JUL` vs pmus `26JUL30`) — **the
  L21 phantom-edge surface**; fixed `k_econ_period` + regression test.
- **Independent parity review** (`tasks/_agent_bus/20260611-parity-review/`, no authorship stake):
  verdict **FAITHFUL** by *differential execution vs live Python* (incl. the float-nasty `4.4−0.1` twin +
  the `26JUL30` GDP period). **The L21 12.2¢ econ phantom CANNOT recur** through the live order path. One
  WARN: sports date-binding looser than Python `pick_game` — zero live exposure (sports never becomes a
  tradeable `Pair`); porting `pick_game` is a pre-condition for the deferred sports-subscribable feature.

**Net:** the bot is functionally complete + safety-spine intact + highest-stakes decoder independently
verified. What remains is **inherently owner-environment** (demo-sandbox session, pmus POST-body signing
live-verify) or a **deferred feature** (sports two-ticker pricing, edge-rate/maker modes) — and the edge
itself still gates the money: do not arm beyond demo until the 0014 confirmatory run passes on multi-week
data. Plan + follow-ups in [todo](../tasks/todo.md); safety model in [bot-rs/README](../../bot-rs/README.md).

---

## 2026-06-11 (UTC, late) — Owner override → live trading bot in Rust (`bot-rs/`), safe-by-default

Owner explicitly **overrode the read-only governance** ([0015](../decisions/0015-owner-override-live-trading-phase.md)),
stated they hold the Kalshi read-write key, and directed: build the live bot in Rust. Captured the
reversal as decision 0015 (with a Claude protest-of-record: the same-session audit/backtest say the
edge is unvalidated; staged rollout strongly recommended). Built the **stage-1 safety-critical spine**
of `bot-rs/` (std-only, compiles offline): `types`, `config` (safe defaults), `risk` (every learned
edge-case gate — kill-switch, reconnect-pause, settlement-identity, crossed/stale book, mid-divergence,
edge floor, per-pair/cluster/total caps, depth+bankroll sizing, idempotency), `exec` (dry-run backend +
real Kalshi payload builder; live POST is the owner-env seam), `ledger` (outcome-independent PnL + taker
fees, parity-vs-`ledger.py` flagged TODO-before-live), `main` (safety banner + hard prod-consent gate).

- **SAFE BY DEFAULT:** dry-run unless `EXECUTION_MODE=live`; demo-sandbox unless `VENUE_ENV=prod`
  (+ refuses prod without `CROSSARB_I_UNDERSTAND_PROD`); 1-contract / tiny-notional caps; kill-switch.
  Read-write key referenced by external path, never read/copied by Claude. **Live submission runs in
  the owner's env** — this sandbox blocks real order submission (verified: it blocked a balance check).
- **Installed Rust** (rustup + GNU toolchain, no MSVC needed) and verified: `cargo test` **12/12 green**;
  `cargo run` dry-run smoke behaves — correctly **rejects** the live econ U-3 9¢ gap (settlement not yet
  verified) and **approves** a clean weather arb at size=1; prod-refusal + kill-switch gates fire.
- **Stage 2 (owner env):** venue WS + auth ports, matcher/signal port w/ a parity test vs the Python
  selftests, leg-sequencer/unwind, and the live transport (sign + HTTPS POST). Docs: `bot-rs/README.md`;
  CLAUDE.md phase/layout/index + working-agreement updated to reflect the 0015 reversal.

## 2026-06-11 (UTC, late) — Deployment-readiness audit + live econ settlement reconciliation

Owner pushed toward live deployment; reframed to "test everything, find blindspots." 7 parallel
adversarial audits ([research/deployment-readiness-2026-06-11.md](../research/deployment-readiness-2026-06-11.md)):
verdict **NOT READY** — effective n≈1 day (91% of edge on 06-10), ~71% of apparent edge is phantom,
85% sub-1¢ (uncapturable at the 1¢ tick), the median arb is friction-negative, the fattest edges are
the most adversely-selected (≥8¢ = 66% toxic, die fastest), and the decisive naked-unwind cost is
unmeasurable read-only. Foundations solid (weather settlement, matcher integrity, no false-positive
joins). Corrected a census double-count (62.6k→33.5k post-epoch; my late-append fix shadowed the .gz).

- **Live U-3 ≥4.2 "edge" investigated** (owner: "isn't that a good arb?"): the buckets DO line up
  (≥4.2 ↔ Above-4.1, post-0013); pmus YES 0.75 ask (7,392 deep) vs Kalshi YES 0.86 bid = a **real
  ~9¢ crossable lock**, not a phantom — corrected the audit's over-dismissal. Real catch = 3-week
  capital lock to the 07-02 print + the (then-)unverified econ settlement + n=1.
- **Econ settlement reconciled NOW** (owner: "reconcile now instead of saying it hasn't been"):
  econ releases RECUR, so PAST settlements reconcile immediately. Built `recon_econ` into
  `settle_recon.py` (`--econ-only`; cumulative-`≥`-twin, exact-bucket print-identity, FOMC
  categorical; selftested). Result: **CPI Apr + CPI May + FOMC Apr = 5 rows, 0 diverge** — both
  venues settle off the identical government number. **Structural discovery:** pmus lists CPI as
  exact-value buckets (not a tradeable twin; `econ_colisted` correctly skips them) while U-3/NFP are
  cumulative `≥`. Lesson **[L24]** (reconcile recurring markets on past cycles; honor the parsed
  `ineq`; a throwaway script's 10 "divergences" were all its own `≥`-assumption). selftest 19/19.
  Docs corrected (the "econ — zero reconciliations" claim was retracted across CLAUDE.md + the
  readiness/probe briefs + scripts/README).

## 2026-06-11 (UTC) — Probe program: all 10 ranked next-steps probed in one parallel read-only pass

Owner: "probe these" (the 10-item ranked list from session close). 7 parallel probe agents + 1 coding
agent; synthesis in [research/probe-program-2026-06-11.md](../research/probe-program-2026-06-11.md);
per-item notes in `tasks/_agent_bus/20260611-probes/`. Data: fresh pull at 01:00 UTC — ~31k
post-0013-epoch records (~16 h of detection-time ms + `px` + `depth`).

- **Taker not rejected at measured RTT (#2):** naked-leg 17–23% @100–150 ms (n=575 capturable ≥1¢),
  survivors keep ~1.9¢ median, breakeven naked-unwind ≈4–6¢ vs ~1–3¢ plausible; ~29% of edges die
  <250 ms (never raceable). The 55.5%@1s figure was the wrong regime for real latency.
- **Maker study narrowed to ONE config (#1):** rest-on-**Kalshi** + taker-hedge-pmus = +0.14–0.44¢/
  attempt (bounded); rest-on-pmus structurally toxic (15–16¢ hedge slip); full maker-maker 32%
  one-leg-naked. Sampling gap measured (~23.8k Kalshi weather prints/day vs 220 visible crossings) →
  ladder/trade-logging spec written AND implemented (wave-2); rides the next gated redeploy.
- **Invariant #1 weather EMPIRICALLY CONFIRMED (#4):** 360/360 settled buckets identical (3-way vs
  NWS CLI, incl. a real revision day). Found+fixed a CRITICAL parse bug — pmus `outcomes[]`/
  `outcomePrices[]` are not index-aligned (`marketSides` authoritative; 286/286 validation) —
  **retracting** the published "pmus interim verified WRONG"/"4-YES day" findings ([L23]); corrections
  landed in CLAUDE.md + settlement-verification + execution-feasibility briefs.
- **MLB void window is MINUTES, not 2 days (#7):** Kalshi closed voided markets 47–90 min
  post-scheduled-start (n=3); 5-min statsapi poll detects postponements 5/5 (with reschedule date);
  pre-game unwind books 1¢-spread deep; unwind ≈ +12–13¢/contract on trigger. Rule spec written.
- **No-gap build verified ready (#3):** 18/18 + integration green; droplet sha checked vs HEAD; old
  build censors ~310 episodes/day (~152/day avoidable, 32/32 reconnects on the 309 s cycle-on-add
  grid). **Redeploy bundle (no-gap + ladders/trades + fee tripwire) awaits the 0006 greenlight.**
- Cheap items resolved: direction skip-gate NULL overall, weather leg-sequencing signature (18% vs
  79% toxic, z=4.6) is hypothesis-grade — pre-register (#5); early-exit = **hold-all** (breakeven
  needs P(flip)>2% vs 0/14 station-days measured; the diverging leg is always the Kalshi leg) (#6);
  recycle arm **$0** structural (#8); city-date cluster exposure ≤20%, opt-in knob built (#9);
  `/series/fee_changes` tripwire implemented — laptop half active now (#10).
- New scripts: `maker_feasibility`, `early_exit_ev`, `probe_mlb_postpone`, `recycle_arm_experiment`
  (exploratory, outside the 0014 freeze); extended: `shadow_fill` (sub-second grid, `--post-epoch`),
  `adverse_selection` (at-open gate), `settle_recon` (3-way weather recon, marketSides fix). Lesson
  [L23] filed. Re-run calendar in the brief (FOMC recon 06-18; sports recon ~06-23/25; U-3/NFP 07-03).
- **Owner greenlight (same session): flagged fixes + REDEPLOY + commit/push.** Fixes: (a)
  `pull-data.ps1` recreate-after-delete data-loss hazard — a late-append-recreated archived day is
  now kept RAW beside its canonical `.gz`, never re-gzipped/overwritten; (b)
  `weather_spread_snapshot.py` → marketSides-primary read ([L23]); (c) daily post-pull settle-recon
  (~12:30 Z scheduled pull = inside the previously-unobserved 0–13.5 h pmus-finality window;
  `ALERT.txt` on DIVERGE, `CA_NO_RECON=1` skips). Gate re-run post-fixes: **19/19**.
  **Redeployed 02:55 UTC** via `deploy.sh` (0006 owner-greenlit): build **`f8f261298097`**
  sha-byte-verified on the droplet, session_start self-identified (60 wx / 216 sports / 13 econ,
  289 pmus / 505 Kalshi), NRestarts=0, zero journal errors; **new streams verified live within one
  300 s cycle** — `trades-*.jsonl` (real prints w/ venue `vt` + taker side), `ladders-*.jsonl`
  (`hb` top-5 snapshots both venues), `fee_changes: 0` beacon. Rollback ref: prior HEAD
  `bfa9e2fccf15`. Work committed in 4 focused units + pushed to `origin/main`.

## 2026-06-10 (UTC, evening) — 0013 open items closed (multi-sub probe → no-gap adds; fee re-pin) + allocation rule PRE-REGISTERED; pipeline verified end-to-end

Owner: "complete the tasks that are doable now, then verify end-to-end pipeline." All three doable-now
items closed + the full measurement chain verified on freshly pulled post-0013 data.

- **Kalshi multi-subscription semantics PROBED + the cycle-on-add replaced** ([todo](../tasks/todo.md)):
  `probe_kalshi_ws.py --multisub` (read-only, live) answered all three unknowns decisively — ONE sid per
  channel per connection (a 2nd `subscribe` MERGES into it; the feared second-seq-counter doesn't exist),
  control acks themselves consume seq slots (seq stayed contiguous 1..16 across add/overlap-add/delete),
  `update_subscription add_markets` snapshots only the added tickers. So `monitor.py` now adds tickers
  **in-place (no-gap)** with a snapshot-confirm (`ADD_CONFIRM_SECS`) → cycle fallback, and
  `delete_markets`-unsubscribes pruned tickers; genuine seq gaps still cycle. New offline integration
  test `scripts/test_monitor_nogap.py` (localhost fake-Kalshi WS drives the real `run_live`; happy path +
  fallback both asserted) added to `selftest_all` → **18/18 green**. Motivation quantified from today's
  droplet data: 4 cycle-on-add reconnects in ~1 h, each a censored ~650-ticker rebuild. *Droplet still
  runs the old build — picks this up at the next gated redeploy (0006).*
- **Fee schedules re-pinned from primary sources** ([research/fee-pin-2026-06-10.md](../research/fee-pin-2026-06-10.md)):
  all 4 coefficients CONFIRMED (Kalshi taker verbatim via the CFTC-filed schedule — kalshi.com still
  429s; pmus via docs.polymarket.us/fees + live `feeCoefficient=0.05`). Real finding: **Kalshi maker
  fees are series-gated — 12/23 tracked series (all 5 weather, esports, ITF, UFC) charge makers $0**,
  and pmus REBATES makers −0.0125 → a weather maker-maker round-trip is fee-*negative* (~−0.3¢) vs
  ≈3.5¢ taker-taker, strengthening the queued maker study. `fee_multiplier=1` everywhere;
  `/series/fee_changes` (live tripwire) empty. `kfee(taker=False)` documented as series-blind
  (selftest-only today). ledger/audit docs updated.
- **Allocation rule PRE-REGISTERED before the data exists** ([0014](../decisions/0014-preregistered-allocation-rule.md),
  [prereg](../research/allocation-prereg-2026-06-10.md)): H1 = 0012-tested reservation semantics +
  τ=2¢ + category caps 20/10/5%; H2 = edge-rate ordering with lock-day priors frozen in-text. An
  independent **stats-methodology audit ran pre-freeze**
  ([report](../tasks/_agent_bus/20260610-prereg/stats-ml-logic-reviewer.md)): 3 CRITICAL / 7 WARN /
  5 INFO, all incorporated — headline fixes: fold-level inference (the draft's per-position bootstrap
  was ill-posed for a policy delta), arrival-date one-market-one-fold folds + a market-level ≤33%
  share gate (closed a single-persistent-market false-pass path), pinned H1 semantics + a code-freeze
  clause (unimplemented machinery = a tuning channel), trigger moved 14→**21 event-days (K≥7;
  K=4 was arithmetically incapable of a 95% distribution-free confirmation)**.
- **End-to-end pipeline VERIFIED**: `selftest_all` 18/18 → `pull-data.ps1` (sha256 mirror; today's
  live file hit the by-design TOCTOU append-race twice, finalized days gzipped+moved) → post-0013
  schema confirmed on pulled records (ms detection-time stamps, per-venue `px`, depth ladders,
  remapped econ twins live, `ws_reconnect` markers) → `analyze_persistence` + `capital_sim` clean
  over the 1.59 d mirror with the 0013 quarantine firing (36 pre-remap econ records), the −1.25 s
  lag-correction applying to pre-fix CLOSEs only, and 193 restart-censored episodes excluded.
  Droplet healthy (sessions.jsonl + transitions fresh to the minute).

## 2026-06-10 (UTC, later) — Full adversarial review #3 → ALL findings fixed; econ pairing + measurement integrity corrected

Owner asked for a full no-shortcuts review, then "fix everything + clean the docs + full test."
Review verified code against live venue APIs and the pulled archive; every finding fixed same-session
([0013](../decisions/0013-econ-grid-step-twin-and-measurement-integrity.md), [econ brief](../research/econ-settlement-identity-2026-06-10.md), lessons [L21]/[L22], full fix table in [todo](../tasks/todo.md)).

- **CRITICAL #1 — the econ mapping was off by one bucket** (pmus `≥T` inclusive vs Kalshi "Above T"
  STRICT, verified in both venues' rules text): the celebrated "first econ edge" (12.2¢ U-3, depth 423)
  was the **market-priced P(print==T)** — proven live (pmus ≥4.4 mid 0.275 ≈ twin T4.3 mid 0.33, 17¢
  from old partner T4.4 mid 0.105). Fixed via grid-step twin join (`econ_twin`): 24 pairs → **14
  identical** + 13 honest skips; remapped pairs show no phantom edges; pre-remap econ records
  quarantined at the shared loader. **Allocation OOS headline corrected: +64%/+269% → +9%/+141%.**
- **CRITICAL #2 — every CLOSE was stamped at debouncer FLUSH time** (+1.0–1.5s on every episode
  duration; no clean episode could ever log <1s, structurally defeating the sub-second leg-fill plan).
  Fixed (detection-time stamps; pre-fix data lag-corrected −1.25s). **Corrected shadow-fill: leg-fail
  55.5% @1s / 62.7% @2s** (published 39%/67%); ~27% of capturable ≥1¢ edges die ~instantly.
- **Monitor robustness**: `ws_reconnect` censoring markers (both venues); supervised heartbeat;
  degraded-discovery prune skip; **single-subscription Kalshi invariant** (cycle on seq gap / new
  tickers — second-subscribe semantics were never probed); doubleheader + duplicate-ticker guards;
  one-sided-book orientation guard; maker-fee rounding per venue-audit §2.1; `scan_all` now imports the
  bot's matchers + marginal fees (private copies had drifted); bounds-dict weather join; econ Dec/Jan
  year fix; `cod` (KXCODGAME) mapped; analysis cohort consistency + FLIP=leg-fail + both-venue ages +
  `open_flat`; `cli_stream` restart-dedup; `session_start` build hash.
- **Full test**: new `scripts/selftest_all.py` — **17/17 offline self-tests green**; live discovery +
  remapped-pair price sanity verified against both venues; full backtest pipeline re-run on the
  corrected data (capital/account/alloc/clip/shadow/velocity/adverse/cli).
- **Owner then greenlit both gated steps, done same session**: (1) bounded `--live 75` smoke test ran
  clean (351 pmus / 658 Kalshi, 14 remapped econ pairs, detection-time ms stamps; the one fat record —
  13¢ U-3 dir P — inspected and confirmed a real wide-book dislocation on a now-identical bucket, not a
  phantom); (2) **0013 build deployed to the droplet** (sha byte-identical `bfa9e2fccf15`;
  `session_start` self-identifies with `build`+argv; tracking 30/307/14). Epochs set:
  `ECON_REMAP_DEPLOY_TS = DEBOUNCE_STAMP_FIXED_TS = 1781082189` — **the multi-week accumulation clock
  restarts here on the corrected schema** (third restart: int-second build → ms+px build → 0013 build).
- **Post-deploy verification + policy read**: droplet healthy (NRestarts=0, both streams `rx_age` 0.0,
  the first live `ws_reconnect` marker was the new cycle-on-add behavior working as designed); local
  scheduled tasks green. Re-ran the policy tables on the corrected pipeline (in-sample + OOS, all
  policies) and **queued 8 next-session strategy/policy explorations in [todo](../tasks/todo.md)**
  (headliners: pre-register the category-differentiated 2¢-floor+cap rule before the multi-week data;
  edge-RATE ranking — ¢ per dollar-day, not ¢; a maker-side execution study attacking the 55%-naked
  leg-fill risk + the taker-fee wall together). Skills considered and declined (drift risk; script +
  existing `update-managerial-docs` cover it).

## 2026-06-10 (UTC) — GATED redeploy: droplet brought to current HEAD (ms+px+ECON now live)

Found the live droplet was running a **pre-`dabc106` monitor** — diagnosed not by inference but by checksum:
deployed `bot/monitor.py` (`596ac46d…`) ≠ local HEAD (`946dfcb0…`), and deployed `colisted_map.py` had **zero
`ECON`**. So the multi-week accumulation was silently running on the **wrong schema**: integer-second `t` (can't
resolve the sub-second leg-fill regime — the documented #1 execution risk), no per-venue `px` (no
adverse-selection signal), and the entire **econ** category (the cleanest US-legal subset, 24 clean pairs)
absent. Owner greenlit the gated redeploy ([0006](../decisions/0006-deploy-on-digitalocean-consult-first.md)).

- **Deployed current HEAD** via `deploy/deploy.sh cross-arb-droplet` (ships only the runtime cone + the two
  read-only secrets out-of-band; re-provision; restart). Service `active (running)`, RSS ~47 MB.
- **Verified end-to-end on disk** (not just "active", per [L9](../tasks/lessons.md)): deployed `monitor.py`
  sha now **byte-identical** to local HEAD; `round(time.time(), 3)` ms-logging present; `ECON` present. Live
  records carry fractional `t` (`1781057082.402`) + per-venue `px` (`{"p_yb":…,"k_ya":…}`). **ECON is live and
  immediately found edges**: `urc-…-june-2026-07-02-atl4pt4 OPEN net 0.1222, depth c2=423` — a 12.2¢ U-3 edge
  with real depth, event-date-partitioned to `transitions-2026-07-02.jsonl`. First econ data point ever
  collected. (Magnitude preliminary / n=1 like every `$`/`%` figure here.)
- **Effect:** the accumulation clock restarts now on the correct schema. Todo updated — next is let-it-run
  ≥ weeks + re-pull, then re-run `shadow_fill`/`adverse_selection`/`settle_recon`/`analyze_persistence`/
  `capital_sim` + a first ECON persistence read.
- **Re-ran all five harnesses on the 0.86 d mirror** (owner asked for "now," not weeks): persistence (median
  edge 0.46¢, median life 3 s; MLB line-lag is the persistent tail), `capital_sim` (peak ~$33 k / ~1.4%/day,
  preliminary), `shadow_fill` (sub-second buckets STILL resolution-limited — the ms data is only ~30 min old,
  so the gating leg-fill question is instrumented-not-answered), `adverse_selection` (n=7, noise),
  `settle_recon` (still inconclusive — pmus `closed`≠finalized). All paper / gross / <1 day.
- **Built `scripts/account_sim.py`** (self-tested) — fixed-bankroll ($500) sim answering "realized vs locked":
  walks the bankroll forward, recycles capital at settlement, splits the book into **REALIZED** (exited) vs
  **UNREALIZED** (locked). Result over 0.86 d: **~0 realized / ~100% locked** (nothing settles that fast —
  the capital-velocity finding made literal). The clip sweep is non-monotonic because PnL = deployed-capital ×
  avg-edge-per-contract and a fixed $500 saturates the capital term immediately → PnL peaks at moderate
  diversification (~clip 25), not at max or min size.
- **Confirmed discovery is parallel/batch, not sequential** (answering "how do arbs arrive first"): one catalog
  pull + concurrent WS subscribe; `open_t` = the real book-crossing instant (~97% genuine, ~3% restart re-emits).
  Corrected an earlier over-claim that arrival order was a subscription artifact — the clip's path-dependency is
  a genuine online-allocation effect, not a bug.
- **Added [docs/architecture.md](architecture.md)** — data-flow diagram (Mermaid + ASCII) venues → discovery →
  monitor → transitions → analysis/bot, with a stage-walkthrough table and the read-only→trade boundary where the
  clip / position-size lever sits. Indexed in CLAUDE.md (doc-hygiene rule).
- **Tested the clip-stage allocation policy + fixed a phantom-inflated number** (owner asked: is FIFO blind to
  bigger arbs?). Brief: [research/allocation-policy-2026-06-10.md](../research/allocation-policy-2026-06-10.md);
  decision [0012](../decisions/0012-clip-allocation-edge-floor-and-phantom-filter.md); audit
  `tasks/_agent_bus/20260610-0522/`. `alloc_policy_experiment.py` (4-agent workflow + verify): FIFO-by-arrival
  pins ~$500 on whatever crosses first (funds 1/219); the "wait 1 s + sort the batch" idea captures only **3.6%**
  of the FIFO→optimal gap and *adds* a ~39% naked-leg latency tax — the lever is a **global edge floor**, not a
  batch window. `clip_threshold_test.py` then tested **edge-floor + clip cap out-of-sample**: trustworthy claim is
  a **non-oracle fixed rule (2¢ floor + a deploy-to-full cap ~10–20%/pair) = +64% to +269% over FIFO OOS** (11
  diversified pairs); the +1744%(in-sample)/+462%(fitted) are oracle/single-obs artifacts (the +462% is 93% one
  econ contract). **Clip-cap alone = risk-control, not PnL (−3% OOS)**; FIFO → $0 under ≥0.5¢ friction; ordering
  stays 2nd-order to capital velocity (only ~19 arbs clear 2¢/0.81 d). **Phantom found + fixed:** the fattest
  "arb" (37.7¢ ITF tennis, depth 690) was a book-init artifact (captured 1.5 s post-resubscribe in a restart
  storm, flat `c2==c1==c0`, `censored=restart`) = **75% of the old in-sample headline**; `capital_sim.capturable()`
  now drops restart-censored (it didn't, though `analyze_persistence` already did) → candidates 226→219, in-sample
  +7127%→+1744%, OOS unchanged. Also fixed `account_sim.py --selftest` (list-vs-int). New scripts
  `alloc_policy_experiment.py` + `clip_threshold_test.py`. → [L19] (in-sample sweep is an oracle), [L20] (gate at
  the shared chokepoint; instrumentation ≠ gating).

## 2026-06-09 — Second adversarial review (92-agent audit) + full fix landing

Ran a second, deeper reviewer audit (10-dimension multi-agent find→adversarially-verify pass + manual
cross-read) → [tasks/reviewer-audit-2026-06-09.md](../tasks/reviewer-audit-2026-06-09.md) (8 CRITICAL root
causes, ~35 WARN). **Landed every fix this session; all 7 self-tests green + live-reverified.**

- **Invariant-#2 guards added to the live matcher** (`bot/colisted_map.py`, mirrored to `scan_all.py`/
  `persistence_scan.py`): C2 sports games bind on the **pm-slug ET date exactly** (`pick_game`) — kills the
  adjacent-series wrong-game mispair from UTC-truncated `gameStartTime`+`dnear±1`; C4 weather buckets pair
  only on **canonical inclusive `[lo,hi]` boundary equality** (`pm_bounds`/`kbounds`) — the blind positional
  index-zip is gone; `smatch` got a ≤1-char prefix guard (rejects martin~martinez); first-ever `_selftest`.
- **C3 orientation guard** in `game_edge` (>40c pm-A vs Kalshi-A reject) + `scripts/verify_sports_settlement.py`
  (C5 — sports settlement-identity was never verified; owner runs it per league).
- **C6 monitor reconnect**: both WS streams now supervised reconnect-with-backoff (the clean-close silent
  half-dead-collector hole is closed), `return_exceptions=True`, per-venue `rx_age` in the health beacon.
- **C1/ledger**: `enter()` honors the crossed/no-arb + priceability guards; `mtm`/`unwind_all` survive
  one-sided books.
- **C8 economics**: `capital_sim` de-double-counts re-detections (`one_per_market`) + book-average (trapezoid)
  profit → corrected headline **peak ≈ $13.6k (was $100k) / ~6%/day (was 8.2%)**; persistence headline now
  depth/age-gated; `analyze_persistence.load()` per-date dedup; `pull-data.ps1` TOCTOU re-hash.
- **C7 econ-legality corrected** across CLAUDE.md + README + research/README + decisions/0001 + catalog brief
  (econ IS US-legal on pmus; only crypto blocked). Settlement "VERIFIED" / `age` wording tempered. Deploy
  egress-hardening + read-only-key warning (`bot/kalshi_book.py`) + `.env.example`.
- **Lesson [L17](../tasks/lessons.md):** a guard on an unverified external convention (bucket inclusivity) must
  be reverified on LIVE data + raw source — two offline-green iterations were silently wrong (caught only live).
- **Settlement residuals closed (same session, read-only):** (a) **weather-FAQ timing contradiction RESOLVED**
  by re-reading the live FAQ — pmus *does* specify 8 AM / 11 AM-if-CLI≠METAR (the catalog brief was right);
  asymmetry narrows, not eliminated. (b) **Middle 2° bucket boundary VERIFIED** (SFO 66-67° ↔ pmus gte66lt67f,
  both `[66,67]`, same source/station). (c) **Sports settlement verifier RUN, then per-league read (10 leagues)** → new brief
  [research/sports-settlement-verification.md](../research/sports-settlement-verification.md): clean for games
  that complete on schedule, but the postpone/void tail diverges — **materially for MLB** (the proof case):
  Kalshi waits for a replay only if rescheduled **≤2 days** (else voids to "a fair price"), pmus waits **≤2
  weeks** (else last-traded), so a game replayed in that gap settles real-winner on pmus but void on Kalshi →
  both-legs loss. Esports/WNBA non-completion: pmus last-price vs Kalshi silent. Updated CLAUDE.md +
  settlement-verification.md + research/README; mitigation (don't hold MLB through a postponement) tracked in todo.
- **Sports-void EV term BUILT** (first pass): grounded the MLB postpone rate in public data (29/31 per ~2430
  games in 2024/2023 ≈ 1.3%, mlbschedulegrid.com), added `capital_sim.void_haircut()` (`P(postpone)·P(2d-2wk
  gap)·loss`, MLB ~0.26c / other sports ~0.10c / weather 0, `--void-mult` knob) → modeled sports edge drops ~7%
  ($825→$767/day). Tagged every sports pair `void_clean=False` in `colisted_map`. Decision 0010 item 3b. The
  `p_gap`/`loss_frac` are estimates pending makeup-game data; latency + leg-fill EV (0010 items 2/3) still open.
- **Read-only execution-feasibility tests built + run** ([research/execution-feasibility-2026-06-09.md](../research/execution-feasibility-2026-06-09.md),
  + [latency-playbook.md](../research/latency-playbook.md)): `latency_probe.py` (RTT ~86–261ms, network-bound →
  language is noise), `shadow_fill.py` (leg-fill hit-rate collapses with latency: 39% naked @1s, 67% @2s — but the
  sub-second regime was below the old integer-second data resolution), `settle_recon.py` (invariant #1 empirically
  inconclusive — **pmus `closed`≠finalized, ~2wk lag, interim outcomes unreliable/one verified wrong**),
  `adverse_selection.py` (instrumented, accruing). Acted on the findings: `bot/monitor.py` now logs **ms
  timestamps** (un-hides the sub-second fill regime) + per-venue `px` touches (enables adverse-selection) — both
  take effect on the next gated deploy. Rust deferred (Tier 4; compute is irrelevant at this latency scale).
- **ECON coverage added — "include ALL series" (2026-06-09).** The coverage map mapped only weather + sports;
  **econ (the cleanest US-legal subset) was silently excluded from every analysis.** Fixed: built
  `scripts/verify_econ_settlement.py` (establishes settlement identity — same family/period/threshold + same
  BLS/BEA/Fed source), then added `ECON` to `bot/colisted_map.py` (+ `scan_all.py` mirror) matching pmus
  CPI/U-3/NFP/GDP/Fed ↔ Kalshi on family+period+threshold. **24 clean co-listed pairs** now tracked (U-3 9,
  GDP 6, Fed 5, NFP 3, CPI 1); only SAME-orientation `≥`-threshold + Fed-categorical pairs are mapped (pmus
  `/book` verified YES-oriented), with `≤`-tails (opposite orientation) + "exactly X%" point-buckets skipped+
  flagged. Wired into the monitor (`MarketTracker`, like weather; fixed the prune-set so econ isn't dropped each
  heartbeat) + the analysis category functions (econ gets 0 void-haircut — cleanest settlement). Full universe
  now 60 weather + ~216 sports + 24 econ. 11/11 self-tests green.
- **Capital velocity MEASURED — early-exit refuted (2026-06-09).** Built `scripts/capital_velocity.py`
  (per-arb edge × capital recycle rate per category) + `scripts/exit_liquidity.py`. Probed resolved 06-08
  markets: **10/10 had EMPTY order books once `closed=true`** → pmus freezes the book at resolution → **no
  early-exit** → capital locked to `endDate` (~15d sports). Corrects the earlier optimistic "sports rescued by
  early-exit" — it's not. MEASURED capital efficiency: weather fast (~1.2d), sports/econ slow (~15d / weeks-mo);
  velocity (not edge) is the differentiator. Early-exit would only help when edge > exit-haircut anyway (the thin
  median 1.6–2c edges can't clear a ~2c exit). Recorded in research/execution-feasibility-2026-06-09.md §5.
- **Weather coverage + depth measured (2026-06-09).** "Why only 5 cities": **pmus lists only 5 weather markets
  total** (SF/LA/NYC/Miami/Chicago, all high-temp; 0 elsewhere) — Kalshi has ~22 cities but a cross-arb needs
  both, so pmus is the cap and the `WX` map is complete. Built `scripts/weather_depth.py` to measure size (the
  binding constraint on the one capital-efficient category). **Correction:** the resting books are DEEP (~244k
  pmus / ~72k Kalshi contracts) but the CROSSABLE depth — where the cross-venue prices actually cross to lock a
  pair — is **~157 contracts at edge≥0 and ~1 at gross-2c** (an earlier `min(total offers, total bids)` estimate
  of ~71k was wrong — that isn't crossable). So weather is deep-resting but EFFICIENT; the "thin" finding stands
  (thin on LOCKABLE edge/size); lockable size appears intermittently, not in an efficient snapshot.
- **Session synthesis (capital-efficiency thread).** Across categories: **weather** = clean + capital-efficient
  (1.2d settle + liquid evening early-exit) but SMALL (5-city pmus cap, efficient/thin lockable); **sports** =
  the depth (MLB line-lag) but capital-slow (~15d lock, book frozen — no early-exit) + void tail; **econ** =
  cleanest number but slowest capital (no early-out). No category wins {clean, fast capital, real depth}.
  Everything *measured* (latency ~86–261ms, lockups, early-exit, depth, coverage, settlement-source identity) is
  solid; every *edge/$/%* figure is preliminary (one ~8h window). New: decision
  [0011](../decisions/0011-econ-co-listing-same-orientation-only.md) (econ coverage) + lesson
  [L18](../tasks/lessons.md) (measure the decision-relevant quantity, not a flattering proxy).

## 2026-06-09 — Settlement residual-risk: live-object read + CLI revision logger

Closed the review's last settlement-timing open item two ways — read the primary source, then built the
empirical gauge for the residual risk (owner: *"do 1 and quantify the residual risk"*).

- **Live-object read** (`scripts/verify_settlement.py`, owner item #1): a live MIA pair shows Kalshi's rules
  name the *"official … **final** value"* with **expiry 10 AM EDT** (waits past 8 AM for the *final*), while
  pmus's market object carries **no** timing/preliminary/revision language (only *"Outcome verified from NWS
  Climatological Report"*). So the downward-correction asymmetry is now **half primary-source-confirmed**
  (Kalshi side documented; pmus's 8 AM-lock still third-party → needs QCX support or one observed correction
  day). → [research/settlement-verification.md](../research/settlement-verification.md).
- **CLI revision logger BUILT + LIVE** (`bot/monitor.py` `cli_stream`): polls the NWS CLI for 5 stations
  (NYC/LAX/MDW/MIA/SFO) every 30 min, logs each distinct `(station, report_date, max)` to `_data/cli.jsonl`
  (deduped on max, so NWS `version=1` issuance-flap doesn't log). `scripts/cli_revisions.py` reports the
  revision / **downward** / drop-magnitude rates per station-day — an **upper bound** on the
  "downward 8–10 AM correction splits the venues" loss rate. Both self-tested; redeployed (`active`, 0
  restarts). Day-1 read: **0 revisions / 5 station-days** — accrues over weeks.

## 2026-06-09 — Independent review → correctness fixes, settlement verified, de-filtering

Ran a fresh-eyes adversarial review of the whole project (given only the thesis, none of our own findings)
and acted on every confirmed finding. Saved as [tasks/independent-review-2026-06-09.md](../tasks/independent-review-2026-06-09.md).

- **Confirmed bugs fixed** (`bot/ledger.py`, `bot/monitor.py`): per-**order** Kalshi fee (was per-contract,
  over-charging) + a float-imprecision ceil overshoot (175.0000003→176) (**C1**); `ledger.enter` refuses a
  non-positive-edge book (**C2**); crossed/locked-book rejection (**C3**); a Kalshi seq-resync now writes a
  `kalshi_resync` marker the analyzer censors (**C6**). Lessons **L10–L13**.
- **Instrumentation:** added per-venue **staleness (`age`)** to each transition + a `capital_sim`
  **clean-fillable** filter (liquidity + freshness) to separate persistent-fillable from persistent-stale-
  phantom (review **L2**).
- **Settlement identity VERIFIED** (`scripts/verify_settlement.py`, the review's #1 risk): pulled both venues'
  live weather rules → **same NWS CLI Daily + same station (incl. NYC = Central Park) + matching boundaries**
  across 5 cities; *timing/revision* still open. → [research/settlement-verification.md](../research/settlement-verification.md).
- **De-filtering** (owner's rule — filter a trade ONLY when its ACTUAL edge ≤ 0): **marginal-fee detection**
  (the n=1 ceil fee over-charged 0.25–0.9c and dropped real at-size arbs; booking keeps the exact per-order
  fee); **permissive analysis baseline** (every positive arb; 1c/liquidity/staleness are opt-in knobs); and
  **per-direction pricing** so a one-sided book no longer drops the valid direction (`make_px`/`signal`/
  `game_edge`). Lessons **L14–L15**; decision [0010](../decisions/0010-all-in-edge-filtering-and-cost-model.md)
  (filter on the ALL-IN edge — fees + spread IN; slippage/latency/leg-risk OUT, to model in the trade layer).
- Monitor redeployed several times (live, `active`, 0 restarts). Pushed to `origin/main` through `34554dc`.

## 2026-06-09 — Deployed live + foolproof data pipeline + liveness alerting

Took the read-only monitor from build-complete to **running 24/7 on a DigitalOcean droplet**, then made the
data collection robust and self-monitoring.

- **Hardened deploy** (gated [0006](../decisions/0006-deploy-on-digitalocean-consult-first.md), owner-greenlit).
  Droplet `cross-arb-droplet` (`198.199.67.245`, 1 vCPU·1 GB·NYC1·Ubuntu 24.04 — the *measured* sizing:
  `scripts/probe_monitor_footprint.py` showed ~7 MB heap **flat over 30 sim-days** after **idle-market
  pruning**, vs ~216 MB unpruned). A dedicated unprivileged **`cross-arb`** user owns only `/opt/cross-arb`
  (`0700`); the systemd unit runs as that user with `ProtectSystem=strict`, `--forever`, `Restart=always`.
  `deploy/deploy.sh` ships **only the runtime cone** (4 bot modules + reqs + unit) via scp; secrets
  out-of-band; ed25519 key `cross-arb_ed25519` + `~/.ssh/config` alias. RSS ~76 MB live.
- **Monitor changes:** `--forever`; idle-market pruning (flat memory); **event-date partitioning**
  (`transitions-<date>.jsonl` — a market lifecycle is never split at wall-clock midnight); `sessions.jsonl`
  restart marker; `health.json` liveness beacon. Self-tests extended; all green.
- **Foolproof pull** (`deploy/pull-data.ps1`, [0009](../decisions/0009-event-date-partition-copy-keep-pull.md)):
  sha256-verified, idempotent mirror to `Kalshi/data/cross-arb/`; **verified-move** of finalized days
  (gzip local → decompress-verify == remote sha256 → delete on droplet); live files copied, never deleted.
- **Alerting:** `deploy/healthcheck.ps1` (every 30 min) checks service-active + beacon freshness → desktop
  balloon + `ALERT.txt` + non-zero exit on failure; closes the "is it still collecting?" gap (catches a
  hung-but-`active` process). Both jobs scheduled via `deploy/register-tasks.ps1` (`PullCrossArbData` daily,
  `CrossArbHealthcheck` 30 min); both test-ran clean. **All paths verified end-to-end** (move deletes a
  synthetic finalized file after verify; alert fires on forced-stale; healthy clears the alert).
- **Captured:** decision [0009](../decisions/0009-event-date-partition-copy-keep-pull.md); lessons **L8**
  (systemd has no inline comments — `ProtectSystem=strict  # …` was silently ignored, caught only in
  verification) + **L9** (verify the property you changed, not "active").

## 2026-06-08 — Version control: git init + private GitHub remote

Put the project under git (it had none) and pushed it. Early in the session: `git init -b main`, verified
the gitignore excludes `scripts/.env` / `*.pem` / `scripts/_data/` (only `.env.example` tracked), initial
commit. End of session: created **private** `github.com/kunpark04/cross-arb` via `gh` and pushed `main`
(9 commits, `7342c35`..`3241bf2`) — secrets never left the machine. Visibility is private by the owner's
choice (flip with `gh repo edit --visibility public`). Work was committed in focused units as it landed.

## 2026-06-08 — Monitor refinements: dynamic re-subscribe + FLIP debounce (build complete)

Closed the two remaining monitor pieces. (1) **Dynamic re-subscribe:** `run_live` holds the live WS
connections and, on each heartbeat, re-runs full discovery and subscribes any NEW weather days / games on
both streams (`register()`), so a day-long run keeps coverage without a restart (settled markets idle
out). (2) **FLIP debounce:** extracted `FlipDebouncer` (own self-test) — a CLOSE is held ~1s; an
opposite-direction OPEN within the window becomes one FLIP, a same-direction reopen is a suppressed
flicker, otherwise the CLOSE is flushed. **Live-verified** (`--live 120 30`): both streams + dispatch +
debouncer + heartbeat ran clean — captured a full LAX weather edge lifecycle (OPEN→WIDEN→NARROW→CLOSE)
plus live MLB edges, heartbeat re-discovery executing each cycle. The persistence-layer monitor (todo #10)
is **build-complete** as a read-only logger; an extended data-collection run is the gated DigitalOcean
deployment (decision 0006).

## 2026-06-08 — Sports 2-outcome tracker (GameTracker) + first live dual-stream run

Built `GameTracker` in `bot/monitor.py`: the 2-outcome cross-venue tracker for the sports topology (the
polymarket game market YES=team A + the TWO Kalshi single-team tickers), computing the cheapest-venue-
per-side edge (`game_edge`) and emitting the same OPEN/CLOSE/FLIP/WIDEN/NARROW transitions as the weather
`MarketTracker`. Self-test passes. Rewired `run_live` into a unified dispatch (pmus slug → fn, Kalshi
ticker → fn) routing weather to `MarketTracker` and sports to `GameTracker`; `--live N` now does a bounded
read-only run. **First live dual-stream run** (`--live 75`): connected both streams, tracked 60 weather +
124 sports (184 pmus slugs / 292 Kalshi tickers), and logged real live MLB cross-venue edges
(`aec-mlb-nyy-cle` OPEN dir PK net 0.0375; `aec-mlb-phi-tor` OPEN dir KP net 0.0681) — also confirming a
sports slug's pmus WS frame delivers a usable book. Remaining: dynamic (un)subscribe on discovery churn;
per-frame FLIP debounce.

## 2026-06-08 — Co-listed map: full-discovery builder + coverage audit

Built `bot/colisted_map.py`: `build_colisted_map()` rebuilds the {pmus slug ↔ Kalshi ticker} map from a
FULL catalog pull each call (dynamic date/event grouping → new weather days + sports games auto-covered),
reusing `scan_all.py`'s identity matching (no false positives, L1). It also returns a COVERAGE AUDIT that
flags any pmus climate city / sports league not in `WX`/`LEAGUES`. Live run: 60 weather + 123 sports
pairs; the audit immediately caught an unmapped league `twc` (influencer soccer, 1 market, no Kalshi
co-listing — left unmapped by choice). Wired into `bot/monitor.py`: `run_live` now self-discovers the
weather map and the heartbeat re-runs discovery (coverage + churn logging). Design = decision 0008;
gap-class lesson L7. Remaining for a live run: the SPORTS 2-outcome tracker, dynamic re-subscribe on
churn, FLIP debounce.

## 2026-06-08 — Kalshi snapshot/delta merge built (KalshiBook) + wired into the monitor

Built `bot/kalshi_book.py`: `KalshiBook` reconstructs a live Kalshi book from an `orderbook_snapshot`
plus `orderbook_delta` stream (prices/qty as `*_dollars`/`*_fp` strings; YES ask = 1 − best NO bid),
with a connection-level `SeqTracker` (gap → resubscribe). Confirmed empirically that Kalshi's `seq` is
**one counter per connection** (not per-market) by observing live frames across 8 markets on one `sid`.
Offline self-test passes; the live test merged 8 snapshots + **28 real deltas with no seq gaps**. Wired
it into `bot/monitor.py`'s `kalshi_stream` (replacing the stub) — both venue streams now feed the same
`MarketTracker`. Promoted the verified WS message format into `research/kalshi-venue-audit.md`. Remaining
before a live dual-stream run: the co-listed `{slug:ticker}` map (`scan_all.py`), FLIP debounce, REST
heartbeat.

## 2026-06-08 — Kalshi WS fully validated (read-only key added)

Copied the **read-only** Kalshi API key into the project (`scripts/kalshi_readonly.pem`, gitignored;
`.env` updated with the key id) — demo + read-write keys intentionally left out (least privilege,
[decision 0007](../decisions/0007-readonly-kalshi-key-least-privilege.md)). Re-ran
`scripts/probe_kalshi_ws.py`: RSA-PSS signed handshake → `101`, `subscribed` + `orderbook_snapshot`
received → **Kalshi `orderbook_delta` VERIFIED**. Both venues' WebSockets are now proven end-to-end
(polymarket.us Ed25519 + Kalshi RSA-PSS). Remaining before a live dual-stream run: the Kalshi
snapshot/delta merge in `bot/monitor.py`, the co-listed `{slug:ticker}` map, and FLIP debounce.

## 2026-06-08 — Kalshi WS: endpoint validated; authed stream blocked on creds

Validated the Kalshi side of the dual-stream as far as possible without creds (`scripts/probe_kalshi_ws.py`):
the WS endpoint is **`wss://api.elections.kalshi.com/trade-api/ws/v2`** (unauth → `401
token_authentication_failure` = live + auth-gated). Corrected the venue audit's endpoint drift — legacy
`trading-api.kalshi.com` is dead; `external-api-ws.kalshi.com/` root 404s. Kalshi WS needs auth even for
orderbook data (RSA-PSS over `{ts}GET/trade-api/ws/v2`). **Blocker:** `scripts/.env` has only PMUS creds,
so the authenticated `orderbook_delta` validation can't run — needs a Kalshi API key
(kalshi.com/account/profile). The probe is written to finish in one command once the key is added. The
`bot/monitor.py` Kalshi stream stays a stub (endpoint + auth scheme documented) until then.

## 2026-06-08 — Step 2: WS auth verified + dual-stream monitor scaffold

Verified the polymarket.us WS end-to-end (`scripts/probe_pmus_ws_auth.py`): the upgrade signs the same
REST string `{ts}GET/v1/ws/markets` → `101`; camelCase `SUBSCRIPTION_TYPE_MARKET_DATA` subscribe; live
snapshot on `tc-temp-nychigh-2026-06-08-lt72f` with frames identical to the REST book. (Also learned
`?active=true` returns stale markets — use `?closed=false` / `categories[]=climate`.) Built the
persistence-monitor scaffold `bot/monitor.py`: a pure, self-verifying transition core
(OPEN/CLOSE/FLIP/WIDEN/NARROW) + the polymarket.us stream wired with the verified protocol. Remaining
live wiring: Kalshi `orderbook_delta` auth/merge, the co-listed `{slug:ticker}` map, FLIP debounce,
REST heartbeat. Recorded the owner's deployment constraint as
[decision 0006](../decisions/0006-deploy-on-digitalocean-consult-first.md) (DigitalOcean droplet;
consult before deploy) + a cross-session memory. WS protocol promoted into
`research/polymarketus-api-auth.md` §3c (now CONFIRMED). Lessons L5 (per-frame flicker) + L6 (stale
`active=true`) captured.

## 2026-06-08 — Step 1: confirm polymarket.us WebSocket (architecture fork resolved)

Probed whether polymarket.us exposes a retail WebSocket — the one unknown blocking the persistence
monitor (`tasks/todo.md` #10). **Confirmed it does:** `wss://api.polymarket.us/v1/ws/markets`,
slug-keyed, channels MARKET_DATA / MARKET_DATA_LITE / TRADE, ≤100 markets/subscription, Ed25519 auth
on the handshake; update frames identical to the REST book. Verified empirically
(`scripts/probe_pmus_ws.py` → live, auth-gated `401`) and against `docs.polymarket.us`. Protocol
promoted into `research/polymarketus-api-auth.md` §3c. **Fork resolved:** the monitor is a clean
**dual-stream** ([decision 0005](../decisions/0005-dual-stream-persistence-monitor.md)), not the
hybrid fallback; 0003's fast-poll path is dropped. **Next:** authenticated handshake to confirm the
WS signed-string, then build the transition logger.

## 2026-06-08 — Managerial-doc scaffolding

Stood up the full managerial-doc set (the project had research + scripts + a ledger but no working
agreement). Created `CLAUDE.md` (index + invariants), `README.md`, per-directory READMEs
(`research/`, `scripts/`, `bot/`), the `decisions/` log (convention + template + entries 0001–0004),
`tasks/lessons.md` (L1–L4), and this log. Back-filled the four load-bearing decisions and four
lessons that were already implicit in the work. No code or research changed.

- **Decisions filed:** [0001](../decisions/0001-us-legal-only-venue-pair.md) ·
  [0002](../decisions/0002-comprehensive-coverage-no-pruning.md) ·
  [0003](../decisions/0003-event-driven-persistence.md) ·
  [0004](../decisions/0004-ledger-layer-by-default.md)
- **Lessons captured:** L1 (fuzzy match → fake edge), L2 (settlement identity), L3 (parse before
  "mispriced"), L4 (book keys on slug).

## 2026-06-08 — Research + scanning + accounting core *(reconstructed from artifacts)*

The substantive work that already existed when the docs were written, summarized from the briefs,
scripts, and `tasks/todo.md`:

- **Settlement + legality mapped.** Confirmed the US-legal overlap is **weather + sports**; the
  clean-settlement econ/crypto families are international-Polymarket-only (US-blocked). Briefs:
  `research/settlement-map.md`, corrected by `research/us-legal-overlap-audit.md`.
- **Data access cracked.** polymarket.us full-depth books are **public**, keyed on slug; weather uses
  the same 6 buckets as Kalshi (1:1). `research/live-edge-findings.md`.
- **Weather edge validated.** First clean, fee-netted, depth-aware scan: ~5¢ modal-bucket edge,
  ~$20/day gross, intermittent. `scripts/weather_arb_scan.py`.
- **Sports matched without false positives.** Rebuilt the matcher on `(league, date, abbrev)` after
  the v1 city-name false positive (→ lesson L1). MLB ~$23 live; deep books (tennis/UFC/ITF) fully
  arbitraged. `scripts/sports_match_v2.py`, `scripts/sports_name_match.py`.
- **Coverage made comprehensive.** Unified scanner keeps the full ~200-market co-listed universe,
  prunes nothing. `scripts/scan_all.py` → `_data/scan_all.json`.
- **Accounting core built + self-verified.** `bot/ledger.py` proves additive PnL + outcome-independence
  and encodes the layer/rotate/hold rule.
- **Open item:** the event-driven persistence monitor (`tasks/todo.md` #10) — design decided, not yet built.
