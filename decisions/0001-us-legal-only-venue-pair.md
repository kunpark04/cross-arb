# 0001 — US-legal venues only (Kalshi × polymarket.us), identical settlement required

- **Date:** 2026-06-08
- **Status:** Accepted
- **Deciders:** Project owner + Claude

## Context

The seed idea (`miami-temp-arb.html`) compared Kalshi against *international* Polymarket
(polymarket.com) and showed a fat ~24¢ weather spread. Two problems surfaced in the venue audits:
(1) international Polymarket is **geoblocked for US persons** (2022 CFTC settlement + ToS), so a US
person can't legally trade it; (2) the fat spread came from **divergent settlement sources** — Kalshi
grades on NWS CLI, intl Polymarket on Weather Underground (and many markets via the UMA optimistic
oracle). Two different numbers means the "arb" can lose *both* legs — it's a directional bet in
disguise. The cleanest-settling families (CPI/FOMC/GDP/crypto, which grade on identical government
prints) exist **only** on the blocked international venue.

## Decision

Scope the project to **US-legal venues only: Kalshi × polymarket.us (QCX)**. Treat a cross-venue
gap as an arb **only when both venues grade off the same named deterministic public number** (same
source, same station, same bucket boundaries).

## Alternatives considered

- **Include international Polymarket** for the fat econ/crypto/weather spreads — rejected: illegal for
  US persons and settlement-divergent (can lose both legs). Out of scope entirely.
- **Trust large spreads as edge signals regardless of source** — rejected: a big gap between
  differently-graded venues is evidence *against* an arb, not for one (see lesson L2).

## Consequences

- The tradable universe is the US-legal overlap: **econ + politics + weather + sports** (see the
  2026-06-08 update below — the earlier "collapses to weather + sports" framing was wrong). Edges are
  smaller and cleaner than the fat divergence engine, which is gone by construction.
- Creates the project's **invariant #1 (settlement identity)** that the matcher, scanner, and ledger
  all assume. `bot/ledger.py`'s additive/outcome-independent guarantees depend on it.
- **Revisit if:** polymarket.us adds politics/econ/crypto with Kalshi-identical settlement (would
  reopen the clean families to US persons) — tracked in `research/settlement-map.md` due-diligence list.

## Update 2026-06-08 (live data)

The "Revisit if" trigger above is **already satisfied** for econ + politics. Live
`gateway.polymarket.us/v1` pulls found **econ (CPI / U-3 / GDP / NFP / Fed, 36 live macro markets)**
and **politics** ARE listed and US-legal on polymarket.us, each econ market grading on the same
BLS/BEA/Fed print Kalshi uses (see `research/us-legal-overlap-audit.md`). So the Context/Consequences
claim that the universe "collapses to weather + sports" is **incorrect**: the US-legal universe is
**econ + politics + weather + sports**, with **crypto the only still-US-blocked family** (international-
only). The decision itself stands unchanged — invariant #1 (require an *identical* settlement source on
both venues) is exactly what makes the now-confirmed econ overlap clean; only the scope of which
families clear that bar has widened.
