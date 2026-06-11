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

## L17 — A guard resting on an UNVERIFIED external convention must be reverified on LIVE data + the raw source, not just a self-test

**Pattern:** the 2nd-review C4 fix (weather bucket boundary-equality guard) was written + offline-self-tested
**three different ways**, and the first two were *wrong* — they silently dropped 40 / 10 legitimate live pairs
because the self-test baked in the same guessed inclusive/exclusive convention the code used. The offline test
couldn't catch it: both sides shared the wrong assumption. Only running the guard on the **live discovery
path** surfaced the mass false-misalignment, and only **inspecting the raw Kalshi `floor_strike`/`cap_strike`
+ `yes_sub_title` next to the pm slugs** revealed the true (non-uniform) convention — Kalshi *middle* buckets
are `[floor, cap]` inclusive while *tails* encode the boundary exclusively (`floor+1`/`cap-1`), and pm
`gteXltY` is the 2°-wide `[X, Y]` bucket. So you must **canonicalize BOTH venues to one representation**
(inclusive `[lo,hi]`) before comparing — never compare raw venue-specific encodings.

**Rule:** when a settlement-/identity-critical guard rests on a premise the project itself marks UNVERIFIED
(here: bucket inclusivity), an offline self-test is necessary but **NOT sufficient** — reverify end-to-end on
live data and confirm the decoding against the **primary raw fields**. A test written from the same wrong
mental model passes while the guard quietly destroys coverage (false negatives) or admits false positives.
"Tests green" ≠ "correct" when test and code share an unproven assumption.

## L18 — Measure the DECISION-RELEVANT quantity, not a convenient proxy — and be suspicious when a number flatters

**Pattern:** in one session, two "favorable" figures both collapsed under the rigorous version, *each time the
optimistic proxy was the wrong quantity*:
- **Leg-fill risk** was first quoted off the *full capturable cohort* (39% naked at 1s) — which is dominated by
  1-second flickers you'd never trade. The decision-relevant cohort is the **persistent + deep** subset, where
  the 100s+ duration makes leg-fill a non-issue. Quoting the aggregate made a fillability problem look worse (and
  earlier, a different aggregate made the *opportunity* look thinner) than the tradeable subset actually is.
- **Weather "depth"** was first reported as `min(total offers, total bids)` ≈ **71k** "fillable" — but that
  counts resting orders that *never cross*. The decision-relevant quantity is **crossable** depth (Kalshi YES bid
  must exceed the pmus YES ask), measured by the two-pointer walk: **~157 contracts at edge≥0, ~1 at gross-2c.**
  The books are deep-*resting* but *efficient*; lockable size is tiny. `min(total,total)` flattered it ~450×.

Both errors share a shape: a cheap aggregate (a sum, a min-of-totals, the whole cohort) stood in for the precise
thing the decision turns on (the tradeable subset; the crossable, edge-qualified, settle-clean size).

**Rule:** before citing a number, ask *"is this the quantity the decision actually turns on, or a proxy that
happens to be easy to compute?"* Edge/depth/fill claims must be on the **tradeable, crossable, edge-qualified,
settle-clean** subset — not the raw aggregate. And when a figure flatters the thesis, treat that as a prompt to
re-derive it the hard way *before* repeating it: the favorable number is exactly the one most likely to be the
wrong quantity. (The user named the tell: "the favorable numbers keep needing the asterisk.")

## L19 — An in-sample parameter sweep is an ORACLE; quote the out-of-sample, non-tuned number as the result

**Pattern:** the allocation-policy experiment chose τ=5.835¢ by sweeping τ over the *same* data it scored on,
yielding "+7127% vs FIFO". That τ is fit to the data — an oracle ceiling, not a deployable constant. When the
design (edge threshold + clip cap) was then tested out-of-sample (`clip_threshold_test.py`), the audited reality
was far smaller and differently-shaped: a **fitted** policy's "+462% OOS" turned out to be **93% one econ
contract** (re-cutting the train/test split inflated it to +801%/+1237% purely by shrinking the FIFO
denominator), while the honest, trustworthy claim was a **non-oracle fixed rule** (skip <2¢, cap ~5%/pair) at
**+61% OOS across 11 diversified positions**. The headline magnitude and the deployable magnitude differed by
~100×, and a second lever I'd bundled in (the clip cap) turned out to contribute **nothing to PnL** (−3% OOS) —
its value is risk-control, not return.

**Rule:** never quote an in-sample swept-parameter magnitude as a result. The reportable number is the
**out-of-sample** one produced by a rule that was **not tuned on the test data** — and check it isn't a single
lucky observation (bootstrap / re-cut the split / report top-1 position share). When a design bundles multiple
levers, isolate each: a lever that doesn't move the held-out metric is not part of the edge (it may still be
justified as risk-control — say which). On <1 day of data this is a **method demo, not validation**; label it so.

## L20 — A quality gate must live at the shared chokepoint, not in one report's metric; instrumentation ≠ gating

**Pattern:** the project *had* a "phantom filter" (review L2: `liq_floor` + `max_age` in `capital_sim.capturable()`),
so it felt safe to assume phantoms were excluded. They weren't. A book-initialization phantom — a 37.7¢ ITF-tennis
"arb" (`aec-itfm-fravaz-fedval`) captured **1.5 s after a fresh resubscribe** during a ~10-restarts-in-90-min storm,
with a **flat `c2==c1==c0=690` ladder** and `censored="restart"` — flowed straight into every allocation analysis and
became **75% of the in-sample headline** (in-sample FITTED $142 → $36 once removed). Three holes lined up: (1) the
`max_age` gate was **off by default** (and `build_episodes` keeps only `max(p,k)` age, discarding the `k=0` fresh-subscribe
tell); (2) there was **no flat-depth check**; and decisively (3) `analyze_persistence.summarize()` *already* excluded
restart-censored from its CAPTURABLE metric (line 192), but the shared `capturable()` that `account_sim`/`alloc_policy`/
`clip_threshold` actually call **never got that rule** — the same concept ("capturable") was defined two ways. Compounding
it: the **live monitor is instrumentation-only** — it logs age/depth/px and gates only genuinely-crossed books, so *every*
downstream consumer must apply the phantom filters; "the monitor/analysis already filters it" was an unverified assumption.

**Rule:** put a quality/identity gate at the **single shared chokepoint** every consumer passes through (here: `capturable()`),
never in just one report's metric — and when the same concept is defined in two places, **trace the actual call path** to
prove they agree (a grep for the field name in the function would have caught it: `capturable()` had no reference to
`censored`). **Instrumentation ≠ gating**: a logged field (`age`, `depth`, `px`) changes nothing until code acts on it;
don't assume a recorded dimension is an enforced one. And reconfirms [L18]: a too-good number (19¢ on an obscure ITF match)
is the asterisk — interrogate the raw record (age, depth-ladder shape, censor reason) before believing it.

## L21 — "Same threshold number" is not "same bucket": verify the INEQUALITY semantics per family, against primary rules text

**Pattern:** the econ matcher joined pmus `≥ 4.4` to Kalshi `T4.4` because the threshold *numbers* matched, and
0011 waved the `≥`-vs-`>` difference off as "a narrow residual, akin to the weather downward-correction." It is
not a tail: pmus "at least T" is inclusive, Kalshi "Above T" is **strict** (`strike_type: greater`, verified in
rules text), and on a 0.1-quantized print `> T ≡ ≥ T+0.1` — so the pair is **off by one bucket** and diverges on
a print landing exactly on T, the **modal region** for an at-the-money threshold. The market priced it: the
"12.2¢ persistent U-3 edge with 423 contracts of depth" the monitor proudly logged was P(print==4.4), a
both-legs-loss lottery sold as a lock; pmus `≥4.4`'s mid sat next to Kalshi `T4.3` (the true twin), 17¢ from its
assigned partner. The bitter part: the weather matcher already encoded this exact convention correctly
(`kbounds`: floor-tail = `[floor+1, ∞)` on the integer grid) — the knowledge existed in-repo and didn't transfer.

**Rule:** for ANY threshold market pair, verify three things separately, per family, against **primary rules
text** (never by analogy to another family): (1) the threshold **number**, (2) the **inequality** (inclusive vs
strict — `strike_type`, "at least" vs "above"), (3) the print **grid** (one decimal? thousands?). The identical
twin of an inclusive `≥T` against a strict-`>` venue is `floor = T − grid_step`. A residual whose probability is
the *modal outcome* is not a residual — quantify the divergence-outcome's probability (the market itself prices
it: adjacent-strike gaps) before calling anything "narrow." And when a *brand-new category's very first data
point is the fattest edge on the board* ([L18]'s tell), suspect the mapping before the market.

## L22 — A smoothing layer in front of a logger biases every downstream measurement; stamp DETECTION time, not emission time

**Pattern:** `FlipDebouncer` held every CLOSE ~1.0–1.5 s (flip-coalesce window + flusher tick) and `_write`
stamped `time.time()` **at flush** — so every clean episode's duration was silently inflated by ~1.0–1.5 s. That
single emission-time stamp (a) made the shadow-fill leg-fail rate at 1 s read 39% when the lag-corrected value is
**55.5%** (optimistic in exactly the regime the decision turns on), (b) hid that ~27% of capturable ≥1¢ edges die
essentially instantly, and (c) made the *planned* sub-second measurement structurally impossible — with a 1 s
hold in front of the logger, **no clean episode could ever log a duration under ~1 s**, so the ms-timestamp
upgrade shipped to answer the sub-second question could never have answered it.

**Rule:** any debounce/coalesce/smoothing layer between detection and a measurement log must carry the
**detection timestamp** through to the record (smooth the *event stream*, never the *clock*). Before trusting a
duration-derived metric, trace the timestamp's origin end-to-end (who calls `time.time()`, when?) — and check the
floor: if a pipeline stage holds events for X seconds, no logged interval below X is real, and any analysis
claiming resolution finer than X is measuring the pipeline, not the market. When retro-correcting, subtract the
lag at the shared loader (one place), clamped so corrected times can't cross other events.

## L23 — Sibling arrays in one API object are NOT index-aligned by default; validate a pairing convention on cases where the conventions DIVERGE

**Pattern:** pmus market objects carry the settled winner in two encodings: flat sibling arrays
(`outcomes[]`, `outcomePrices[]`) and self-labeling `marketSides[]` (each side carries its own label +
settled price). We documented "pair `outcomes[i]` with `outcomePrices[i]`" — wrong: `outcomePrices`
follows **`marketSides` order**, while `outcomes`' order is cosmetic display noise. The bug manufactured
22 phantom weather "divergences", 44/70 internally-impossible multi-YES days, and two *published*
findings — "pmus interim settled data verified WRONG (ATP case)" and "MIA 4-YES day" — i.e., a
venue-trust conclusion created entirely by our own parser. It survived the 2026-06-09 spot-checks
because Yes-first objects agree under **both** conventions (the checks only sampled coinciding cases —
the L17 failure shape again). It was caught when the recon's multi-YES tripwire fired on 44/70 days: an
*impossible* market outcome indicts the parse, not the market (L3). Corrected read validated 286/286 raw
settled objects vs Kalshi; weather recon flipped from "22 divergences" to **360/360 identical**.

**Rule:** when one response encodes the same fact twice, prefer the **self-labeling** encoding (the label
travels *with* the value) over positional sibling arrays — and never assume two flat arrays are
index-aligned without a primary-doc statement or a divergence test. Validate any pairing convention on
cases where the candidate conventions **disagree** (here: `["No","Yes"]`-ordered objects); agreement on
coinciding cases is zero evidence (L17). Build **impossibility tripwires** (e.g. exactly-one-YES per
exclusive bucket set) into every reconciliation reader — they convert parse bugs into loud errors before
they become "findings". And retraction discipline: when a published finding dies, retract it at every
place it was stated (briefs, CLAUDE.md, README indexes), not just where it was born.

## L24 — RECURRING markets reconcile on PAST cycles (don't wait for the next print); and honor the parsed inequality, never re-assume orientation in a throwaway

**Pattern:** two errors, one session, both about econ settlement. (1) We kept saying "econ settlement is
unverified — wait for the FOMC/U-3 release," which conflated "*this* cycle hasn't settled" with "*none
ever* has." Econ releases **recur**: past CPI (Apr/May) and FOMC (Apr) markets were already settled on
**both** venues and reconciled **immediately** when the owner pushed back — CPI print-identity (both
venues' ladders imply the same 3.8 / 4.2) + FOMC categorical (both "maintains") = 5 rows, **0 diverge**,
empirically confirming the same-government-number basis weeks before the next print. (2) A throwaway
reconciliation script treated **every** pmus econ bucket as cumulative `≥` and manufactured **10 phantom
"divergences"** on CPI — because it **ignored the `ineq` field `econ_parse` already returns**. pmus
structures CPI as **exact-value buckets** ("CPI YoY = X.X%", one wins) while U-3/NFP are cumulative `≥`;
`econ_parse` tags these `==` vs `>=`, and `econ_colisted` correctly **skips** the `==` ones
(`point_bucket`). The production matcher was *right*; the throwaway re-derived a wrong assumption the real
code never made, and for a moment it looked like a catastrophic settlement divergence.

**Rule:** for any **recurring** market (econ prints, monthly/quarterly releases), reconcile the **most
recent settled cycle now** rather than waiting — "not yet settled this cycle" is not "never reconciled."
And never re-derive in an analysis script a parse that a shared module already performs: call
`econ_parse` and **honor its `ineq`** (`>=` twin-join vs `==` exact-bucket vs `cat`), don't assume
orientation (L21 applied to *analysis*, not just the matcher). When a quick script disagrees with
production, **suspect the script first** — here all 10 "divergences" were the script's `≥` assumption, 0
were real. Interrogate the raw object before believing a divergence (L3/L18/L21/L23 — the reflex held: the
single-YES-bucket pattern indicted the parse, not the venues).
