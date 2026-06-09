# Latency-reduction playbook — cross-arb execution

**The one thing to internalize first.** For this strategy the latency floor is **network RTT + the venues' own
API/matching/rate-limit latency** (tens-to-hundreds of ms over the public internet to two retail prediction-market
APIs), **not** language-level compute (microseconds). The edge persists for **seconds to minutes** (monitor:
~2s median episode, MLB line-lag over hours) and you **cannot colocate at a matching engine** — these venues
don't offer it. So you're a **fast-follower vs other retail/semi-pro arbers**, not an HFT latency-arbitrageur.
That ordering is why the list below is ranked: **location + connection + concurrency dwarf compute, and compute
(Rust/C++) is the *last* tier, deferred until after the edge is proven.** Measure your real floor with
`scripts/latency_probe.py` before optimizing anything.

Ranked by leverage (biggest latency win first):

## Tier 1 — Where the box lives (the dominant lever)

1. **Put the bot in the cloud region nearest each venue's API endpoint.** Most US fintech APIs are AWS
   us-east-1 (N. Virginia); host there and RTT collapses from ~30-100ms (residential/cross-country) to ~1-10ms.
   This single change usually beats *everything* below combined. It's a deploy choice, not code.
2. **Measure where each venue actually is first.** If Kalshi and pmus resolve to different regions/providers,
   you have a real decision: one box near the slower venue (you fire both legs concurrently, so you optimize the
   *max* RTT), or two boxes (one per venue) linked over a fast backbone. `latency_probe.py` gives the per-venue
   RTT to decide.
3. **Same cloud provider / backbone as the venue, not the public internet.** Being on AWS when the venue is on
   AWS routes you over the backbone, skipping public-internet hops. Avoid residential ISP, WiFi, VPNs, proxies,
   and extra NAT hops — every hop is jitter and tail latency.

## Tier 2 — Connections kept warm (big lever, pure config/code)

4. **Persistent, always-open connections.** Keep the market-data WS open 24/7 (the monitor already does) AND
   keep a warm, authenticated connection for ORDER submission. Never pay TLS handshake + TCP slow-start in the
   hot path — that alone is one or more RTTs you'd otherwise eat per order.
5. **Pre-warm and cache auth.** Load the signing key into memory ONCE at startup (today `kalshi_ws_headers` /
   `_pmus_auth_headers` re-read the PEM/`.env` from disk every call — fine for the read path, **must be fixed
   for the order path**). Pre-compute everything in the signature except the per-order timestamp/nonce.
6. **Use the venue's lowest-latency order channel.** If a venue offers WS or FIX order entry, prefer it over a
   cold REST POST. Submit over an already-open, ideally prioritized, socket (HTTP/2 multiplexing or a dedicated
   order connection separate from the data feed).
7. **Separate sockets for data vs orders** so a burst of market data never head-of-line-blocks an order, and a
   data-feed rate-limit never throttles your order path.

## Tier 3 — Execution design (concurrency — Python is entirely adequate here)

8. **Fire both legs CONCURRENTLY, never serially.** Send both orders at once (async): execution floor becomes
   `max(rtt_A, rtt_B)`, not `rtt_A + rtt_B`. This roughly halves effective latency vs naive sequential legging.
9. **Pre-stage the orders before the trigger.** When an edge is forming, pre-build BOTH signed order payloads
   (size, price, side) so the trigger only does the network send. All decision logic runs *before* the trigger;
   the hot path is "send two pre-built packets."
10. **Pre-stage the unwind too.** Have the naked-leg flatten order pre-built so a one-leg-fill can be cleaned up
    in a single RTT (this is risk control *and* latency — the faster you flatten a naked leg, the smaller the
    adverse move; see `scripts/adverse_selection.py`).
11. **Drive decisions off the local WS book, never a REST fetch in the hot path.** The monitor already maintains
    the book locally; the order path must read that in-memory state, not call the API.

## Tier 4 — Compute (the SMALLEST lever — where Rust/C++ *might* eventually fit)

12. **Tight hot path in whatever language:** no allocations, no logging, no blocking I/O, no GC pause between
    decision and send. In Python: pre-allocate, avoid per-tick object churn, keep the send path tiny. This
    matters far more than the language.
13. **Faster serialization** only after the above: a pre-serialized order template with price/size spliced in,
    or `orjson` — micro-optimizations behind a 20-100ms network floor.
14. **Rust/C++ ONLY if a profile proves the language is the bottleneck (it won't be here), and even then only the
    hot decision+sign+serialize path** — single-digit-microsecond gains, invisible behind Tier 1-3. The good
    reason to eventually use Rust is **reliability of a 24/7 money-touching daemon** (no GC pauses, strong types,
    single-binary deploy), **not latency** — and it's a post-edge-proven hardening step, not a prerequisite.

## Tier 5 — Operational (keep the floor from regressing)

15. **Measure continuously** (`latency_probe.py`): track the **p99**, not just p50 — you lose on the slow tail.
    Alert on regressions; a bad route or a noisy neighbor can silently double your RTT.
16. **Stay under rate limits.** A 429 is *infinite* latency. Budget requests; keep data and order quotas separate.
17. **Clock discipline** (NTP/chrony) so your own latency measurements and order timestamps are accurate.
18. **Don't fight a slow venue.** If `latency_probe` shows a venue is congested in a window, the edge isn't worth
    chasing then — more speed has diminishing returns once you're reliably inside the seconds-long edge lifetime.

## The honest ceiling

You cannot out-latency the **information event** (a line move, a forecast update) that makes the lagging venue
snap to fair — you can only *react* to it. So beyond "fast enough to fill both legs inside the edge's lifetime
and ahead of competing retail arbers," more speed has sharply diminishing returns. The biggest lever on realized
P&L is not raw speed at all — it's **execution correctness** (concurrent two-leg submit + fast naked-leg cleanup).
Spend the latency budget on Tier 1-3; spend the *engineering* budget on fills.
