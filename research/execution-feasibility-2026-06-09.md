# Execution feasibility — read-only empirical tests (2026-06-09)

The thesis "an arb is structurally +ROI; losses are edge cases" is only true for a **completed, held** pair. What
decides realized ROI is **fills + settlement**. These four read-only tests (no capital, no orders) measure what we
can of that, before building any trade engine. Scripts under `scripts/`; each has an offline `--selftest`.

## 1. Read-path latency — MEASURED (`scripts/latency_probe.py`)

REST GET round-trip to each venue (40 samples, dev host, residential internet) — a **lower bound** on order latency
(your order can't beat your data path):

| venue | p50 | p90 | p99 | min |
|---|--:|--:|--:|--:|
| Kalshi | 75 ms | 168 ms | 195 ms | 58 ms |
| pmus | 86 ms | 135 ms | 261 ms | 66 ms |

Two-leg execution floor: **SERIAL** (leg A ack → leg B) p50 161 / p99 455 ms; **CONCURRENT** (both at once) p50 86 /
p99 261 ms. **Verdict: this is a tens-to-hundreds-of-ms game, network-dominated — ~4–5 orders of magnitude above
per-op compute, so Rust-vs-Python compute latency is noise.** Fire both legs concurrently (halves the floor). See
[latency-playbook.md](latency-playbook.md). *Caveat:* dev host, not the droplet (which would be lower/tighter); no
keep-alive (a warm connection lowers it further); read-path, not the auth'd order path — so a true lower bound.

## 2. Leg-fill risk (shadow-fill) — PARTIALLY MEASURED, key regime still unresolved (`scripts/shadow_fill.py`)

Replays the edge trajectory: if both legs land after entry latency `L`, what survives? (158 capturable episodes, 0.33 d.)

| L | fill-survival | median realized edge | leg-fail (naked) |
|--:|--:|--:|--:|
| 0 | 100% | 1.95c | 0% |
| 1 s | 61% | 1.96c | 39% |
| 2 s | 33% | 1.59c | 67% |
| 10 s | 20% | 1.95c | 80% |

**Latency kills your hit RATE, not the size of the hits you keep** (survivors stay ~1.6–2.0c — survivorship toward
longer-lived edges). A ~2s entry leaves you **naked-legged 2-of-3 times**. **Critical limitation:** the archive's
timestamps were **integer seconds**, so the sub-second regime — exactly where the measured ~86–261 ms order latency
lives — is **below the data's resolution** (the flat 100% at L≤0.5s is an artifact, not safety). **The single most
important number — your real leg-fill rate at ~150 ms — is still unmeasured.** Fix applied: `bot/monitor.py` now logs
**millisecond** transition timestamps, so a re-pull after the next deploy makes the sub-second curve measurable.

> ## ⚠ CORRECTION (2026-06-10 full review, [0013](../decisions/0013-econ-grid-step-twin-and-measurement-integrity.md))
>
> The table above is **optimistically biased beyond the stated caveat**: the monitor's FlipDebouncer
> stamped every flushed CLOSE at **flush time** (+1.0–1.5 s after the edge died), inflating every episode
> duration — so "survived L=1 s" really meant "true duration ≳ 0 s". This also means **no logged clean
> episode could ever show a duration < ~1 s**, structurally defeating the planned sub-second read on the
> ms-era data until the fix (CLOSEs now carry detection time; pre-fix data is lag-corrected −1.25 s in
> `analyze_persistence.build_episodes`). FLIPped episodes also counted as survival though the original
> legs are the wrong direction post-flip (now leg-fail). **Corrected table** (0.86 d archive, 418
> capturable ≥1¢ episodes, lag-corrected + FLIP=fail):
>
> | L | fill-survival | median realized edge | leg-fail (naked) |
> |--:|--:|--:|--:|
> | 0 | 72.5% | 1.97c | **27.5%** |
> | 1 s | 44.5% | 1.91c | **55.5%** |
> | 2 s | 37.3% | 2.01c | **62.7%** |
> | 10 s | 20.8% | 1.66c | 79.2% |
>
> ~27% of capturable ≥1¢ episodes die essentially **instantly** (their entire pre-correction "duration"
> was the flush lag). The L=0.25/0.5 rows still equal L=0 — int-second-era resolution; the sub-second
> curve becomes measurable only with post-redeploy ms data **collected by the fixed debouncer**.
>
> **First post-0013 sub-second read (16 h, 2026-06-11, [probe-program brief](probe-program-2026-06-11.md) §2):**
> naked-leg **9.4% @50 ms / 17.0% @100 ms / 23.3% @150 ms / 29.4% @250 ms** (n=575 capturable ≥1¢); survivors
> keep ~1.9¢ median; ~29% of episodes die <250 ms (confirming the "~27% instant" share); the 1 s / 2 s rows
> soften ~8 pp (47.3% / 55.0%). At the measured RTT, naked taker execution is **not rejected** — breakeven
> naked-unwind ≈4–6¢ vs ~1–3¢ plausible. Preliminary (one sports-heavy day).

## 3. Settlement reconciliation — INCONCLUSIVE + a new pmus finding (`scripts/settle_recon.py`)

> **CORRECTED 2026-06-11 ([probe-program brief](probe-program-2026-06-11.md) §4, [L23]):** the two
> "unreliable interim data" bullets below were **our parse bug**, not a venue fact — pmus `outcomes[]`/
> `outcomePrices[]` are **not index-aligned** (prices follow `marketSides` order; the self-labeling
> `marketSides` is authoritative — validated 286/286 raw objects). Under the fixed reader the weather recon
> is **CLOSED: 360/360 settled buckets identical, three-way vs the NWS CLI**, and sports interim reads are
> 56/56 == Kalshi. What **survives** below: `closed:true` ≠ finalized (sports `endDate` ≈ D+14 — still treat
> pre-`endDate` sports reads as interim) and the absent settlement timestamp. The ATP Diallo–Mannarino case
> is retro-unadjudicable (today it reads correct under both conventions).

Empirical test of invariant #1 (do both venues grade a co-listed market identically?). **It is not yet answerable**,
and finding out *why* is itself the result:
- **pmus `closed:true` ≠ finalized.** Every one of the 46 compared sports pairs has a pmus `endDate` ~2 weeks in the
  **future**; pmus serves an **interim/placeholder `outcomePrices`** until then. The 65% "match rate" is **not** a
  settlement measurement and must not be cited as one.
- **pmus's pre-finalization public data is unreliable.** One externally-verified case (ATP Diallo vs Mannarino,
  s-Hertogenbosch) had pmus's interim winner **wrong** (Mannarino won; Kalshi correct). Weather was worse: pmus MIA
  06-08 returned "Yes" for **four disjoint buckets** (impossible); Kalshi had the correct single bucket.
- **Implication:** you cannot read a pmus outcome immediately — there's a **~2-week finalization lag**, and the public
  gateway's interim settled data can't be trusted. Re-run `settle_recon.py` after pmus markets pass `endDate`.

So invariant #1 is **rules-text-verified for weather, empirically unconfirmed for both** — and the pmus finalization
lag is a new operational fact (capital-recycling + settlement-confirmation both wait on it).

## 4. Adverse selection (which side moves on close) — INSTRUMENTED, accruing (`scripts/adverse_selection.py`)

"Why is the cheap side cheap?" — attributes each edge's close to **cheap-rose** (benign laggard) vs **dear-fell** (the
cheap quote was *informed*; you'd miss the dear leg = toxic). Needs the per-venue touch prices, now logged by
`bot/monitor.py` (`px` field, added this session). Current archive predates the field → 998 weather episodes, 0 yet
attributable; produces real numbers after the next deploy + re-pull.

## 5. Capital velocity / early-exit — MEASURED: no early-exit on pmus (`scripts/exit_liquidity.py`, `capital_velocity.py`)

The capital case hinged on early-exit: once the outcome is known, sell the winning leg at ~$1 and redeploy rather
than wait for the far-future pmus finalization (`endDate`). **Measured — and it's structural / venue-timing-driven, opposite for sports vs weather:**
- **Sports:** pmus **freezes the book AT resolution** (`closed=true` at game-end) — **10/10 resolved 06-08
  markets had empty books** (the one with a live book was still `closed=false`). So you cannot sell the winning
  leg; capital is locked from resolution to `endDate` (~**15 days**). The early-exit rescue is **refuted** —
  sports is capital-inefficient, deep but slow.
- **Weather:** the OPPOSITE. pmus closes weather markets **late** (`endDate` 1 AM local — corrected 2026-06-11, was "~2 AM ET" — well after the ~6 PM
  high-lock), so there's an **~8-hour evening window** where the outcome is known AND the book is live. Measured
  the winning bucket at ~8 PM ET 06-09: **MDW 88-89°F bid 0.99 (depth 22,340), LAX 72-73°F bid 0.98 (depth
  3,273)** — a deep bid near $1; losing buckets sit at 0.01/no-bid. So weather **has a liquid early-exit** (~1-2c
  discount). Combined with its ~1.2-day natural settlement, **weather is doubly capital-efficient.** The standout.
- **Econ:** weeks-to-months, no early-out (outcome known only at the far-future release).

`capital_velocity.py` quantifies the consequence: velocity, not edge, separates the categories (per-turn RoC is
~equal; monthly RoC diverges by lockup). And early-exit only helps when **edge > exit-haircut** anyway — at the thin
median edges (1.6–2c) a ~2c exit would wipe the edge even if a book existed.

## Net

Latency is measured and benign-for-language-choice; **leg-fill is the gating risk and its true magnitude is now
*instrumentable* but not yet measured** (ms timestamps + the shadow-fill replay); settlement identity is
**empirically CONFIRMED for weather (2026-06-11: 360/360 — the "unreliable interim" scare was a parse bug, [L23])**
and rules-verified for sports/econ pending their finalization windows. None of this needs capital —
it needs the next gated deploy of the (now ms-resolution, px-logging) monitor, a few weeks of data, and a re-run.
