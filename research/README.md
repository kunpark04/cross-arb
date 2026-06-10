# research/ — evidence base

The decision-grade briefs behind the project. Scripts in `../scripts/` produce raw data; conclusions
worth keeping get promoted into a brief here. Read in the order below — it goes from "is settlement
even compatible?" down to "here's a measured live edge."

> **Freshness note:** `settlement-map.md` is **partially superseded** by `us-legal-overlap-audit.md`
> (which used live API pulls). Where they disagree, trust the audit. Each brief carries its own date
> and a Sources section.

## Reading order

| # | Brief | Covers |
|---|---|---|
| 1 | [settlement-compatibility-matrix.md](settlement-compatibility-matrix.md) | Which market families settle on the *same* number vs divergent sources (NWS vs Wunderground, UMA, etc.) |
| 2 | [kalshi-venue-audit.md](kalshi-venue-audit.md) | Kalshi API, account, fee structure, settlement sources |
| 3 | [polymarket-venue-audit.md](polymarket-venue-audit.md) | International Polymarket (.com) API + settlement — the **blocked/divergent** venue |
| 4 | [polymarketus-catalog-settlement.md](polymarketus-catalog-settlement.md) | polymarket.us (QCX) catalog + CFTC deterministic settlement rulebook |
| 5 | [polymarketus-api-auth.md](polymarketus-api-auth.md) | polymarket.us auth (Ed25519) + retail-vs-institutional API surfaces |
| 6 | [us-legal-overlap-audit.md](us-legal-overlap-audit.md) | **Live API pulls** — corrects earlier settlement assumptions; the freshest legality map |
| 7 | [settlement-map.md](settlement-map.md) | Decision-grade settlement-identity × US-legality map per family *(partly superseded by #6)* |
| 8 | [backtest-feasibility-and-prior-art.md](backtest-feasibility-and-prior-art.md) | Historical-data availability + backtest design + prior art |
| 9 | [live-edge-findings.md](live-edge-findings.md) | **First validated** cross-venue weather scan — fill depth + net edges |
| 10 | [settlement-verification.md](settlement-verification.md) | **Live rules diff (2026-06-09, updated)** — both venues' weather markets grade off the *same* NWS CLI Daily + *same* station; boundary equality now **verified** on a low-tail AND a middle 2° bucket (and enforced in `colisted_map.py`); **timing resolved** (pmus FAQ specifies 8 AM / 11 AM-if-CLI≠METAR — primary-source; downward-correction asymmetry narrowed, not eliminated). Probe: `scripts/verify_settlement.py` |
| 11 | [sports-settlement-verification.md](sports-settlement-verification.md) | **Live sports rules diff (2026-06-09)** — normal completed games agree, but the **void/abandonment/reschedule tail diverges** (esports abandonment → pmus "last fair price" vs Kalshi silent; tennis >2wk reschedule → pmus $0.50). Sports is settlement-clean only for games expected to complete. Probe: `scripts/verify_sports_settlement.py` |
| 12 | [execution-feasibility-2026-06-09.md](execution-feasibility-2026-06-09.md) | **Read-only empirical tests** — latency (~86–261ms, network-bound), shadow leg-fill (hit-rate collapses with latency; **corrected 0013**: leg-fail 55.5% @1s / 62.7% @2s once the debouncer flush-stamp lag is removed; ~27% die ~instantly), settlement reconciliation (inconclusive + pmus finalization-lag/unreliable-interim finding), adverse-selection (instrumented). Probes: `latency_probe`/`shadow_fill`/`settle_recon`/`adverse_selection.py` |
| 13 | [allocation-policy-2026-06-10.md](allocation-policy-2026-06-10.md) | **Clip-stage allocation, tested OOS** — FIFO loses; the lever is a **2¢ edge floor + a deploy-to-full per-pair cap** (**corrected 0013: ~+9–141% over FIFO OOS**, 10 diversified pairs — the published +64–269% contained an econ settlement-phantom); clip-cap-alone is risk-control not PnL; the "wait 1 s + sort" idea captures only ~4% of the gap. Records the phantom that was 75% of the in-sample headline + the `capturable()` fix ([0012], [L20]). **Method demo on <1 d.** Scripts: `alloc_policy_experiment.py`/`clip_threshold_test.py` |
| 14 | [econ-settlement-identity-2026-06-10.md](econ-settlement-identity-2026-06-10.md) | **Econ settlement identity VERIFIED — and the 0011 pairing was off by one bucket**: pmus `≥T` (inclusive) vs Kalshi "Above T" (strict) differ on an exact-T print, the modal region ATM; the observed 12.2¢ U-3 "edge" was the market-priced P(print==T). The identical twin is `floor = T − step` ([0013]); 24 pairs → 14 identical + 13 honest skips; pre-remap econ data quarantined. Probe: `verify_econ_settlement.py` |
| — | [latency-playbook.md](latency-playbook.md) | Engineering playbook: 18 latency-reduction levers ranked by leverage (location+connections+concurrency dominate; Rust/C++ is Tier 4, deferred until edge proven) |

## The one-line takeaway

US-legal overlap = **econ + weather + sports** (politics too); all grade on the *same* deterministic
source as Kalshi. **Econ** (CPI/U-3/GDP/NFP/Fed) **is live and US-legal on polymarket.us** — a live
pull found 36 macro markets settling on the same BLS/BEA/Fed prints Kalshi uses — but "same print"
does **not** mean any threshold pair is identical: pmus `≥T` matches Kalshi `>T−step` (the grid-step
twin, brief #14 / [0013]), giving **14 settlement-identical econ pairs**, and the per-family rules
text must be verified before pairing (lesson [L21]). **Only crypto** is genuinely US-blocked
(international-only). (Corrects an earlier "econ is international-only" claim; crypto IS correctly
blocked.) Full reasoning in `us-legal-overlap-audit.md`.
