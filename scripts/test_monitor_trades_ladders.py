"""Offline integration test for the WAVE-2 weather measurement additions
(tasks/_agent_bus/20260611-probes/ladder-logging-spec.md): Kalshi trade-print REST poll (trade_id
dedupe + restart seed-from-log), k:"tr" transition ladders stamped at DETECTION time, delta-suppressed
k:"hb" heartbeat ladders, and the /series/fee_changes tripwire (record + health-beacon field) — plus
the additive-schema guarantee (transitions-* record shape untouched).

Drives the REAL bot/monitor.run_live against localhost fake venue WS servers (the test_monitor_nogap.py
pattern) with the REST fetchers stubbed in-process. No internet, no creds, no orders — safe for the
scripts/selftest_all.py offline gate.

  python scripts/test_monitor_trades_ladders.py
"""
import asyncio, json, os, sys, tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bot"))
try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass

W1 = {"slug": "tc-test-w1-2026-06-11-gte70", "kalshi": "KXTESTW1"}
TRADE_A = {"count_fp": "5.00", "created_time": "2026-06-11T00:40:09.714292Z", "taker_side": "yes",
           "ticker": "KXTESTW1", "trade_id": "trade-a", "yes_price_dollars": "0.0800"}
TRADE_B = {"count_fp": "3.50", "created_time": "2026-06-11T00:41:00.000000Z", "taker_side": "no",
           "ticker": "KXTESTW1", "trade_id": "trade-b", "yes_price_dollars": "0.0700"}


async def fake_kalshi(ws):
    """Subscribe -> subscribed ack + per-ticker snapshot (yes bid .40 / yes ask .45 from NO bid .55)."""
    try:
        seq = [0]
        async for raw in ws:
            o = json.loads(raw)
            if o.get("cmd") == "subscribe":
                await ws.send(json.dumps({"type": "subscribed", "id": o.get("id"),
                                          "msg": {"channel": "orderbook_delta", "sid": 7}}))
                for tk in o["params"]["market_tickers"]:
                    seq[0] += 1
                    await ws.send(json.dumps({"type": "orderbook_snapshot", "sid": 7, "seq": seq[0],
                        "msg": {"market_ticker": tk, "yes_dollars_fp": [["0.40", "50.00"]],
                                "no_dollars_fp": [["0.55", "50.00"]]}}))
            elif o.get("cmd") == "update_subscription":
                seq[0] += 1
                await ws.send(json.dumps({"type": "ok", "id": o.get("id"), "sid": 7, "seq": seq[0], "msg": {}}))
    except Exception:
        pass


async def fake_pmus(ws):
    """One marketData frame per subscribed slug: P yes bid .62 / ask .70 -> dir-K arb vs the Kalshi book."""
    try:
        async for raw in ws:
            o = json.loads(raw)
            for s in ((o.get("subscribe") or {}).get("marketSlugs") or []):
                await ws.send(json.dumps({"marketData": {"marketSlug": s,
                    "bids":   [{"px": {"value": "0.62"}, "qty": "50.00"}],
                    "offers": [{"px": {"value": "0.70"}, "qty": "40.00"}]}}))
    except Exception:
        pass


async def drive(td, fee_plan, trade_plan, hold=2.8):
    import websockets
    import monitor, colisted_map
    from monitor import TransitionLogger
    kserver = await websockets.serve(fake_kalshi, "127.0.0.1", 0)
    pserver = await websockets.serve(fake_pmus, "127.0.0.1", 0)
    monitor.KALSHI_WS = f"ws://127.0.0.1:{kserver.sockets[0].getsockname()[1]}"
    monitor.PMUS_WS = f"ws://127.0.0.1:{pserver.sockets[0].getsockname()[1]}"
    monitor.kalshi_ws_headers = lambda: {}        # localhost: no auth handshake
    monitor._pmus_auth_headers = lambda: {}
    monitor.fetch_cli = lambda st: None           # keep the offline gate offline
    monitor.WX_POLL_SEC = 0.6                     # spec's 300 s cadence shrunk so ~4 cycles fit the hold
    fee_calls, trade_calls = [0], [0]
    def fake_fees():
        fee_calls[0] += 1; return fee_plan(fee_calls[0])
    def fake_trades(ticker, min_ts, **kw):
        trade_calls[0] += 1; return trade_plan(trade_calls[0], ticker, min_ts)
    monitor.fetch_fee_changes = fake_fees
    monitor.fetch_k_trades = fake_trades
    rep = {"weather_cities_UNMAPPED": [], "sports_leagues_UNMAPPED": [],
           "weather_bucket_MISALIGNED": [], "fetch_errors": []}
    colisted_map.build_colisted_map = lambda: ({"weather": [W1], "sports": [], "econ": []}, rep)
    task = asyncio.create_task(monitor.run_live(TransitionLogger(td), refresh_sec=1.0))
    await asyncio.sleep(hold)
    task.cancel()
    try:
        await task
    except BaseException:
        pass
    kserver.close(); pserver.close()
    await kserver.wait_closed(); await pserver.wait_closed()
    return fee_calls[0], trade_calls[0]


def rows(td, name):
    p = os.path.join(td, name)
    return [json.loads(l) for l in open(p, encoding="utf-8")] if os.path.exists(p) else []


def main():
    td = tempfile.mkdtemp()
    from monitor import TransitionLogger, trade_rec, _iso_unix
    # RESTART SEEDING: trade-a is already on disk from a "previous run" — it must NOT be re-logged.
    TransitionLogger(td).trade(trade_rec(TRADE_A, W1["slug"], 1.0))
    fee_plan = lambda n: [] if n == 1 else [{"series_ticker": "KXTEST", "fee_type": "quadratic"}]
    # the fake honors the endpoint's min_ts contract (server returns only ts >= min_ts, newest first) —
    # so this ALSO verifies the monitor sends a min_ts that reaches back into the 1 s dedupe overlap
    trade_plan = lambda n, tk, min_ts: [t for t in (TRADE_B, TRADE_A)
                                        if _iso_unix(t["created_time"]) >= min_ts]
    nfee, ntrade = asyncio.run(drive(td, fee_plan, trade_plan))
    assert nfee >= 3 and ntrade >= 3, f"expected >=3 poll cycles in the hold, got fee={nfee} trades={ntrade}"

    # A. trades: the pre-seeded print is NOT re-logged; the new print is logged exactly once over n cycles
    tr = rows(td, "trades-2026-06-11.jsonl")
    assert [r["id"] for r in tr] == ["trade-a", "trade-b"], [r["id"] for r in tr]
    assert tr[1]["yes_c"] == 7 and tr[1]["qty"] == 3.5 and tr[1]["taker"] == "no" and tr[1]["venue"] == "k"
    assert tr[1]["vt"] == TRADE_B["created_time"] and tr[1]["market"] == W1["slug"]   # venue ts verbatim (L22)
    print(f"OK - trades: print logged ONCE across {ntrade} poll cycles (restart seed + trade_id dedupe)")

    # B. transitions shape UNTOUCHED + a k:'tr' ladder rides the weather transition at the SAME t
    trans = rows(td, "transitions-2026-06-11.jsonl")
    assert trans and trans[0]["transition"] == "OPEN" and trans[0]["dir"] == "K", trans[:1]
    allowed = {"t", "market", "transition", "dir", "net_edge", "depth", "age", "px"}
    assert all(set(r) <= allowed for r in trans), "transition record shape must stay additive-clean"
    lads = rows(td, "ladders-2026-06-11.jsonl")
    trl = [r for r in lads if r["k"] == "tr"]
    assert trl and trl[0]["t"] == trans[0]["t"], "k:'tr' ladder must carry the transition's DETECTION t"
    assert trl[0]["pb"] == [[62, 50.0]] and trl[0]["pa"] == [[70, 40.0]], trl[0]
    assert trl[0]["kb"] == [[40, 50.0]] and trl[0]["ka"] == [[45, 50.0]], trl[0]
    print("OK - ladders k:'tr': emitted with the weather transition, detection-time t, dual-venue top-5")

    # C. hb ladders: emitted on first sight, then DELTA-SUPPRESSED while the books idle
    hbs = [r for r in lads if r["k"] == "hb"]
    assert 1 <= len(hbs) <= 2, f"idle books should yield 1 hb (2 if the first pass raced the pm frame), got {len(hbs)}"
    assert len(hbs) < nfee, f"suppression never kicked in: {len(hbs)} hb across {nfee} cycles"
    assert hbs[-1]["market"] == W1["slug"] and hbs[-1]["kb"] == [[40, 50.0]]
    print(f"OK - ladders k:'hb': {len(hbs)} snapshot(s) then suppressed across {nfee} poll cycles")

    # D. fee tripwire: empty boot baseline silent; the change logs ONE record; the beacon carries the count
    fees = rows(td, "fee_changes.jsonl")
    assert len(fees) == 1 and fees[0]["n"] == 1 and fees[0]["changes"][0]["series_ticker"] == "KXTEST", fees
    beacon = json.load(open(os.path.join(td, "health.json")))
    assert beacon.get("fee_changes") == 1, beacon
    print("OK - fee tripwire: baseline silent, change logged once, health beacon carries fee_changes=1")
    print("\nALL GREEN - wave-2 trades/ladders/fee logging integration")


if __name__ == "__main__":
    main()
