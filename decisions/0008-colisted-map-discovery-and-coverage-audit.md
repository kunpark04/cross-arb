# 0008 — The co-listed map is rebuilt by full discovery + coverage-audited (never a static list)

- **Date:** 2026-06-08
- **Status:** Accepted
- **Deciders:** Project owner (raised the freshness concern) + Claude

## Context

The monitor needs a `{polymarket.us slug → Kalshi ticker}` map of co-listed markets. A map built **once**
at startup goes stale fast: weather markets are new **every day** (each day's temperature buckets are
fresh slugs/tickers) and sports games roll in as schedules advance. Worse, the discovery config is two
**hardcoded** maps — `WX` (5 weather cities) and `LEAGUES` (12 sports leagues) — so a brand-new
*category* (a 6th city, or a new league) would be matched by nothing and **silently missed**.

## Decision

`bot/colisted_map.py::build_colisted_map()` rebuilds the map by **full discovery on every call** — it
pulls the entire polymarket.us `closed=false` catalog plus the Kalshi series and groups by date/event
**dynamically**, so new weather days and new sports games in known cities/leagues are picked up
automatically. The monitor calls it at startup **and on the REST heartbeat** to refresh. It also returns
a **coverage report** that lists every climate city + sports league polymarket.us is currently listing
and **flags any not in `WX`/`LEAGUES`** — so an unmapped category is loud, not silent. **Never ship a
frozen map.**

## Alternatives considered

- **Static map built once at startup** — rejected: stale within a day (weather) and as games schedule.
- **Auto-map every league discovered** — rejected: surfaces niche/un-co-listed noise (e.g. `twc`
  influencer soccer, 1 market, no Kalshi counterpart). A human decides whether a flagged category is
  worth mapping; the audit just makes sure it's *seen*.

## Consequences

- New dates/games in mapped cities/leagues are covered automatically (live run 2026-06-08: 60 weather +
  123 sports pairs). New categories are flagged — the audit immediately caught `twc` (soccer) on the
  first run; left unmapped by choice (no Kalshi co-listing).
- `WX`/`LEAGUES` are **mirrored** in `bot/colisted_map.py` and `scripts/scan_all.py`; the coverage audit
  is the backstop that makes any drift between them (or vs reality) visible. (Lesson L7.)
- Weather pairs are **1:1** (slug↔ticker) and wired into the monitor now; **sports** pairs are a game ↔
  **two** Kalshi team tickers and need the 2-outcome tracker (next extension).
- **Remaining:** dynamic (un)subscribe on discovery churn (heartbeat currently logs churn + coverage;
  re-subscription is the open TODO).
