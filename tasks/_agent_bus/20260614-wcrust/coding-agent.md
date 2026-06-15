---
from: coding-agent
run_id: 20260614-wcrust
timestamp: 2026-06-14T04:08:00Z
task_summary: Implement the bot-rs LIVE-path side of "World Cup tradeable" — per-outcome BINARY arbs (discovery + routing marker), reusing the weather/econ 1:1 core unchanged.
files_changed: [bot-rs/src/discovery.rs, bot-rs/src/main.rs, bot-rs/src/postpone.rs, tasks/todo.md]
files_created: []
context_loaded: [~/.claude/plans/deep-waddling-barto.md, bot-rs/README.md, tasks/lessons.md, bot/colisted_map.py, bot-rs/src/discovery.rs, bot-rs/src/types.rs, bot-rs/src/main.rs, bot-rs/src/matcher.rs, bot-rs/src/risk.rs, bot-rs/src/signal.rs, bot-rs/src/config.rs, bot-rs/src/postpone.rs, bot-rs/src/unwind.rs, bot-rs/src/venue.rs]
libraries_consulted: []
self_review_artifact: tasks/_agent_bus/20260614-wcrust/code-logic-reviewer.md
---

## What I changed
- `discovery.rs`: new SOCCER 3-way branch (port of `colisted_map.py::soccer3_emit`). `SOCCER3` (`fwc`->`KXWCGAME`) + `SOCCER_CC_ALIAS` (`irn`->`iri`, `alg`->`dza`, `hai`->`hti`) kept SEPARATE from `LEAGUES_ABBREV` (moneyline path untouched). New `soc_parts` slug parser + `pick_wc_game` (exact-date + used-set bind resolving team-A/B + TIE, all distinct). The branch groups pmus `category=="sports"`+`marketType=="drawable_outcome"`+`atc-fwc-` slugs per `(a,b,date)`, requires all 3 siblings + a Kalshi TIE, and emits each outcome as a PER-OUTCOME BINARY `Pair { kalshi_b: None, cat: Cat::Sports, soccer: true, settle_clean: true }`. `discovery.rs:748-833`.
- `discovery.rs`: added `soccer: bool` to `Pair` and `soccer_pairs`/`soccer_leagues_unmapped` to `Discovery` (coverage audit, L7). `discovery.rs` Pair/Discovery defs.
- `main.rs`: `soccer` threaded through `LivePair` + the `From<discovery::Pair>` impl; `track_position` skips MLB-poll enrollment for a soccer pair (empty poll metadata -> the MLB poll's `league=="mlb"` filter ignores it); WC smoke case added (binary `signal` arm, dry-run). `main.rs` LivePair / track_position / smoke.
- `postpone.rs:218`: the non-mlb "no auto-unwind source" warning now skips an EMPTY-league position (a WC `Cat::Sports` pair enrolls without poll metadata on purpose).
- ROUTING: **no routing-logic change needed** — the live loop already routes on `pair.kalshi_b.is_some()` (`main.rs:462`: `Some` -> `game_signal`, `None` -> `signal`). A WC pair emits `kalshi_b = None`, so it structurally takes the BINARY `signal` arm. `build_legs`/`risk::evaluate`/`exec`/`unwind`/`Position`/`game_signal` are UNCHANGED.

## Why (non-obvious only)
- **Reused `Cat::Sports`, did NOT add a `Cat::Soccer` variant.** Traced against every `Cat`-keyed branch: settlement passes via the `q.settle_clean` clause (`risk.rs:107`); `lock_days(Cat::Sports)` (`risk.rs:80`) uses the dynamic `days_to_event` — the correct near-dated model (a `Cat::Soccer` variant would fall to the 21d econ fallback = wrong); the toxicity gate is `Cat::Weather`-only; the MLB postpone poll only acts on `league=="mlb"` so WC is never wrongly unwound. The minimal `soccer: bool` marker carries the regulation-settlement semantics + de-enrolls WC from the MLB poll.
- **Removed a dead `pm_yes_price` port** (was in the first draft, mirroring the Python). In `bot-rs`, discovery only MAPS slug<->ticker (like the weather/econ branches) — it never reads catalog prices. The live pmus YES book comes from the per-SLUG YES-oriented WS frame (`venue::parse_pmus_market_data`: `bids`=YES bids, `offers`=YES asks), so the L23 YES-orientation is honored where the price is actually read; a catalog `marketSides` Yes-side reader in discovery is dead code (self-resolved in the review pass).

## Verify
- `cargo test --manifest-path bot-rs/Cargo.toml`: **135 passed** (was 128; +7 WC tests net after removing the dead-code test).
- `cargo clippy --manifest-path bot-rs/Cargo.toml --all-targets`: **clean** (0 warnings; refactored a complex-type annotation out of the soccer branch).
- `cargo run --manifest-path bot-rs/Cargo.toml -- --smoke`: the WC case shows `APPROVED size=1 dir PK` -> `[DRY-RUN] Pmus Yes @42c market=atc-fwc-ger-cuw-2026-06-14-ger` + `Kalshi No @55c market=KXWCGAME-26JUN14GERCUW-GER` — the 1:1 binary shape (Kalshi **No** leg on the SAME outcome's ticker), in contrast to the moneyline sports case's `Kalshi Yes ...-PIT` (team-B). Dry-run, no orders.
- Differential parity vs the Python `soccer3_emit` (same fixtures): GER/CUW 3 records (-ger->GER/-cuw->CUW/draw->TIE), alias irn->iri binds IRI, unknown-code 0, partial-game 0 — byte-identical. Python `colisted_map.py` selftest still green (reference untouched).
