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
**debounce** concern, not something to fake by resetting state mid-update. **(Implemented 2026-06-08 as `FlipDebouncer` in `bot/monitor.py`, with its own self-test.)**

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

## L8 — systemd has NO inline comments; a trailing `# …` silently breaks the directive

**Pattern:** the `cross-arb-monitor.service` unit had `ProtectSystem=strict          # whole FS read-only`
and `ReadWritePaths=/opt/cross-arb/scripts/_data   # … except the JSONL sink`. systemd parsed the **entire
rest of the line** as the value → "Failed to parse protect system value, ignoring" and "ReadWritePaths path
is not absolute, ignoring: #". Both hardening directives were **silently dropped** (non-fatal warnings), so
the service ran *without* the read-only-FS confinement I thought I'd applied. Caught only because
post-deploy verification ran `systemctl show -p ProtectSystem` instead of trusting "active (running)".

**Rule:** **never put an inline/trailing comment on a systemd directive line** — comments must be on their
own line starting with `#`. And **verify hardening took effect** with `systemctl show -p <Directive>`
(active ≠ configured-as-intended); a unit can run fine while half its `[Service]` settings were ignored.

## L9 — Don't trust "active (running)"; verify the thing you actually changed, end-to-end

**Pattern:** related to L8 — a deploy can report `active (running)` while a load-bearing property is silently
off (ProtectSystem ignored; or a write path that the read-only sandbox would block). The green status said
nothing about whether confinement worked **or** whether the monitor could still write its log under it.

**Rule:** verify the **specific property you changed**, not a proxy. After enabling `ProtectSystem=strict`,
confirm both that it's effective (`systemctl show`) **and** that the intended write still succeeds (the log
file grew). "It started" is not "it does what I changed it to do."

## L10 — Kalshi fee is per-ORDER (ceil once), and naive `ceil()` overshoots on float noise

**Pattern:** `kfee` charged `ceil(0.07·p(1-p))` **per contract** then summed — over-charging vs the published
`ceil(0.07·N·p(1-p))` rounded **once per order** (100@0.5: $2.00 vs $1.75). Worse, `0.07*100*0.25*100`
floats to `175.00000000000003`, so `ceil` jumped to **176** → a spurious extra cent.

**Rule:** model exchange fees on the **whole order** (size known at booking), not per-contract-summed; and
when `ceil`-ing money, subtract a tiny epsilon (`ceil(x - 1e-9)`) so float noise at a cent boundary doesn't
manufacture a cent. **Detection** (`signal`, per-$1) must use the **at-scale MARGINAL** fee (no ceil) — the
n=1 ceil fee over-charges ~0.25–0.9c and would drop a real arb that's only +EV at size (see L15); reserve the
exact per-order ceil fee for **booking** (`Ledger.enter`, where the size is known).

## L11 — An accounting/entry function must REFUSE a non-positive-edge book, not silently book it

**Pattern:** `ledger.enter()` computed the edge but booked the pair **regardless of sign** — fed a no-arb
book it locked a guaranteed loss (net −4.19) with no guard. Wired to a live bot, a stale snapshot books a
losing "arb."

**Rule:** the order-booking path refuses `net_edge ≤ 0` (and a forced-direction negative) loudly (raise),
with an explicit `force=True` override for tests. Never let the accounting core book a position the signal
says is a loss.

## L12 — A crossed/locked venue book manufactures a PHANTOM arb; reject it

**Pattern:** when a single venue's book is internally crossed (`yes_bid > yes_ask`, e.g. from a stale/in-play
mid-update), the cross-venue math reports a fat "edge" (and depth) that isn't takeable — and the depth code
even paired that venue with *itself*. Crossed/locked books are almost always stale.

**Rule:** reject any book with `best_bid > best_ask` on a venue (allow `==`, a legitimate locked book), and
compute depth on the **signalled cross-venue direction** so both legs are always on different venues. This
guard belongs in the **live** transition path, not just the offline scanner (the >40¢ mid-divergence guard
of L1 should follow it there too — still TODO).

## L13 — "Persistent edge" ≠ "fillable edge"; instrument staleness + liquidity and filter on them

**Pattern:** the persistence harness measured how long an edge *state* lasted, but a wide gap can persist
precisely because the cheap side is a **stale phantom quote** nobody can hit, or a 5-contract one-sided book.
Persistent-and-fillable and persistent-and-stale were indistinguishable.

**Rule:** log the inputs that decide fillability — per-venue book **staleness** (`age`) and **depth** at the
edge — and gate "capturable" on them (fresh both sides + depth ≥ floor), not just on net + duration. An edge
on a stale or thin book is a measurement artifact until proven otherwise.

## L14 — Prose conclusions must not outrun the code/data; an independent adversarial review is cheap insurance

**Pattern:** an independent reviewer (given only the thesis, none of our findings) confirmed the engineering
was sound but caught that managerial-doc prose stated "the edge is real / MLB ~$23 / ~$20/day" as conclusions
that 9 transitions + 17 minutes of data + a wrong fee model couldn't support — and that one load-bearing
assumption (settlement identity) was self-contradicted across our own briefs and never closed.

**Rule:** a `research/`/`README` claim must be traceable to code that does what it says + data that supports
the magnitude; otherwise mark it preliminary/unverified. Periodically run a **fresh-eyes adversarial review**
scoped to *only* the thesis (no spoon-fed conclusions) — it found 4 real bugs and the #1 thesis gap in one pass.

## L15 — Filter a trade ONLY when its actual edge is ≤ 0; thresholds are analysis knobs, not silent filters

**Pattern:** hardening added guards that, read as trade filters, dropped genuinely positive arbs — detection
charged the n=1 ceil fee (over-stated ~0.25–0.9c → marked real at-size arbs no-arb), and the capital/
persistence tools *defaulted* to net ≥ 1c + a liquidity floor + a freshness gate, hiding positive sub-1c /
thin-but-real arbs. The owner caught it: "don't filter trades for no reason unless the actual edge is ≤ 0."

**Rule:** the only legitimate reason to drop a candidate trade is its **actual edge ≤ 0** (at the size you'd
trade) or **unreliable data** (stale/crossed = effectively ≤ 0). Magnitude/liquidity cutoffs are **opt-in
analysis lenses** that must always show the unfiltered baseline alongside — never silent defaults. A thin
book is *small* size, not *no* trade (size down, don't skip). Detection uses the **marginal (at-scale)** fee
so nothing +EV-at-size is dropped (L10); booking uses the exact per-order fee.

## L16 — Edge-location and scale-capacity are DIFFERENT axes; target their intersection

**Pattern:** a core finding read "edge appears in inefficient corners, **not** deep books." True about *where
the gap is*, but it silently conflated two orthogonal axes and made the strategy look un-scalable. **Edge**
(the gap) comes from inefficiency, which correlates with **thinness**; **capacity** (size you can deploy
before walking the book past the edge) comes from **depth**. Efficient books are deep *because* they're
arbitraged, so edge and depth usually **anti-correlate** — which is exactly why "edge ≠ deep books" reads as
a dead end. It hides the only case that scales: a deep book that is *transiently* dislocated.

**Rule:** measure "where's the edge" and "how much can I deploy" as **separate** quantities and hunt their
**intersection** — depth AND edge co-occurring. The proof case is **MLB new-venue line-lag** (`lad-pit`
c2≈4800 contracts *on* a live gap); weather corners are edge-but-thin; tennis/UFC are depth-but-no-edge.
Scaling = **breadth of depth-and-edge events × per-event depth-capped size**, never bigger clips in one thin
corner. Initial capital is sized to **peak concurrent deployable depth** (`capital_sim.py`), not to
opportunity count. Keep thin corners in scope (real edge, just small size — L15), but the bankroll thesis
rides on depth-and-edge events.
