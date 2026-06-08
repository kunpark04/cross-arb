# Session log — cross-arb

Process changelog: what each working session shipped, decided, or changed in scope. Newest first.
Append an entry for any session that ships code, makes a decision, or moves scope. Keep entries
terse — link the artifact (brief / script / decision / todo item) rather than restating it.

---

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
