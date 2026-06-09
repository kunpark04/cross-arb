# Independent adversarial review — cross-arb (2026-06-09)

An independent agent was given **only** the strategy thesis + the repo path (none of our own findings)
and told to read all code + docs, verify every claim against the code, and flag assumptions / logical
errors / code errors / blind spots. It ran the self-tests and wrote its own probes. This is its report,
**with a Resolution line added to each finding** (what we did in response, same day).

**Reviewer's one-line verdict:** *"The engineering is unusually disciplined… but the thesis rests on one
settlement-identity assumption that the project's own research contradicts itself about and never closed,
and the headline 'edge' numbers are most consistent with stale/in-play books and unverified settlement,
not a clean arb. The economic model also omits the dominant real-world cost (one-leg fill risk)."*

## Biggest risks to the thesis (reviewer's ranking) + resolution

1. **Settlement identity unproven / self-contradicted (A1, L1).**
   → **RESOLVED (mostly).** Built `scripts/verify_settlement.py`; it pulls BOTH venues' live rules per
   city. Result 2026-06-09: source (NWS CLI Daily), station (incl. NYC=Central Park/KNYC), and sampled
   bucket boundaries are **identical** on all 5 cities — refuting the worst case. **Open:** settlement
   timing/revision (pmus states no time in the market object). See `research/settlement-verification.md`.
2. **Every profit figure assumes both legs fill, zero slippage (B1, A2).**
   → **PARTLY.** `capital_sim --haircut` exists; the leg-fill-failure model is still TODO (open item).
   `ledger.py` S5 already shows the naked-leg loss; no headline number yet incorporates fill-failure prob.
3. **Edge survives only where data is least trustworthy / no crossed-book or liquidity guard (B2, C3, L2).**
   → **RESOLVED.** Crossed-book rejection (C3) + per-transition staleness (`age`) + the `capital_sim`
   clean-fillable filter (liquidity floor + freshness) now separate fillable from stale-phantom.
4. **Fillability unmeasured — no venue-to-venue latency (B3, L5).**
   → **PARTLY.** The monitor now logs per-venue book `age` (staleness) at each transition; a dedicated
   order-ack latency study is still TODO.
5. **Conclusions outrun data + fee/capital model wrong (B7, C1, L3/L4).**
   → **C1 FIXED** (per-order fee + float-ceil bug). Overconfident prose tempered (CLAUDE.md/README). Cost
   of carry (L4) still omitted from `capital_sim` (open item).

## Confirmed code bugs (reviewer verified each by running probes)
- **C1 — Kalshi fee per-contract not per-order** → **FIXED.** `kfee(p, n)` rounds the whole order up once;
  also fixed a float-imprecision ceil overshoot (175.0000003 → 176). `ledger.enter/mtm/unwind_all` pass size.
- **C2 — `ledger.enter()` booked guaranteed losses** → **FIXED.** Refuses net ≤ 0 (raises) unless `force=True`.
- **C3 — no crossed/locked-book rejection (phantom arbs)** → **FIXED.** `signal`, `make_px`, `game_edge`
  reject a strictly-crossed venue book; depth now uses the signalled cross-venue direction (no same-venue pairing).
- **C6 — seq-gap resync unrecorded** → **FIXED.** A Kalshi resync writes a `kalshi_resync` marker to
  `sessions.jsonl`; the analyzer force-closes episodes there (censors the spurious re-OPENs).
- C4/C5/C7-C9 (no live mid-divergence guard; WS-snapshot-vs-delta assumption; pagination cap; stale-dated
  `weather_arb_scan`; first-date `event_partition`) — **OPEN** (lower severity; tracked in `tasks/todo.md`).

## Blind spots (still open unless noted)
- **B1 leg-fill risk** — model the one-leg-fill probability in the capital numbers (TODO).
- **B2 adverse selection / competition** — the edge lives where fills are worst; keep the clean-fillable lens.
- **B3 latency budget** — measure order-ack latency both venues (the `age` field is the first half).
- **B4 settlement timing mismatch** — the remaining half of the settlement check (pmus rulebook).
- **B5 capital lockup across longer-dated markets**, **B6 venue/counterparty/regulatory risk on a 6-mo-old
  venue**, **B7 small sample**, **B8 trust "live-verified" claims** — all OPEN; honest caveats added to docs.

## What the reviewer found solid (verified correct)
`ledger.py` invariant proofs (additivity, outcome-independence, hold-don't-panic-unwind); `KalshiBook`
merge + `SeqTracker`; the `depth_curve` walk; `peak_and_avg`; event-date partitioning + restart-censoring;
and the overall discipline (pure self-tested cores, loud coverage audit, least-privilege key, gated deploy,
verify-before-delete). *"The process is better than most production quant prototypes; the problem is that
the prose conclusions ran ahead of what the code and data establish."* — which we've now corrected.
