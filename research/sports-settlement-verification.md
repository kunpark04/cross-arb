# Settlement-identity verification — sports pair (2026-06-09)

**Why this brief exists.** Settlement identity (invariant #1) was verified for *weather* only
([settlement-verification.md](settlement-verification.md)); the 2nd reviewer audit (C5,
[../tasks/reviewer-audit-2026-06-09.md](../tasks/reviewer-audit-2026-06-09.md)) flagged that **sports was never
checked**, even though the scalability thesis leans on it (MLB `lad-pit`). Unlike weather (a deterministic NWS
number), sports settles off an official-result **call**, and that call can diverge on a contested game. This
brief records the first live diff. Probe: `scripts/verify_sports_settlement.py` (read-only; both venues' live
rules, per league).

## Result (2026-06-09 live run; MLB, UFC, WNBA, CS2, LoL, Valorant, ATP, WTA, ITF-M/W — 10 leagues)

**Normal completed games agree on the winner** (same factual outcome, deterministic). The divergence is entirely
in the **tail** — postponed / rescheduled / cancelled / abandoned / retired / forfeited / no-contest games —
where the two rulebooks differ, sometimes materially:

| Edge case | Kalshi | polymarket.us | Verdict |
|---|---|---|---|
| **MLB — postponed & replayed (rain, doubleheader)** | settles on the replay **only if rescheduled ≤ 2 DAYS**; cancelled or **> 2 days → resolves to "a fair price"** (voids; does NOT settle on the actual game) | waits & settles on the replay **if ≤ 2 WEEKS**; else **last-traded prices** | 🔴 **MATERIAL window mismatch (2 days vs 2 weeks).** A game replayed 3–14 days later → **pmus pays the real winner while Kalshi voids to fair price** → the YES/NO legs no longer offset → both-legs risk on the depth-and-edge proof case |
| **Void PRICE basis (when BOTH void)** | *"a fair price in accordance with the rules"* | *"last-traded prices"* (esports/WNBA: *"last fair market price"*) | ⚠️ even a *symmetric* void doesn't offset to $1 — Kalshi's rule-derived fair value ≠ pmus's last-traded |
| **Esports (CS2/LoL/Valorant) & WNBA — abandonment / not officially completed** | **SILENT** (esports) / silent (WNBA) | → **last fair market price** | 🔴 pmus pins a price; Kalshi has no stated rule → asymmetric |
| **UFC — no-contest** | explicit *"tie or no contest"* handling in `rules_secondary` | *"no-contest … settle at last-traded prices"* (only tie/draw → $0.50 is explicit) | ⚠️ different resolutions for a no-contest |
| **Tennis/ITF — walkover / retirement / forfeit** | resolves only *"after a ball has been played"* | retire after start → official result; walkover/forfeit/withdrawal **before** start → **last-traded prices** | ⚠️ different mechanics around the "ball played" line |
| **Tie / draw** | resolves Yes/No on the winner | **$0.50** per instrument | ⚠️ different (rare in these sports) |
| **Source of truth** | league official (ATP/UFC/…) + AP/Sportradar | *"official … result"* / DCM rulebook | ✅ same factual winner, ≠ same named provider |

## Conclusion

For a **game that completes on schedule**, both venues grade off the same factual winner → the cross-venue pair
is settlement-clean. But the **postpone/void/reschedule tail is NOT identical**, and for **MLB — the depth-and-
edge proof case — the divergence is material**: Kalshi's reschedule window is **2 days**, pmus's is **2 weeks**,
so a rain-postponed game replayed within that 12-day gap settles to the **real winner on pmus but to a fair-price
void on Kalshi**. On a held YES+NO lock that is a **both-legs loss event**, not a wash. Even a *symmetric* void
doesn't fully offset, because Kalshi's *"fair price per the rules"* and pmus's *"last-traded price"* are computed
differently. This is the invariant-#1 risk, **unmitigated in code** beyond the live `game_edge` price-sanity
guard (which catches gross mispricings, not a clean void). MLB rain-postponements rescheduled 3+ days out are
routine, so this is not a rare tail.

## Still to close before deploying sports capital

1. **Per-league read — DONE for the co-listed set** (MLB, UFC, WNBA, CS2/LoL/Valorant, ATP/WTA/ITF; NBA/NHL not
   co-listed right now — re-run in season). The material finding is the **MLB 2-day-vs-2-week reschedule window**.
2. **Model the asymmetric-void EV term** in the all-in cost model
   ([0010](../decisions/0010-all-in-edge-filtering-and-cost-model.md)): `P(postpone)·P(replayed in the 2d–2wk
   gap)·loss` + a general `P(void)·(Kalshi-fairprice − pmus-lasttraded)` term, alongside leg-fill-failure. For
   MLB specifically, weight by the empirical rain-postponement rate.
3. **Gate sports arbs behind a per-league `void_settlement_verified` flag** (default False), and for MLB do not
   hold a pair through a postponement — **unwind before the 2-day Kalshi window expires**, or avoid weather-risk
   games. The bot must treat a postponement as a settlement-divergence event, not a delay.

## Sources

Live rules pulls 2026-06-09 via `scripts/verify_sports_settlement.py` (Kalshi `rules_primary`/`rules_secondary`
per single-team market; pmus market `description`/rule fields), 10 co-listed leagues: **MLB** (AZ vs MIA),
**UFC** (Gaethje vs Topuria), **WNBA** (DAL vs MIN), CS2 (Falcons vs G2), LoL (paiN vs LOS), Valorant
(Global vs XLG), ATP (Hurkacz vs Fucsovics), WTA (Navarro vs McNally), ITF-M (Mridha vs Ferri), ITF-W
(Price vs Candiotto). Full MLB rules text pulled directly to confirm the 2-day vs 2-week reschedule windows.
NBA/NHL were not co-listed at run time (re-run in season).
