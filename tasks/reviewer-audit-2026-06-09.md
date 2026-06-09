# Reviewer audit — cross-arb (2026-06-09)

Full-project review as a skeptical staff engineer: blindspots, errors, bugs, overstated assumptions.
Method: a 10-dimension multi-agent find→adversarial-verify pass (92 agents) over code + docs + the live
pulled dataset, cross-checked against an independent manual read of every load-bearing file. Each issue
below was reproduced against the actual file/data/line before being kept; 80 findings survived
verification (9 CRITICAL, 35 WARN, 37 INFO), 1 was refuted. Deduped here to distinct root causes.

This audit **post-dates** [independent-review-2026-06-09.md](independent-review-2026-06-09.md); where that
review claimed a fix "RESOLVED", we checked whether it actually landed. Several had not.

## One-line verdict

The engineering discipline is genuinely high (pure self-tested cores, loud coverage audit, gated deploy,
least-privilege intent). The problems are five recurring patterns: **(1)** the two load-bearing invariants
are *asserted but not enforced in the live matching code* — invariant #2 (no false positives) has no
boundary/identity guard on either weather or sports, and invariant #1 (same settlement number) was verified
for weather only, never sports; **(2)** the headline edge magnitudes are inflated by a real counting bug
(~6–7×) on top of a single ~8-hour sample; **(3)** the deployed monitor has a robustness hole that can
*silently corrupt the very dataset the thesis rests on*; **(4)** a few real latent bugs sit in the
not-yet-wired accounting/booking path; **(5)** a flat factual contradiction about which markets are
US-legal propagated across four managerial docs. None of this is capital-at-risk *today* (read-only phase),
but most of it must close before any of these numbers or "clean arb" claims can be trusted.

---

## CRITICAL (8 distinct root causes)

### C1. `enter()` bypasses the crossed-book guard → books a phantom "locked arb" off a stale quote
`bot/ledger.py:71-83`. `enter()` calls `signal(px)` but consumes only `s["dir"]`, discarding
`s["crossed"]`/`s["no_arb"]`. Reproduced: `Ledger().enter({'p_yb':0.30,'p_ya':0.32,'k_yb':0.70,'k_ya':0.60}, 100)`
(K internally crossed, bid 0.70 > ask 0.60) **books an entry with net_edge +35.44** because NO@K is priced
`1 − k_yb = 0.30` off the stale-high bid — while `signal()` on the *same* px correctly returns
`{crossed:True, no_arb:True}`. The only guard (`net_edge ≤ 0`) passes because the phantom edge is positive.
The C3 crossed-book rejection (decision 0010) lives only in `signal()`, never in the actual booking method.
The S6 self-test never caught it because it only asserts on `signal()`, and its crossed book happens to be
crossed in the *other* direction (caught by C2 by luck). **Latent** (live monitor doesn't call `enter()` yet)
but defeats the project's headline stale-phantom guarantee the moment a trade loop is wired.
*Fix:* add `if (s.get("crossed") or s.get("no_arb")) and not force: raise ValueError(...)` after line 73;
add an S6 regression asserting the stale-high-bid book raises.

### C2. Sports date-join can bind the WRONG game (invariant #2)
`bot/colisted_map.py:128-137` (and the byte-identical `scripts/scan_all.py:127-137`). The pm↔Kalshi game
match keys on `frozenset(team abbrevs)` + `dnear` (±1 day) + **first-match-wins** over `kbydate` in
dict-insertion order, with no game-instance pin. Every MLB/NBA/NHL series runs the same two teams on
consecutive days, so both days' Kalshi events satisfy the abbrev+(±1) key; which one binds is
dict-order-dependent. Verified in `scripts/_data/scan_all.json`: Phillies/Blue Jays, Red Sox/Rays,
Mariners/Orioles, Astros/Angels all present on adjacent days; the WSH-SF row already carries the UTC date
(06-09) for a game that is actually 06-08. Root cause is two-fold and confirmed against live data:
`gameStartTime[:10]` is **UTC-truncated** (91/278 = 33% of moneyline slugs have UTC date ≠ slug/ET date, all
late/West-Coast games rolling past 00:00Z), which is *why* the unsafe ±1 window exists. The >40c price guard
does **not** catch it (consecutive-day moneylines are ~0.12 apart). This is on the **live monitor path** and
silently pairs two distinct games' prices as one event → can lose both legs.
*Fix:* parse the date from the **slug** (carries the correct ET date, matches Kalshi's ticker date exactly on
all 278 slugs) and require exact-date equality, dropping `dnear`; optionally disambiguate on the Kalshi
ticker's embedded `HHMM` start time vs pm `gameStartTime`. Mirror into both files.

### C3. Sports leg ORIENTATION (pm YES = team A) assumed, never verified (invariant #2)
`bot/colisted_map.py:121-141` picks `lo = next(s for s in sides if s.get("long"), sides[0])` and emits it as
`teamA`; `bot/monitor.py:155-178` then hard-assumes pm's resolved YES token == team A. Nothing confirms the
pm game `/book` actually settles to the `long`/first side. If pm's YES is really team B, the cross-venue
"hedge" (YES@P teamA + NO@K teamA-wins) is **two bets on the same team = naked directional** (the S5 leg-risk),
and a mis-oriented pair can manufacture a *phantom positive edge* precisely because the legs don't offset — so
the bug is invisible in the edge logs. The only mitigation (a >40c sanity reject) exists only in the offline
scanners, **not** in `colisted_map.py` (the live path), and is blind to near-pick'em games. `lessons.md` L12
already flags this guard as "still TODO".
*Fix:* port the >40c guard into the live discovery path; better, write a read-only probe confirming which
`marketSide` the slug `/book` tracks, and quarantine sports pairs until orientation is proven.

### C4. Weather bucket pairing is a blind positional index-zip — no boundary-equality check (invariant #2)
`bot/colisted_map.py:96-101`. pm buckets (sorted by `pm_lo`) and Kalshi buckets (sorted by `floor_strike`)
are paired **by index** with no assertion that `pm_lo(pm[i]) == floor_strike(kb[i])`; only Kalshi's label is
stored, the pm boundary is discarded. The "1:1-aligned" comment rests on a single Miami observation. Today all
5 cities happen to align (6 buckets each), so it's **latent** — but if either venue ever lists a different
bucket count or a shifted boundary, every bucket above the divergence silently mis-pairs (pm gte70 ↔ Kalshi
floor 68) → two non-identical thresholds treated as one binary outcome → phantom edge in the dataset, double
loss if traded. The project *already owns* the correct guard: `scripts/nyc_align_check.py` parses true
(lo,hi) ranges and checks equality — but the production path (`colisted_map.py`, `persistence_scan.py:73`,
`scan_all.py:91`) all use the unguarded zip.
*Fix:* port `nyc_align_check`'s boundary parse; only pair when `(lo,hi)` match; surface mismatches as a loud
coverage gap; assert `len(pm)==len(kb)` per date.

### C5. Sports settlement-identity (invariant #1) was NEVER verified — only weather got `verify_settlement.py`
`scripts/verify_settlement.py` and `research/settlement-verification.md` are weather-only (grep for
sport/moneyline/Sportradar/league → no matches). Yet CLAUDE.md rests the *scalability* thesis on sports
(`lad-pit c2≈4800` MLB depth). `settlement-map.md:27` itself names *different* sources per venue for sports
(Kalshi "league/AP/Sportradar" vs pmus "DCM rulebook", "Same #? usually ✅" — an inference). Sports settles off
an official-result *call* that can be overturned/protested/postponed/forfeited; if the two venues' rulebooks
diverge on a contested game (or void asymmetrically — see W-tier), the YES+NO "lock" loses both legs on exactly
the deepest positions the bankroll thesis rides on. This gap appears on **no** open-items list (the prior
review's invariant-#1 resolution closed weather only).
*Fix:* add a sports arm to `verify_settlement.py` diffing both venues' rules incl. dispute/void/postponement
handling, per-league (MLB suspended-game, tennis/UFC retirement, esports forfeit/remake differ). File it as an
explicit open item; temper CLAUDE.md to mark sports settlement-cleanliness UNVERIFIED.

### C6. WS streams have no reconnect loop → silent half-dead collector that no alarm catches
`bot/monitor.py:601-695`. `pmus_stream`/`kalshi_stream` are a single `async with connect(): async for msg in ws`
with no `while True`, no try/except, and `asyncio.gather(...)` has no `return_exceptions`. Two failure modes,
both verified: **(a) clean close** (1000/1001 — idle timeout, LB cycling, common over weeks): `async for` ends
via StopAsyncIteration, the coroutine *returns normally*, gather keeps awaiting the other four tasks, so the
process stays alive with one venue's book **frozen** — recomputing edges and logging phantom WIDEN/NARROW/CLOSE
into the persistence dataset, while `rest_heartbeat` keeps `health.json` fresh and `healthcheck.ps1`
(`systemctl is-active` + beacon age) stays green. **(b) abnormal close**: ConnectionClosedError propagates
through gather → process exits → systemd `Restart=always` fires, but every restart re-snapshots and re-OPENs
every active edge, and >20 bounces/5min hits `StartLimitBurst` → unit dead until manual intervention. So the
robustness hole **corrupts the dataset the entire thesis is measured on**, undetected. (Note: `cli_stream`,
`rest_heartbeat`, `flusher` *do* loop+try — the two WS streams are the lone fragile tasks, so the omission
looks unintentional.)
*Fix:* wrap each stream in a supervised reconnect-with-backoff loop (re-subscribe current targets, clear+resnap
Kalshi books); set `return_exceptions=True` as belt-and-suspenders; add per-venue last-message-received
timestamps to the beacon so a silent stream is observable.

### C7. Core finding "CPI/FOMC econ is US-blocked, international-only" is FALSE — refuted by the repo's own data
`CLAUDE.md:24-25` says the clean econ universe (CPI/FOMC/crypto) is US-blocked and the US-legal overlap is
"weather + sports". But `scripts/_data/pmus_open_markets.json` (live pull, same day) contains **36 live macro
markets on polymarket.us** — CPI ("verified from Bureau of Labor Statistics"), Fed ("from Federal Reserve"),
GDP ("BEA Advance Estimate"), NFP, U-3 — each grading on the same government print Kalshi uses. The project's
own `research/us-legal-overlap-audit.md:8` states it outright: *"Econ IS live and US-legal on polymarket.us …
the cleanest arb subset — and it is not blocked,"* ranking econ the #1 "prize subset". Only **crypto** is
genuinely absent. The wrong premise is propagated across **four managerial surfaces**: CLAUDE.md, `decisions/
0001:15-16,33-34` (its own "Revisit if pmus adds econ" trigger is already satisfied but status still
"Accepted"), `research/README.md:28-31`, and `research/polymarketus-catalog-settlement.md:16,98,102` (a
same-day brief that declares econ "international-only and therefore US-illegal", never reconciled). This inverts
the project's own top-priority conclusion and mis-scopes the whole search.
*Fix:* correct all four docs — econ + weather + sports + politics are US-legal; only crypto is blocked; econ is
the structurally cleanest (episodic). Split "econ/crypto" everywhere they're lumped.

### C8. Capital + profit headlines inflated ~6–7× by counting re-detections of one market as concurrent positions
`scripts/capital_sim.py:71-81` + `scripts/analyze_persistence.py:62-118`. `build_episodes` emits a fresh
episode on every re-OPEN (after a CLOSE or a restart force-close); `simulate()` then books one interval *per
episode*, and `peak_and_avg` sums overlapping intervals as independent concurrently-held arbs. Reproduced on
the live archive: 15,799 transitions → 1,079 episodes across only **144 distinct markets**; `aec-mlb-lad-pit`
alone → 19 episodes, 13 "capturable", each booking a fresh 1,000-contract clip held to the *same* settlement.
Collapsing to one position per market: **peak capital $100,305 → $13,634 (7.4×); daily profit $2,753 → $450
(6.1×)**; the "8.2%/day on $100k" headline is a fragmentation artifact. (The driver is mostly genuine clean
OPEN→CLOSE→OPEN flicker, not restarts — removing all 18 restarts only drops episodes ~6%.) The script does
carry "<1 day = PRELIMINARY noise" caveats, but this is a *counting bug*, not small-sample noise, and the
figures are cited downstream.
*Fix:* group capturable episodes by market before computing capital/profit (one interval/market: earliest
open → shared settle, profit counted once); add a self-test that N re-OPENs of one market yield one interval.

---

## WARN — by theme (all confirmed/partial; reproduced against code+data)

**Economic claims overstated beyond C8** (the thesis's empirical core is one ~8h morning window):
- Profit applies the **top-of-book** net edge to the **entire c2 depth** with no walk-the-book decay
  (`capital_sim.py:76-77`); c2 is depth down to *2c-gross* marginal, so realized book-average edge is
  materially below `open_net` — overstatement ~1.3–1.75× and *largest for the fat-edge deep books* the
  scalability thesis leans on.
- `analyze_persistence` CAPTURABLE/scalability headline has **no depth/staleness filter** (`:184`) — 22/63
  robust episodes have `open_c2==0`; the fillability gates `capital_sim` added (L2 fix) were never applied to
  the persistence script's own headline.
- Default `--edge-min 0 / --window-min 0` reports **58% sub-5s flicker** episodes as "capturable", a ~4.8×
  overstatement of deployable edge (3213→671 c/day with edge≥1c, dur≥30s).
- `lad-pit c2≈4800` is **n=1** generalized into a class property; the 06-10 archive's two MLB markets
  (atl-cws c2≤1064, edge often negative, books stale to 84s) **fail** to replicate "deep AND edgey".
- "~$20/day gross" weather is the **sum of a single 15-min, 7-snapshot probe** (2026-06-08 06:16–06:31Z) with
  tiny/volatile depth; live archive shows median weather `open_c2 = 0`.
- "appears mid-day / evening cluster" is contradicted by the data (opens only 06–13Z = overnight-to-morning ET)
  and is a **monitor-uptime artifact** (zero coverage 14–05Z), yet todo #69-71 plans intraday allocation off it.
- All "/day" rates are a **×3 extrapolation** of a single morning regime.

**Latent ledger bugs (accounting core, not yet wired live):**
- `mtm()` / `unwind_all()` raise `TypeError` on a one-sided book when holding the missing side
  (`ledger.py:98-103,116-120`) — `signal()`/`make_px` were hardened for `None` touches; these were not.
- `enter()` raises raw `TypeError` (not the intended clean `ValueError`) when given a direction the book can't
  price, incl. the default-direction path when `signal()`'s no-opts fallback returns dir 'P' on a both-sides-
  missing book.

**Matching false-positives (beyond C2/C4):** `dnear ±1` root cause + the unused HHMM start-time disambiguator
sitting in the Kalshi ticker; `smatch` 4-char surname **prefix** rule collides distinct players
(martin~martinez, williams~williamson, mann~mannarino) — no live FP today but structurally unguarded;
`colisted_map.py` — the most identity-critical module — has **zero offline tests/asserts** (every other core
ships a `_selftest`).

**Data pipeline:** midnight-UTC finalize/delete in `pull-data.ps1` can drop late same-event-date transitions
(a US-evening game still appending past 00:00Z whose file gets gzipped-then-deleted; delete trusts the step-1
hash, never re-hashes the live remote — TOCTOU); raw+`.gz` for one date can coexist after a recreate and
`load()` double-reads (no per-date dedup).

**Settlement (beyond C5):** "VERIFIED" overstates a **sampled, eyeball-only** check — `verify_settlement.py`
prints boundary strings but never programmatically compares inequality direction (< vs ≤) or rounding, and only
low-tail buckets on one day were checked (the modal high lands in the unchecked middle 2°F buckets);
**internal contradiction** on pmus weather-FAQ timing — `catalog-settlement.md:49-52` quotes a verbatim
"8:00 AM ET / delayed to 11:00 AM ET" while `settlement-verification.md:46-47` says "the FAQ is silent",
unreconciled, and the whole Kalshi-vs-pmus asymmetry narrative rests on it.

**Monitor:** `age` measures "seconds since the book last *changed*", so a quiet-but-live (fillable) resting
quote and a wedged-stream phantom both accrue large age — it does **not** cleanly "distinguish fillable from
stale-phantom" as CLAUDE.md claims (depth does the real work; age is a coarse hint).

**Security/deploy:** the README claim "cannot place an order even if compromised" is **mis-attributed** to the
systemd hardening — `ProtectSystem=strict` is filesystem-only; the unit has **no egress restriction**
(`IPAddressDeny`/`RestrictAddressFamilies`/`SystemCallFilter` all absent). What actually prevents an order is
the read-only Kalshi key (a venue-side ACL), and decision 0007's least-privilege is **operational only** —
nothing in code verifies the loaded key is read-only; pointing `KALSHI_PRIVATE_KEY_PATH` at a trade-capable PEM
would be silently accepted.

**Legal granularity:** the audit's clean "NFP ✅" is agency-level — the same-name Kalshi sibling `KXUSNFP`
settles on **Trading Economics** (aggregator), not BLS; several econ series carry copy-paste-wrong source URLs,
so identity matching must be per-ticker, not per-agency. The WU-vs-NWS "CRITICAL divergence" in
`polymarket-venue-audit.md` is a **.com-only** artifact not fenced as such (live pmus = 48/48 NWS CLI).

**Docs↔code drift:** README's `$23 MLB / $20/day weather` figures were **never tempered** despite the prior
review claiming "(CLAUDE.md/README)" — the temper landed only in CLAUDE.md, contradicting the project's own
L14 lesson; `weather_arb_scan.py` is hardcoded to a past date (2026-06-08) + 2 cities but documented ⭐ as the
canonical "live" scan (returns empty today).

---

## INFO (selected — full list in the audit run output)

Settlement: LST-vs-local-clock day-window divergence omitted from the residual-risk list (a both-legs-lose
path independent of revisions, live during DST); `cli_revisions` rate is 5 station-days on one day (supports no
claim); `_cli_iso` silently corrupts report_date if NWS ever abbreviates a month; the "final value" Kalshi
confirmation generalizes one MIA market to all 5 cities. Ledger: `kfee` maker path scales after ceiling
(slightly-low sub-cent fee); the `EPS=0.011` comment is misleading (the additive identity is exact — the ceil
cancels on both sides). Gap-tier blindspots (acknowledged-open, unquantified): detection-to-both-legs-filled
latency (the dominant real-world arb killer; `--haircut` defaults to 0 = "assume zero latency+leg-risk");
two legs settle at different clock times so capital isn't freed atomically; venue min-order-size/tick not
enforced in the depth model; displayed depth treated as executable (never pinged); counterparty/withdrawal/
idle-float (must pre-stage capital on *both* venues ≈ 2× float); correlation across "independent" edges (one
bad city settlement hits all its buckets); wash-trade/self-match + position limits at scale. Docs: CLAUDE.md
says "9 research briefs" (now 10); deploy README says NYC3, droplet is NYC1; "0 restarts" vs 18 boots in the
data. Deprecated `sports_match.py` (the original L1 fake-edge culprit) still present, unmarked.

---

## What's solid (verified correct, or correctly refuted)

- `bot/kalshi_book.py` snapshot/delta merge, YES-ask = 1−NO-bid, ladders, `SeqTracker` — no side-flip or
  off-by-one. `depth_curve` two-pointer walk, `peak_and_avg` overlap math, event-date partitioning +
  restart-censoring logic, `cli_revisions` chain/downward classify — all correct.
- The ledger's **additive PnL + outcome-independence invariants are genuinely proven** (for balanced books);
  the fee *coefficients* (Kalshi 0.07 whole-order ceil, pmus 0.05 linear) match the documented venue schedules.
- `pull-data.ps1` sha256 verify-before-trust and verify-before-delete (decompress local .gz to the remote hash
  before `rm`) is real integrity engineering (the TOCTOU above is a narrow edge of it).
- **Refuted finding (worth recording):** the claim that a Kalshi seq-gap resync emits phantom CLOSE→OPEN
  transitions because tracker `state` isn't reset is **wrong** — `evaluate()` only runs after a full snapshot
  rebuilds the book, `classify()` returns None when unchanged, and *retaining* state is exactly what avoids a
  phantom CLOSE. Resetting (the proposed "fix") would be worse. Skepticism cut the right way here.

---

## Recommended priority order

1. **C6** (monitor reconnect) — it's live now and silently poisoning the dataset every collection day; nothing
   downstream is trustworthy until it's fixed. Cheapest high-value fix.
2. **C2/C3/C4** (invariant #2 in the live matching path) — wrong-game/orientation/bucket pairs corrupt the
   persistence data and would lose both legs; all three are cheap guards the project already has parts of.
3. **C8 + economic-WARN cluster** — correct the counting bug and re-state every cited $/day, %/day, depth, and
   "$23/$20" number as preliminary-and-corrected before anyone reasons from them.
4. **C7** (econ legality) — one-paragraph fix across 4 docs; it changes *where the project should be looking*.
5. **C5** (sports settlement identity) + the settlement WARNs — verify before sports size is called "clean".
6. **C1 + ledger WARNs** — close before any trade loop is wired (latent today).
7. Security/deploy + docs drift — accuracy/hygiene; do alongside the above.
