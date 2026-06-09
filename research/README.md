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
| 10 | [settlement-verification.md](settlement-verification.md) | **Live rules diff (2026-06-09)** — both venues' weather markets grade off the *same* NWS CLI Daily + *same* station (low-tail boundary spot-checked; middle buckets + inequality/rounding alignment still open; timing still open); resolves the source/station settlement-identity risk. Probe: `scripts/verify_settlement.py` |

## The one-line takeaway

US-legal overlap = **econ + weather + sports** (politics too); all grade on the *same* deterministic
source as Kalshi, which makes them settlement-clean. **Econ** (CPI/U-3/GDP/NFP/Fed) **is live and
US-legal on polymarket.us** — a live pull found 36 macro markets settling on the same BLS/BEA/Fed
prints Kalshi uses — and is the structurally cleanest subset, though episodic. **Only crypto** is
genuinely US-blocked (international-only). (Corrects an earlier "econ is international-only" claim;
crypto IS correctly blocked.) Full reasoning in `us-legal-overlap-audit.md`.
