# Deployment-readiness audit — 2026-06-11

**Question put to the audit:** test everything as accurately as possible on the data we have right
now; don't miss any blindspots. Is the strategy ready for live-capital deployment, and would a bot
make consistent profits if turned on today?

**Method:** seven independent adversarial reviewers, each owning one dimension, each instructed to
hunt blindspots and report honest numbers with sample sizes, reusing the shared loaders/gates (not
re-implementing). Per-dimension working notes in `tasks/_agent_bus/20260611-readiness/`.

**Data basis (corrected):** **33,568 post-0013-epoch transitions / 19.83 h** (detection-time ms + px
+ depth). NOTE the earlier "62.6k" was a double-count — the 06-10 late-append raw file shadows its
`.gz` and the loader dedups them; the raw census did not. Plus the lag-corrected, econ-quarantined
pre-epoch archive (~1.98 d total span).

## Verdict: NOT READY. A bot turned on today would not make consistent profits.

Seven independent passes converged on the same answer, and not narrowly. The strategy's *foundations*
are sound (settlement identity for weather, matcher integrity, measurement discipline); the *evidence
base* is not remotely sufficient to deploy capital, and several frictions land in the bad direction.

### The single dominant fact: effective n ≈ 1 day

After one-per-market dedup there are **269 candidate arbs, of which 246 (91%) fall on a single
event-date (06-10)** — the only fully 24 h-covered day. 06-11 is a ~10 h overnight stub that produced
**zero** clean arbs (Audit A). Only **28 candidates clear the τ=2¢ floor** — below the pre-registered
analysis's own ≥30 minimum — and there are **0 complete temporal folds** vs the **≥7** the 0014
protocol requires (Audit F). For an edge-*existence* claim the honest unit is the arrival-day, so
**n = 1**. Everything below compounds downward from that.

### What the seven audits found

| Dim | Finding | Headline number |
|---|---|---|
| A — edge reality | A tiny clean edge survives every filter, but ~71% of apparent edge-¢ is phantom; 19 of the top-20 fattest episodes are artifacts (flat ladders, single-contract depth, book-init). | **16 episodes survive all gates** (≈11 after hand-adjudication); 70% of ≥1¢ edge dies <5 s |
| B — execution | Taker EV is positive at ≤261 ms **only if** the naked-leg unwind cost `u` is low — and `u` is structurally unmeasurable read-only. Indirect evidence is bad: logged pmus spread median **5¢** vs a 150 ms breakeven of **10¢**. | naked 16%/24%/32% @86/150/261 ms; breakeven `u` = 10/6.7/3.1¢ |
| C — settlement | **Weather empirically safe** (360/360 reconfirmed, no drift). **Econ source-identity now empirically reconciled on PAST recurring releases** (CPI Apr/May print-identity + FOMC Apr categorical = 5 rows, 0 diverge — both venues settle off the identical BLS/Fed number; added 2026-06-11 after the owner asked "reconcile now"). **Sports (224 pairs) still rules-text-only.** | weather VERIFIED; econ source VERIFIED (n=3 events); the U-3/NFP cumulative-twin instrument settles first 07-02; sports OPEN until ~06-23/25 |
| D — capital | Returns are lumpy and *fall* with size (bankroll never binds); ~70–96% idle then lumps; weather (the only fast category) walls out at ~$177/day of deployable capital. | $500 → +0.85%/window net of friction; $10k → +0.25%; top day = 62% of edge |
| E — costs | The cost model is materially incomplete in the bad direction. **The median (0.39¢) arb is friction-negative**; only the ≥1¢ weather cohort stays positive, and only at fast fills. **85% of apparent edges are sub-1¢ — uncapturable at the 1¢ tick.** | weather all-in +0.96¢→+0.36¢; sports +0.22¢→−0.41¢; median arb −0.69/−1.18¢ |
| F — stats | The methodology is sound; the data is ~1/21 of the prereg trigger. No valid CI on realized return exists; the booked-edge proxy CI **spans the sign** once clustering + naked-leg friction are subtracted. | effective n = 1 day; 28 clear τ (need ≥30); 0 folds (need ≥7) |
| G — toxicity/matcher | **No false-positive joins** in 43 live pairs (invariant #2 holds; L1/L17/L21/L23 fixed live). But **the fattest edges are the most toxic and die fastest** — the "larger arbs" are a trap. Weather sequencing signal strengthened (z = −13.1) → you *can* avoid toxic fills by sequencing. | ≥8¢ gap = 66% toxic vs 47% body; fat edges 62% naked @1 s |

### The three findings that most directly answer "consistent profits?"

1. **The bigger the arb, the worse it is.** (Directly answers the earlier "is it worth chasing the
   larger arbs" question.) Edges ≥8¢ are 66% toxic (informed cheap side) vs 47% for the body, and they
   die in ~0.5 s vs ~1.6 s — so the fat tail that drives the EV *mean* is exactly the part you most
   often fail to capture, and when you do capture it you tend to get the wrong side. The headline
   "+1.5–2.4¢/contract EV" leans on a tail that is a mirage (Audits B, G).
2. **The median arb doesn't clear its own costs.** All-in (taker fees + spread + slippage + leg-fill
   EV + carry), the median 0.39¢ arb is **−0.7 to −1.2¢**. Only ≥1¢ weather, fast-fill, single-digit-
   contract clips stay positive — a corner, not a business (Audit E).
3. **You'd be deploying on one day.** 91% of the signal is 06-10; the next day gave zero. There is no
   statistical basis yet to distinguish "real edge" from "one good weekday" (Audits A, F).

### What is genuinely solid (so this is calibrated, not doom)

- **Weather settlement identity is empirically nailed** (360/360, 3-way vs NWS CLI, reconfirmed this
  session with an independent raw-CLI spot-check) — the foundational both-legs-loss risk is closed for
  the one fast/clean category.
- **Matcher integrity holds:** no false-positive join in 43 audited live pairs; the L1/L17/L21/L23
  failure modes are all verifiably fixed on live data.
- **A usable execution finding:** the weather leg-sequencing signal (take the leg that moved first)
  lets you *avoid* the toxic side — weather is the most benign category (34% toxic) and sequencing
  sharpens that.
- **The infrastructure and discipline are deploy-grade** even though the edge isn't — the monitor,
  loaders, phantom gates, and pre-registration are doing their job (they're what caught all of the
  above).

### One new code gap found (actionable, read-only-safe)

The >40¢ price-sanity guard (L1) does not fire on econ, and nothing flags a mid-divergence between
two *settlement-identical* econ twins. The only live econ "edge" in the whole universe right now is
U-3 ≥4.2 at **+9.2¢ (n=1)** — an 18¢ mid disagreement between provably-identical twins on thin books
three weeks pre-release, i.e. a textbook stale/informed-book artifact, not an arb. A twin-mid-
divergence guard should exist before econ is ever traded. (Audit G.)

## What would actually change the verdict (the real path, not a time-gate)

None of this needs capital; it needs the measurements the read-only rig is built to produce:

1. **Multi-day data** — the 0014 confirmatory run needs **≥14 event-days** (we have ~1). This is the
   binding item and it is purely calendar-gated: the rig is already collecting it.
2. **Measure the naked-unwind cost `u`** — the single term the go/no-go pivots on, unmeasurable from
   transition logs. Needs shadow/IOC probe orders post-greenlight, or inference from the new
   `trades`/`ladders` streams now logging.
3. **Settlement reconciliation for sports + econ** — convert 224 + 13 rules-only pairs to empirically
   verified: sports ~06-23/25 (post-endDate), FOMC 06-18, U-3/NFP 07-03.
4. **A built execution layer + a paper-trading phase** — the bot trading live signals into a ledger,
   no money, reproducing the backtested weather edge before a dollar is risked. (No order code exists
   today; the working agreement forbids writing it pre-go.)
5. **Build the twin-mid-divergence econ guard** (above) before econ is in scope.

If a deliberately aggressive pilot is wanted the moment items 1–3 clear, the audits point to exactly
one defensible shape: **weather-only, ≥1¢-only, fast-fill-only, single-digit-contract clips, cheap-leg-
sequenced** — the sole corner where all-in EV stays positive — run as paper-then-micro before scale.

## Bottom line

The idea has merit and the foundations are real, but on one day of data, with the fat edges turning
out to be a toxicity trap, the median arb friction-negative, the decisive cost term unmeasured, and
sports/econ settlement unverified, **turning on a bot today would most likely churn near break-even-
to-negative after frictions — not consistent profit.** The gap to "ready" is measurement and weeks,
not effort — and the rig is already closing it.
