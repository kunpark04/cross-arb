# Settlement-identity verification — sports pair (2026-06-09)

**Why this brief exists.** Settlement identity (invariant #1) was verified for *weather* only
([settlement-verification.md](settlement-verification.md)); the 2nd reviewer audit (C5,
[../tasks/reviewer-audit-2026-06-09.md](../tasks/reviewer-audit-2026-06-09.md)) flagged that **sports was never
checked**, even though the scalability thesis leans on it (MLB `lad-pit`). Unlike weather (a deterministic NWS
number), sports settles off an official-result **call**, and that call can diverge on a contested game. This
brief records the first live diff. Probe: `scripts/verify_sports_settlement.py` (read-only; both venues' live
rules, per league).

## Result (2026-06-09 live run; ATP, ITF-M, ITF-W, CS2, LoL sampled)

**Normal completed games agree on the winner** (same factual outcome, deterministic). The divergence is in the
**tail** — postponed / abandoned / retired / forfeited / rescheduled games — where the two rulebooks differ:

| Edge case | Kalshi | polymarket.us | Verdict |
|---|---|---|---|
| **Tennis (ATP/ITF) — match not played / walkover** | resolves only *"after a ball has been played"*; pre-match walkover/injury/forfeiture/cancellation handled by `rules_secondary` | tie/draw → **$0.50**; postponed/delayed/rescheduled → settle date amended to the reschedule; **if rescheduled >2 weeks → $0.50** | ⚠️ **different mechanics**; the *">2-week reschedule → $0.50"* rule is pmus-only |
| **Esports (CS2/LoL) — no-result / abandonment / forfeit** | rules **SILENT** (no void/abandonment language found) | *"no result (NR), abandonment, or cancellation … resolve to the **last fair market price**"* | ⚠️ **clear divergence**: pmus pins last-traded price; Kalshi has no stated void rule |
| **Tie / draw** | resolves Yes/No on the winner | settles **$0.50** per instrument | ⚠️ different (rare in these sports, but defined differently) |
| **Source of truth** | league official (e.g. *ATP*) + AP/Sportradar chain | *"official … result"* / its DCM rulebook | ✅ usually the same factual winner, but **not the same named provider** |

## Conclusion

For a **game that completes normally**, both venues grade off the same factual winner → the cross-venue pair is
settlement-clean. But the **void/abandonment/reschedule tail is NOT identical**: a contested game can settle the
two legs to **different values** (e.g. an abandoned esports match → pmus "last fair price" vs Kalshi's
unstated/void handling; a tennis match rescheduled >2 weeks → pmus $0.50 vs Kalshi's "ball played" condition),
which on a held YES+NO pair is a **both-legs loss**. This is exactly the invariant-#1 risk for the deep MLB/
esports positions the scalability thesis rides on — and it is **unmitigated in code** beyond the live
`game_edge` >40c orientation/price-sanity guard (which only catches gross mispricings, not a clean void).

## Still to close before deploying sports capital

1. **Per-league void/postponement read.** MLB (suspended/called/rain — "official game" rules), NBA/NHL/WNBA, and
   UFC (no-contest / weigh-in failure) were not in this sample — run `verify_sports_settlement.py --league mlb`
   (etc.) and read each rulebook. MLB is highest-priority (it's the depth-and-edge proof case).
2. **Model the asymmetric-void EV term** in the all-in cost model
   ([0010](../decisions/0010-all-in-edge-filtering-and-cost-model.md)): `P(void)·P(asymmetric resolution)·
   naked-leg-loss`, alongside the leg-fill-failure term. Until then, treat sports settlement as **clean only for
   games expected to complete**, and size the void tail as a real cost.
3. **Gate sports arbs behind a per-league `void_settlement_verified` flag** (default False) so the eventual bot
   cannot size into an un-vetted void path.

## Sources

Live rules pulls 2026-06-09 via `scripts/verify_sports_settlement.py` (Kalshi `rules_primary`/`rules_secondary`
per single-team market; pmus market `description`/rule fields). Sample: ATP S-Hertogenbosch, M25 Värnamo,
W35 Cuiabá, IEM Cologne (CS2), EWC SA/LATAM Qualifier (LoL).
