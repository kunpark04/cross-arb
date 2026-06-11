# Audit G — Adverse selection + matcher integrity (readiness)

**Scope:** dimension G of the adversarial readiness audit. Two blindspots that can make a "real" edge
fake: (1) the cheap side is cheap because it is **informed** (you systematically fill the wrong leg);
(2) bad market **joins** manufacture phantom edges (invariant #2 / L1/L17/L21). Read-only: scripts +
public-API spot-checks; no code edited outside `scripts/_data/` dumps and this file.

**Data basis:** local pull `…/data/cross-arb/` — **60,363** transition records loaded (after the 0013
econ quarantine; **33,568 post-epoch** at t ≥ 1781082189, detection-time stamps + `px` + `depth`).
(The brief's "62.6k" is a fresher droplet pull; this local copy is 60.4k — directions identical.)
Live discovery + book reads taken 2026-06-11.

**Verdicts (one line each):**
- **Toxic-close haircut:** overall **benign** (46% toxic = cheap-side is *more often* the stale laggard).
  BUT the **fattest-edge subset is materially MORE toxic** — gap ≥8¢ = **66% toxic vs 47% body (z=3.4),
  vs 31% on the thinnest bin (z=6.1)**; capturable open_net ≥10¢ = 68% toxic. Fat edges also **die
  faster** (median dur 1.6s→0.5s). The haircut on a real edge is dominated by **naked-leg risk**
  (24–28% leg-fail @150ms; 62% @1s on the fat subset), not price-degradation of the fills you get.
- **Matcher / invariant #2:** **NO false positive** in **43 live pairs** across all 3 categories
  (10 weather / 20 sports / 13 econ). Every join verified event-identity-correct against both venues'
  primary fields. L21 econ off-by-one fix is **verifiably live**.
- **Weather leg-sequencing:** signal **HOLDS and strengthens** (probe z=4.58 → **z=−13.1** here);
  it means you can **avoid** the toxic leg by sequencing, **not** that weather fills are toxic by default.
- **Artifacts:** this file + `scripts/_data/auditG_*.json` + the 3 throwaway analysis scripts in
  `scripts/_data/`.

---

## 1. Adverse selection — toxic-close share, the haircut, and the fat-edge question

`scripts/adverse_selection.py` attributes every OPEN→CLOSE pair: **CHEAP-ROSE** = cheap quote was
stale-low (you'd miss the cheap leg, benign laggard) vs **DEAR-FELL** = the dear side receded toward
the cheap quote (the cheap quote was the *correct/informed* price; you fill the wrong leg, miss the
right one) = **TOXIC**.

**Overall (post-epoch, n=5,180 attributed pairs):** 54% cheap-rose / **46% dear-fell (toxic)**.
Symmetric-to-benign — the cross-venue gap is *more often* a benign line-lag than an informed wide
quote. By category: weather **34%** toxic (benign), sports **48%**, econ n=4 (no release in window).
*(Full corpus identical: 46% toxic.)*

### The fattest-edge subset IS more toxic (the U-3-style informed-wide-book pattern)

Binning toxic-close share by the **opening quoted gap** (gross cross-venue edge; `auditG_toxicity_bins.json`,
post-epoch all-pairs):

| gap bucket | n | toxic share |
|---|---|---|
| 0–1¢ | 543 | 31% |
| 1–2¢ | 1109 | 49% |
| 2–3¢ | 1403 | 47% |
| 3–5¢ | 1903 | 46% |
| 5–8¢ | 137 | 45% |
| **≥8¢** | **85** | **66%** |

The tail turns up sharply: **gap ≥8¢ is 66% toxic vs the 1–5¢ body's 47% (z=3.40)** and vs the
thinnest bin's 31% (z=6.14). On the **capturable** cohort (open_net ≥1¢, c2≥1; `auditG_fat_haircut.json`)
the same monotone tail holds: 1–2¢ = 50% → 5–10¢ = 56% → **≥10¢ = 68%** toxic, fat (≥5¢) = **60%**
(z vs thin = 1.76–1.93, n-limited but consistent). **This is exactly the blindspot the audit names:**
the widest quoted gaps are disproportionately the dear leg about to correct toward an informed cheap
side — a fat number that is *least* likely to be a free lock.

**Realized-vs-quoted haircut.** For the fills you *do* get the realized edge barely degrades (survivor
median ≈ quoted: ALL-capturable quoted 1.9¢ → realized@150ms 1.9¢; fat ≥5¢ quoted 8.3¢ → realized
7.6¢) — because survivors are the benign episodes. **The haircut shows up as naked-leg risk, not price
slippage:** at 150ms entry, **24% of capturable edges leg-fail** (open episode CLOSEs before both legs
land → you hold a naked directional leg); at 1s, **50%**. The fat subset is **doubly penalized** —
more toxic AND shorter-lived (median duration **0.42–0.55s** for ≥5¢ vs 1.6s for 1–2¢) → leg-fail
**28% @150ms / 62% @1s**. So a "9¢ edge" is realistically discounted by the ~25–30% chance you take it
naked (and on the toxic 60% of those, the naked leg is the *losing* side). Net: **the realized edge on
the fat subset is not 8¢ — it is ~8¢ × ~0.72 survival, and conditioned on a 60% toxic-close rate the
naked tail is adverse.** This corroborates §2 of the probe (taker not dead at ~100–150ms RTT) but adds
the fat-edge caveat: the biggest numbers carry the worst selection.

> **Live confirmation of the pattern.** `scan_all.py` right now finds **exactly one** positive edge in
> the entire co-listed universe: **econ/U-3 `≥4.2` = +9.2¢, $18.40, n=1** (`urc-…-atl4pt2` ↔
> `KXU3-26JUN-T4.1`). The join is **correct** (verified §3). But Kalshi prices P(U-3>4.1%) ≈ **86%**
> (yes 0.86/0.87) while pmus prices the settlement-identical `≥4.2` at **bid 0.60 / ask 0.75** — an
> **~18¢ mid divergence between two markets that must settle identically**, on books **3 weeks before
> the 07-02 print**. The pmus book is 15¢-wide and sticky; the Kalshi book is gapped (YES levels jump
> 0.37→0.63→0.86). This is the textbook informed-wide-book / stale-thin-book artifact, not a
> demonstrated lock — and it is the *only* live edge, i.e. today's entire opportunity set is the single
> highest adverse-selection-risk class. **The >40¢ L1 price-sanity guard does not fire on econ; an
> 18¢ twin-mid divergence is a flag the current guards do not catch (see §5).**

## 2. Weather leg-sequencing signal — holds, strengthens, and is an AVOIDANCE lever

The probe (§5) flagged a small-cell weather signal (cheap-made 18% vs dear-made 79% toxic, z=4.58).
On the full corpus it is **far stronger and robust** (`auditG_toxicity_bins.json` → `weather_sequencing`,
post-epoch cross-tab):

| at-open class | cheap_rose | dear_fell (toxic) | toxic % | n |
|---|---|---|---|---|
| **cheap_made** (cheap side created the edge) | 372 | 82 | **18%** | 454 |
| **dear_made** (dear side created the edge) | 60 | 120 | **67%** | 180 |

**z = −13.1**, spanning **23 markets across all 5 cities** — not a small-cell fluke. Interpretation:
- **cheap_made → 18% toxic:** the cheap quote is a benign laggard that will *rise to meet* the dear side
  → your cheap leg is the one that fills; safe.
- **dear_made → 67% toxic:** the dear side moved away and will *recede back* → the cheap quote was the
  informed/correct price; the **dear leg** is the mover you'll miss.

**This is a leg-SEQUENCING signal, not "weather fills are toxic."** Overall weather is **34%** toxic
(the *most* benign category). The actionable read: on a `dear_made` weather open, **take the dear
(moving) leg first**; on `cheap_made`, take the cheap leg first. It says *how to fill*, never *avoid
weather*. (Subgroup-after-a-null per L19 — pre-register before trading on it, but the n and z here are
already well past hypothesis-grade.)

## 3. Matcher audit (invariant #2) — 43 live pairs, ZERO false positives

`scripts/_data/auditG_matcher_verify.py` sampled co-listed pairs across categories and verified each
join against **both venues' live primary fields** (`auditG_matcher_verify.json`). Live discovery
(`bot/colisted_map.py --live`): **58 weather + 224 sports + 13 econ** pairs, **0 coverage gaps,
0 bucket-misaligned, 0 fetch errors.**

- **Sports (20 checked, all leagues): all clean.** Every pair `same_event=True` (both Kalshi tickers
  share one `event_ticker`), and the pmus team names map to the two Kalshi `yes_sub_title`s by the
  correct join (abbrev for team/esports, surname for tennis/UFC). Stress cases all resolved correctly:
  apostrophe (`Sean O'Malley`→OMA), multi-word esports (`Vivo Keyd Stars`→VKS), and the **L1 trap**
  `Sophie Llewellyn vs Lea Ma` — Kalshi ticker suffix is the cosmetic `MAX` but the matcher correctly
  joins on the self-labeling `yes_sub_title` surname ("ma"), not the suffix. No ambiguous-city /
  prefix-collision pair found (smatch's ≤1-char-prefix guard + exact-event binding holds).
- **Weather (10 checked): all bounds identical.** Every pmus slug bound = Kalshi canonical
  `(floor,cap)` across `less` / `between` / `greater` strike types (e.g. `gte75lt76f`=[75,76]=`B75.5`
  "75° to 76°"; `lt71f`=(−∞,70]=`T71` "70° or below"). The L17 inclusive/exclusive canonicalization is
  correct live.
- **Econ (13 checked = all live pairs): twin join verifiably correct.** Every U-3 pair is the **strict-`>`
  off-by-one-corrected** partner per L21: `≥4.2 ↔ Above 4.1%` (`strike_type=greater`, floor=4.1),
  `≥4.6 ↔ Above 4.5%`, etc. — confirmed against Kalshi `rules_primary`/`yes_sub_title`. Fed 5-way
  categorical maps label==label exactly (`maintains→H0/"Fed maintains rate"`, `cut25bps→C25`, …).
  Discovery correctly **skips 13** `ge_no_identical_twin` extremes (no Kalshi `T−0.1` strike) rather
  than mispairing them — the L21 phantom is structurally closed.

## 4. Fat edge × join correctness cross-check

The classic phantom is a fat edge on a bad join. **The one live fat edge (U-3 +9.2¢) sits on a
PROVABLY-CORRECT join** (§1 box, §3) — so it is *not* a join phantom (L21 is fixed). Its risk is the
*other* axis: settlement-identical twins with an 18¢ mid divergence on thin/stale 3-weeks-out books =
the adverse-selection / stale-book risk, which §1's toxicity tail predicts is exactly where fat econ
edges are most dangerous. The historical fat phantoms are gone: the 37.7¢ ITF book-init phantom (L20)
and the 12.2¢ U-3 off-by-one (L21) do not appear (restart-censor drop + 0013 quarantine + twin remap
all confirmed active in the loader). No fat edge in the sample maps to a bad join.

## 5. Blindspots — what toxicity / join-correctness CANNOT be seen without live order data

1. **True fill toxicity needs trade prints, not transition snapshots.** Close-attribution infers "which
   venue moved" from the monitor's >ε transition grid (Kalshi printed ~23.8k weather trades/24h vs ~220
   visible crossings — 2 orders of magnitude undersampled). Whether *you* would have been adversely
   filled (queue position, partial fills, who hit whom) is unmeasurable until the new `trades-*.jsonl` /
   `ladders-*.jsonl` accumulate post-redeploy. Current toxicity is a directional proxy, upper-bounded.
2. **Naked-unwind cost is the real go/no-go term and is unmeasured.** The haircut here counts leg-fail
   *rate*; the *cost* of unwinding a naked leg (the breakeven ~4–6¢) needs live fills.
3. **The econ twin-mid-divergence guard gap.** The L1 >40¢ sanity guard does not fire on econ, and an
   18¢ divergence between *settlement-identical* twins is not flagged anywhere. On thin pre-release
   books this is mostly a stale-quote artifact, but the bot has **no guard that says "settlement-
   identical twins should not disagree by 18¢ — treat as stale/illiquid, not as edge."** Worth a
   twin-consistency tripwire before econ goes live (cleanest first datapoint 06-18 FOMC / 07-02 U-3).
4. **Adverse selection at settlement (the void/correction tail) is a different toxicity** not captured
   here (sports postpone-void, weather 8–11am CLI downward revision) — covered by other audit dims.
5. **Survivorship in the capturable cohort:** the benign episodes are the long-lived ones, so any
   "survivor realized edge ≈ quoted" understates the cost — the toxic episodes are precisely the ones
   that leg-fail and drop out of the survivor median. The honest cost lives in the leg-fail rate, which
   is why fat (short, toxic) edges are worse than their realized-median suggests.

---

### Files

| File | What |
|---|---|
| `scripts/_data/auditG_adverse_all.json` / `_postepoch.json` | `adverse_selection.py` full + post-epoch output (close attribution + direction gate) |
| `scripts/_data/auditG_toxicity_bins.{py,json}` | toxic-close share binned by opening gap (overall + per cat) + weather sequencing on full corpus |
| `scripts/_data/auditG_fat_haircut.{py,json}` | fat-subset shadow-fill: toxicity × duration × leg-fail by edge magnitude (capturable cohort) |
| `scripts/_data/auditG_matcher_verify.{py,json}` | 43-pair live identity verification across all 3 categories (0 flagged) |
| `scripts/_data/auditG_pairs.json` | full structured discovery dump (58 weather / 224 sports / 13 econ) |
| `scripts/_data/auditG_colisted_live.txt`, `auditG_scan_all_run.txt` | live discovery + scan_all run logs |

**Bottom line for the deployment decision:** matcher integrity is **clean** (0/43 false positives;
L1/L17/L21 fixes verified live) — invariant #2 is not the risk. Adverse selection is **benign in
aggregate but concentrated in the fat tail**: the biggest quoted edges (≥8¢, and the only live edge —
U-3) are 60–68% toxic and shortest-lived, so the realized edge on exactly the numbers that look most
attractive is the most discounted by naked-leg risk. Two protective levers exist: the **weather
leg-sequencing rule** (z=−13.1, robust) lets you take the moving leg first; and an **econ
twin-consistency tripwire** (not yet built) would stop an 18¢ stale-book divergence from reading as a
$18 lock. None of this needs capital — it needs the trade-print logging + the first clean econ
settlement (06-18) to convert the proxies into measured fill toxicity.
