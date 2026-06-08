# Lessons — cross-arb

Patterns that bit us, turned into rules so we don't repeat them. Review at session start. Add an
entry after any correction or self-caught mistake; keep each one to *pattern → rule*.

---

## L1 — A fuzzy text match invents fake edges

**Pattern:** the first sports matcher (`sports_match.py`) joined markets by global text similarity.
Ambiguous city names ("Los Angeles", "Seattle" — multiple teams/leagues) cross-joined unrelated
games and reported a "cross-venue edge" that didn't exist.

**Rule:** join markets on **structured event identity** — `(league, date, team-abbreviation-pair)`
for team sports; surname for individual sports — never raw text. Add a **price-sanity guard** (a
co-listed pair whose mids disagree by >40¢ is almost always a bad join or a stale/in-play market —
auto-reject it). This is the `sports_match_v2.py` design and it's why "no false positives" is a
project invariant ([CLAUDE.md](../CLAUDE.md) → working agreement).

## L2 — Confirm settlement identity before believing a spread

**Pattern:** the seed artifact (`miami-temp-arb.html`) showed a fat ~24¢ weather "arb". It was real
as a *number* but not as an arb: Kalshi grades on NWS, while *international* Polymarket graded that
market on Weather Underground — two different thermometers. The spread was a disguised directional
bet on which source read higher, and it can lose **both** legs.

**Rule:** before calling any cross-venue gap an arb, verify **both venues name the same deterministic
settlement number** (and the same station / bucket boundaries). A large spread between venues that
grade differently is evidence *against* an arb, not for one. Settlement identity is invariant #1.

## L3 — Verify endpoint/field semantics before concluding "mispriced"

**Pattern:** an early weather scan reported a "non-monotonic CDF" on polymarket.us and we nearly
treated it as a pricing anomaly. It was a **parsing artifact** — wrong endpoint + wrong threshold
assumption. The real book (`GET /v1/markets/{slug}/book`) is the clean YES side and aligns 1:1 with
Kalshi's 6 buckets.

**Rule:** when data looks anomalous, suspect the parse before the market. Confirm which endpoint,
which side (YES/NO), and which field (`px.value`, `qty`) you're reading against a known-good
reference before drawing an edge conclusion.

## L4 — polymarket.us order books key on **slug**, not numeric id

**Pattern:** querying the book by numeric market id returns 404; we briefly thought depth was private.
It's public — keyed on **slug**.

**Rule:** use the market **slug** for `gateway.polymarket.us/v1/markets/{slug}/book`. Reads need no
auth; only order *placement* needs the Ed25519 key. (Documented in `research/live-edge-findings.md`.)

## L5 — Classify the cross-venue edge on the COMPLETE dual-venue state, not per single frame

**Pattern:** the monitor's transition core (`bot/monitor.py`) first classified on every incoming book
frame. But book deltas arrive one venue at a time, so a genuine direction reversal (FLIP) showed up as
a transient `OPEN` — the half-updated intermediate book made the prior arb vanish, then reappear in the
new direction as a fresh open. The self-test caught it.

**Rule:** treat a "tick" as a **complete dual-venue snapshot** and classify the current complete state
against the last complete state (`MarketTracker.evaluate()`). In the live per-frame path, a true FLIP
legitimately surfaces as CLOSE→OPEN across two frames — coalescing those into one FLIP is an explicit
**debounce** concern, not something to fake by resetting state mid-update.

## L6 — polymarket.us `?active=true` returns STALE markets; use `?closed=false`

**Pattern:** `gateway.polymarket.us/v1/markets?active=true` returned 7-month-old NFL games
(`...-2025-11-02`, `state=null`) — so a "pick a live market" probe found only empty books and a
subscribe failed.

**Rule:** to list **currently-open** markets use `?closed=false&archived=false`; for today's weather,
`?categories[]=climate&closed=false`. Don't trust `active=true` as "tradeable now."
(Recorded in `research/polymarketus-api-auth.md` §3c.)

## L7 — A hardcoded "what to discover" list silently misses new categories — audit it

**Pattern:** co-listed discovery keys on hardcoded maps — `WX` (5 weather cities) and `LEAGUES` (12
sports leagues). These catch new *dates/games* dynamically, but a brand-new **category** (a 6th city, a
new league) matches nothing and vanishes silently. The first discovery run surfaced an unmapped league
`twc` (influencer soccer) that a naive map would have dropped without a trace.

**Rule:** any hardcoded enumeration of "what to look for" needs a **coverage audit** that compares the
*live* universe against the config and **loudly reports** anything unmapped. `build_colisted_map()`
returns that report and the monitor logs it every heartbeat. Detection ≠ auto-inclusion — a human still
decides whether a flagged category is worth mapping ([decision 0008](../decisions/0008-colisted-map-discovery-and-coverage-audit.md)).
