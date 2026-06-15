# NEEDS_MANUAL settlement-identity list — 2026-06-15

**Snapshot of today's live co-listed boards.** Re-run `python scripts/settlement_identity.py --audit`
for the current list — **it rotates daily** (sports/weather slugs are date-stamped; today's MLB/tennis/WC
games are not tomorrow's).

## The 4-category settlement-identity gate ([decision 0018](../decisions/0018-settlement-identity-outcome-not-source.md))

Every co-listed pair is gated on invariant #1 (both venues must grade the *same event* off the *identical
outcome*). The gate returns one of four verdicts:
**IDENTICAL** = every outcome-determining dimension provably matches → auto-tradeable clean input ·
**TAIL** = differs only on a low-probability *priceable* tail (`tail_cost_cents` ¢/contract) → auto-tradeable
with a haircut, iff edge > cost · **DIVERGENT** = a dimension provably differs *structurally* (a "locked" pair
can lose BOTH legs) → avoid · **NEEDS_MANUAL** = the extractor can't auto-prove identical settlement (default)
→ **owner reviews the rules before trusting the lock**.

## Universe split (today's `--audit`)

| category | IDENTICAL | TAIL | DIVERGENT | NEEDS_MANUAL | total |
|---|---:|---:|---:|---:|---:|
| weather | 24 | 0 | 0 | **36** | 60 |
| sports (MLB/ATP/WTA/UFC/ITF/esports/WNBA) | 0 | 59 | 0 | **44** | 103 |
| soccer3 (World Cup, per-outcome) | 0 | 177 | 0 | **0** | 177 |
| econ | 13 | 0 | 0 | **0** | 13 |
| **TOTAL** | **37** | **236** | **0** | **80** | **353** |

**NEEDS_MANUAL = 80** (36 weather + 44 sports). **DIVERGENT = 0.** This file lists every NEEDS_MANUAL pair
the audit surfaced, grouped by category. (Each row is one pmus market ↔ its bound Kalshi ticker(s); the two
ITF/esports/WNBA tickers per row are the two team/player legs of the same game.)

---

## Weather — 36 NEEDS_MANUAL

**Reason (identical for all 36):** *station not specifically named on Kalshi* — pmus names the ICAO code
(KSFO/KLAX/KMIA) in its description, but the Kalshi rules text gives only the city + the generic word
"airport", which is not a station identity (two different cities both say "airport" → matching on it would be
a false IDENTICAL, lesson L1). Source (both NWS CLI) and boundary both match; only the station dimension
abstains. This is a **rules-text naming gap, not a settlement divergence** (see Actionability footer).

### San Francisco (KSFO) — 12
```
tc-temp-sfohigh-2026-06-15-gte77lt78f   ↔  KXHIGHTSFO-26JUN15-B77.5
tc-temp-sfohigh-2026-06-15-lt71f        ↔  KXHIGHTSFO-26JUN15-T71
tc-temp-sfohigh-2026-06-15-gte71lt72f   ↔  KXHIGHTSFO-26JUN15-B71.5
tc-temp-sfohigh-2026-06-15-gte73lt74f   ↔  KXHIGHTSFO-26JUN15-B73.5
tc-temp-sfohigh-2026-06-15-gte75lt76f   ↔  KXHIGHTSFO-26JUN15-B75.5
tc-temp-sfohigh-2026-06-15-gte79f       ↔  KXHIGHTSFO-26JUN15-T78
tc-temp-sfohigh-2026-06-16-gte71lt72f   ↔  KXHIGHTSFO-26JUN16-B71.5
tc-temp-sfohigh-2026-06-16-lt69f        ↔  KXHIGHTSFO-26JUN16-T69
tc-temp-sfohigh-2026-06-16-gte69lt70f   ↔  KXHIGHTSFO-26JUN16-B69.5
tc-temp-sfohigh-2026-06-16-gte73lt74f   ↔  KXHIGHTSFO-26JUN16-B73.5
tc-temp-sfohigh-2026-06-16-gte75lt76f   ↔  KXHIGHTSFO-26JUN16-B75.5
tc-temp-sfohigh-2026-06-16-gte77f       ↔  KXHIGHTSFO-26JUN16-T76
```

### Los Angeles (KLAX) — 12
```
tc-temp-laxhigh-2026-06-15-lt68f        ↔  KXHIGHLAX-26JUN15-T68
tc-temp-laxhigh-2026-06-15-gte68lt69f   ↔  KXHIGHLAX-26JUN15-B68.5
tc-temp-laxhigh-2026-06-15-gte70lt71f   ↔  KXHIGHLAX-26JUN15-B70.5
tc-temp-laxhigh-2026-06-15-gte72lt73f   ↔  KXHIGHLAX-26JUN15-B72.5
tc-temp-laxhigh-2026-06-15-gte74lt75f   ↔  KXHIGHLAX-26JUN15-B74.5
tc-temp-laxhigh-2026-06-15-gte76f       ↔  KXHIGHLAX-26JUN15-T75
tc-temp-laxhigh-2026-06-16-lt67f        ↔  KXHIGHLAX-26JUN16-T67
tc-temp-laxhigh-2026-06-16-gte67lt68f   ↔  KXHIGHLAX-26JUN16-B67.5
tc-temp-laxhigh-2026-06-16-gte69lt70f   ↔  KXHIGHLAX-26JUN16-B69.5
tc-temp-laxhigh-2026-06-16-gte71lt72f   ↔  KXHIGHLAX-26JUN16-B71.5
tc-temp-laxhigh-2026-06-16-gte73lt74f   ↔  KXHIGHLAX-26JUN16-B73.5
tc-temp-laxhigh-2026-06-16-gte75f       ↔  KXHIGHLAX-26JUN16-T74
```

### Miami (KMIA) — 12
```
tc-temp-miahigh-2026-06-15-lt87f        ↔  KXHIGHMIA-26JUN15-T87
tc-temp-miahigh-2026-06-15-gte87lt88f   ↔  KXHIGHMIA-26JUN15-B87.5
tc-temp-miahigh-2026-06-15-gte89lt90f   ↔  KXHIGHMIA-26JUN15-B89.5
tc-temp-miahigh-2026-06-15-gte91lt92f   ↔  KXHIGHMIA-26JUN15-B91.5
tc-temp-miahigh-2026-06-15-gte93lt94f   ↔  KXHIGHMIA-26JUN15-B93.5
tc-temp-miahigh-2026-06-15-gte95f       ↔  KXHIGHMIA-26JUN15-T94
tc-temp-miahigh-2026-06-16-gte90lt91f   ↔  KXHIGHMIA-26JUN16-B90.5
tc-temp-miahigh-2026-06-16-lt88f        ↔  KXHIGHMIA-26JUN16-T88
tc-temp-miahigh-2026-06-16-gte88lt89f   ↔  KXHIGHMIA-26JUN16-B88.5
tc-temp-miahigh-2026-06-16-gte92lt93f   ↔  KXHIGHMIA-26JUN16-B92.5
tc-temp-miahigh-2026-06-16-gte94lt95f   ↔  KXHIGHMIA-26JUN16-B94.5
tc-temp-miahigh-2026-06-16-gte96f       ↔  KXHIGHMIA-26JUN16-T95
```

*(NYC + Chicago, the other two co-listed cities, name a specific station/landmark on both venues and graded
IDENTICAL — they are not in this list.)*

---

## Sports — ITF tennis — 40 NEEDS_MANUAL

**Reason (identical for all 40):** *void window matches (14d) but the fallback price is not both-provable*
— the reschedule window agrees (14 days both venues) and the event/outcome-count dims match, but neither
venue's ITF rules text states the un-replayed-game **fallback price**, so the gate cannot prove the legs net
on a void. (Each row's two tickers are the two players' YES legs.) Genuine unpriced void-tail unknown.

### ITF Men (itfm) — 22
```
aec-itfm-borbut-thopat-2026-06-15   ↔  KXITFMATCH-26JUN15BUTPAT-BUT , KXITFMATCH-26JUN15BUTPAT-PAT
aec-itfm-vlamel-ianbra-2026-06-15   ↔  KXITFMATCH-26JUN15MELBRA-MEL , KXITFMATCH-26JUN15MELBRA-BRA
aec-itfm-alexpet-vitkal-2026-06-15  ↔  KXITFMATCH-26JUN15PETKAL-PET , KXITFMATCH-26JUN15PETKAL-KAL
aec-itfm-maxdus-chacam-2026-06-15   ↔  KXITFMATCH-26JUN15DUSCAM-DUS , KXITFMATCH-26JUN15DUSCAM-CAM
aec-itfm-lucmar-armzam-2026-06-15   ↔  KXITFMATCH-26JUN15MARZAM-MAR , KXITFMATCH-26JUN15MARZAM-ZAM
aec-itfm-joshpec-maralv-2026-06-15  ↔  KXITFMATCH-26JUN15PECALV-PEC , KXITFMATCH-26JUN15PECALV-ALV
aec-itfm-matshe-tylbow-2026-06-15   ↔  KXITFMATCH-26JUN15SHEBOW-SHE , KXITFMATCH-26JUN15SHEBOW-BOW
aec-itfm-marmil-jelsar-2026-06-15   ↔  KXITFMATCH-26JUN15MILSAR-MIL , KXITFMATCH-26JUN15MILSAR-SAR
aec-itfm-kylove-neonie-2026-06-15   ↔  KXITFMATCH-26JUN15OVENIE-OVE , KXITFMATCH-26JUN15OVENIE-NIE
aec-itfm-mikars-wilman-2026-06-15   ↔  KXITFMATCH-26JUN15ARSMAN-ARS , KXITFMATCH-26JUN15ARSMAN-MAN
aec-itfm-tiazha-shiima-2026-06-16   ↔  KXITFMATCH-26JUN16ZHAIMA-ZHA , KXITFMATCH-26JUN16ZHAIMA-IMA
aec-itfm-cagtuf-haygok-2026-06-16   ↔  KXITFMATCH-26JUN16TUFGOK-TUF , KXITFMATCH-26JUN16TUFGOK-GOK
aec-itfm-tokito-bogsel-2026-06-16   ↔  KXITFMATCH-26JUN16ITOSEL-ITO , KXITFMATCH-26JUN16ITOSEL-SEL
aec-itfm-nicalv-rubhar-2026-06-16   ↔  KXITFMATCH-26JUN16GOLHAR-GOL , KXITFMATCH-26JUN16GOLHAR-HAR
aec-itfm-nicris-giaort-2026-06-16   ↔  KXITFMATCH-26JUN16RISORT-RIS , KXITFMATCH-26JUN16RISORT-ORT
aec-itfm-domsim-davmor-2026-06-16   ↔  KXITFMATCH-26JUN16SIMMOR-SIM , KXITFMATCH-26JUN16SIMMOR-MOR
aec-itfm-danspa-davcar-2026-06-16   ↔  KXITFMATCH-26JUN16SPACAR-SPA , KXITFMATCH-26JUN16SPACAR-CAR
aec-itfm-matcov-noakar-2026-06-16   ↔  KXITFMATCH-26JUN16COVKAR-COV , KXITFMATCH-26JUN16COVKAR-KAR
aec-itfm-velkrs-samkyj-2026-06-16   ↔  KXITFMATCH-26JUN16KRSKYJ-KRS , KXITFMATCH-26JUN16KRSKYJ-KYJ
aec-itfm-novnov-jankli-2026-06-16   ↔  KXITFMATCH-26JUN16NOVKLI-NOV , KXITFMATCH-26JUN16NOVKLI-KLI
aec-itfm-gabguz-albmor-2026-06-16   ↔  KXITFMATCH-26JUN16GUZMOR-GUZ , KXITFMATCH-26JUN16GUZMOR-MOR
aec-itfm-oscbro-valcon-2026-06-16   ↔  KXITFMATCH-26JUN16BROCON-BRO , KXITFMATCH-26JUN16BROCON-CON
```

### ITF Women (itfw) — 18
```
aec-itfw-diamar-jimgon-2026-06-15   ↔  KXITFWMATCH-26JUN15MARGER-MAR , KXITFWMATCH-26JUN15MARGER-GER
aec-itfw-beltho-celsan-2026-06-15   ↔  KXITFWMATCH-26JUN15THOANS-THO , KXITFWMATCH-26JUN15THOANS-ANS
aec-itfw-olidro-nikkop-2026-06-15   ↔  KXITFWMATCH-26JUN15DROKOP-DRO , KXITFWMATCH-26JUN15DROKOP-KOP
aec-itfw-klakaj-aliska-2026-06-15   ↔  KXITFWMATCH-26JUN15KAJSKA-KAJ , KXITFWMATCH-26JUN15KAJSKA-SKA
aec-itfw-katwys-gabvil-2026-06-15   ↔  KXITFWMATCH-26JUN15WYSVIL-WYS , KXITFWMATCH-26JUN15WYSVIL-VIL
aec-itfw-tiplem-zozkar-2026-06-15   ↔  KXITFWMATCH-26JUN15LEMKAR-LEM , KXITFWMATCH-26JUN15LEMKAR-KAR
aec-itfw-angho-simkay-2026-06-15    ↔  KXITFWMATCH-26JUN15HOXKAY-HOX , KXITFWMATCH-26JUN15HOXKAY-KAY
aec-itfw-cheyua-hanshi-2026-06-16   ↔  KXITFWMATCH-26JUN16YUASHI-YUA , KXITFWMATCH-26JUN16YUASHI-SHI
aec-itfw-laubru-andsoa-2026-06-16   ↔  KXITFWMATCH-26JUN16BRUSOA-BRU , KXITFWMATCH-26JUN16BRUSOA-SOA
aec-itfw-alemar-sofbar-2026-06-16   ↔  KXITFWMATCH-26JUN16MARBAR-MAR , KXITFWMATCH-26JUN16MARBAR-BAR
aec-itfw-alepat-joucir-2026-06-16   ↔  KXITFWMATCH-26JUN16PATCIR-PAT , KXITFWMATCH-26JUN16PATCIR-CIR
aec-itfw-yasvav-allisa-2026-06-16   ↔  KXITFWMATCH-26JUN16VAVISA-VAV , KXITFWMATCH-26JUN16VAVISA-ISA
aec-itfw-anagab-margut-2026-06-16   ↔  KXITFWMATCH-26JUN16GABGUT-GAB , KXITFWMATCH-26JUN16GABGUT-GUT
aec-itfw-hibsha-aleang-2026-06-16   ↔  KXITFWMATCH-26JUN16SHAANG-SHA , KXITFWMATCH-26JUN16SHAANG-ANG
aec-itfw-beaspa-valart-2026-06-16   ↔  KXITFWMATCH-26JUN16SPAART-SPA , KXITFWMATCH-26JUN16SPAART-ART
aec-itfw-aurcor-amehej-2026-06-16   ↔  KXITFWMATCH-26JUN16CORHEJ-COR , KXITFWMATCH-26JUN16CORHEJ-HEJ
aec-itfw-yihqu-berbon-2026-06-16    ↔  KXITFWMATCH-26JUN16QUXBON-QUX , KXITFWMATCH-26JUN16QUXBON-BON
aec-itfw-elemil-claper-2026-06-16   ↔  KXITFWMATCH-26JUN16MILFER-MIL , KXITFWMATCH-26JUN16MILFER-FER
```

---

## Sports — esports + WNBA — 4 NEEDS_MANUAL

**Reason (identical for all 4):** *void/postpone reschedule window silent on both venues* — neither venue's
rules text states a reschedule window or fallback at all (window = None / None). The void tail is the #1
sports risk and cannot be priced from an unstated rule, so the gate abstains. Event-date and outcome-count
(both 2-way) match. Genuine unpriced void/postpone-tail unknown.

### Esports (CS2 / Valorant) — 2
```
aec-cs2-navi-g2-2026-06-15        ↔  KXCS2GAME-26JUN151300G2NAVI-NAVI , KXCS2GAME-26JUN151300G2NAVI-G2
aec-valorant-lev-xlg-2026-06-16   ↔  KXVALORANTGAME-26JUN161300XLGLEV-LEV , KXVALORANTGAME-26JUN161300XLGLEV-XLG
```

### WNBA — 2
```
aec-wnba-lv-dal-2026-06-15        ↔  KXWNBAGAME-26JUN15LVDAL-LV , KXWNBAGAME-26JUN15LVDAL-DAL
aec-wnba-tor-ind-2026-06-16       ↔  KXWNBAGAME-26JUN16TORIND-TOR , KXWNBAGAME-26JUN16TORIND-IND
```

---

## Econ — 0 NEEDS_MANUAL

None today. All 13 co-listed econ pairs (CPI / U-3 / NFP / GDP / Fed grid-step twins) graded **IDENTICAL**
on the 0013 twin-boundary logic — agency + release + boundary all provable.

## World Cup (soccer3, per-outcome) — 0 NEEDS_MANUAL

None today. All 177 per-outcome World Cup pairs graded **TAIL** (~0.08¢/contract void-fallback tail;
regulation timing matches both sides) — none fell to NEEDS_MANUAL.

---

## DIVERGENT

**None.** 0 pairs across all categories graded DIVERGENT today — no structural, un-priceable settlement
conflict was found in the live universe.

---

## Actionability

- **Weather flags (36, all 3 cities) — a rules-text naming gap, near-certainly clean.** The only un-proven
  dimension is the Kalshi station *string* (Kalshi omits the ICAO code, says only "<city> airport"). Source
  (both NWS CLI) and bucket boundary already match. These are the **same NWS station** as pmus in every case
  the primary-source check has examined — historically **360/360 settled buckets graded IDENTICAL** three-way
  vs the independent NWS CLI (incl. a real revision day, 0 divergence). **Verify** with
  `python scripts/verify_settlement.py` (pulls both venues' live rules per city, diffs source/station/boundary
  from the primary source rather than the regex). **Low effort to promote to IDENTICAL** once the station
  identity is confirmed — these are a heuristic-extractor abstention, not a real divergence.

- **Sports flags (44: 40 ITF tennis + 2 esports + 2 WNBA) — genuine unpriced void/postpone-tail unknowns.**
  The gate could not extract the un-replayed/cancelled-game **fallback price** (ITF: window agrees at 14d but
  fallback silent; esports/WNBA: window itself silent). A void/postpone in the reschedule gap is exactly where
  the two legs can stop offsetting (Kalshi "fair price" ≠ pmus "last-traded"), and the cost cannot be priced
  from an unstated rule — so each **needs a per-rulebook read** (the actual ITF/esports/WNBA void + reschedule
  + fallback handling on both venues) before the lock can be trusted. Higher effort than weather; do not assume
  these net on a non-completion.

---

*Generated from a live read-only `settlement_identity.py --audit` run over both venues' public market lists,
2026-06-15. Counts and slugs are today's boards only.*
