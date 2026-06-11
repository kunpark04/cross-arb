"""Offline integration test for the NO-GAP Kalshi subscription path (probe-verified 2026-06-10).

Drives the REAL bot/monitor.run_live against a LOCALHOST fake Kalshi WS speaking the probe-verified
protocol (subscribed ack -> per-ticker seq'd snapshots; update_subscription acks type=ok and the ack
consumes a seq slot), plus a silent fake pmus WS. No internet, no creds, no orders — safe for the
selftest_all.py offline gate.

  Scenario A (happy path): heartbeat-2 discovery adds a market  -> EXPECT one update_subscription
    add_markets on the live sid and NO reconnect; the snapshot confirms the pending add. Discovery
    then drops the first market -> after the 2-miss prune debounce EXPECT delete_markets (still on
    connection #1, seq contiguous, zero kalshi_resync markers).
  Scenario B (fallback self-heal): the server acks the add but NEVER snapshots the ticker -> EXPECT
    the next heartbeat to cycle the connection (ws_reconnect marker logged, so analysis can censor),
    and connection #2's subscribe to carry the FULL ticker set.

  python scripts/test_monitor_nogap.py
"""
import asyncio, json, os, sys, tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bot"))
try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass


def make_kalshi_handler(klog, conn_subs, snapshot_on_add):
    """Per-connection handler. seq is per-connection (matches live); every control ack after the
    initial `subscribed` consumes a seq slot (probe-verified)."""
    async def handler(ws):
        conn_subs.append([])                      # this connection's subscribe ticker-lists
        seq = [0]
        def nxt():
            seq[0] += 1; return seq[0]
        async def snap(tk):
            await ws.send(json.dumps({"type": "orderbook_snapshot", "sid": 7, "seq": nxt(),
                "msg": {"market_ticker": tk, "yes_dollars_fp": [["0.40", "50.00"]],
                        "no_dollars_fp": [["0.55", "50.00"]]}}))
        try:
            async for raw in ws:
                o = json.loads(raw)
                klog.append(o)
                if o.get("cmd") == "subscribe":
                    conn_subs[-1].append(list(o["params"]["market_tickers"]))
                    await ws.send(json.dumps({"type": "subscribed", "id": o.get("id"),
                                              "msg": {"channel": "orderbook_delta", "sid": 7}}))
                    for tk in o["params"]["market_tickers"]:
                        await snap(tk)
                elif o.get("cmd") == "update_subscription":
                    await ws.send(json.dumps({"type": "ok", "id": o.get("id"), "sid": 7, "seq": nxt(),
                                              "msg": {"market_tickers": o["params"]["market_tickers"]}}))
                    if o["params"]["action"] == "add_markets" and snapshot_on_add:
                        for tk in o["params"]["market_tickers"]:
                            await snap(tk)
        except Exception:
            pass                                  # connection torn down by the test -> fine
    return handler


async def silent_pmus(ws):
    try:
        async for _ in ws:                        # accept subscribes; send no market data (the
            pass                                  # Kalshi subscription path is the test target)
    except Exception:
        pass


async def run_scenario(name, snapshot_on_add, discovery_plan, hold_secs):
    """discovery_plan: fn(call_no) -> list of weather entries for that discovery pass."""
    import websockets
    import monitor, colisted_map
    from monitor import TransitionLogger

    klog, conn_subs = [], []
    kserver = await websockets.serve(make_kalshi_handler(klog, conn_subs, snapshot_on_add), "127.0.0.1", 0)
    pserver = await websockets.serve(silent_pmus, "127.0.0.1", 0)
    kport = kserver.sockets[0].getsockname()[1]
    pport = pserver.sockets[0].getsockname()[1]

    calls = [0]
    def stub_map():
        calls[0] += 1
        rep = {"weather_cities_UNMAPPED": [], "sports_leagues_UNMAPPED": [],
               "weather_bucket_MISALIGNED": [], "fetch_errors": []}
        return {"weather": discovery_plan(calls[0]), "sports": [], "econ": []}, rep

    monitor.KALSHI_WS = f"ws://127.0.0.1:{kport}"
    monitor.PMUS_WS = f"ws://127.0.0.1:{pport}"
    monitor.kalshi_ws_headers = lambda: {}        # localhost: no auth handshake
    monitor._pmus_auth_headers = lambda: {}
    monitor.fetch_cli = lambda st: None           # keep the offline gate offline (no NWS fetches)
    monitor.ADD_CONFIRM_SECS = 0.5                # un-snapshotted add must trip the NEXT 1s heartbeat
    colisted_map.build_colisted_map = stub_map

    td = tempfile.mkdtemp()
    task = asyncio.create_task(monitor.run_live(TransitionLogger(td), refresh_sec=1.0))
    await asyncio.sleep(hold_secs)
    task.cancel()
    try:
        await task
    except BaseException:
        pass
    kserver.close(); pserver.close()
    await kserver.wait_closed(); await pserver.wait_closed()
    sp = os.path.join(td, "sessions.jsonl")
    sessions = [json.loads(l) for l in open(sp, encoding="utf-8")] if os.path.exists(sp) else []
    print(f"\n[{name}] connections={len(conn_subs)}  cmds={[(o.get('cmd'), o.get('params', {}).get('action')) for o in klog]}")
    return klog, conn_subs, sessions


W1 = {"slug": "tc-test-w1-2026-06-10-gte70", "kalshi": "KXTESTW1"}
W2 = {"slug": "tc-test-w2-2026-06-10-gte80", "kalshi": "KXTESTW2"}


def main():
    # --- Scenario A: no-gap add then prune-delete, all on ONE connection ------------------------
    plan_a = lambda n: [W1] if n == 1 else ([W1, W2] if n == 2 else [W2])
    klog, conns, sessions = asyncio.run(run_scenario("A happy", True, plan_a, 4.6))
    adds = [o for o in klog if o.get("cmd") == "update_subscription" and o["params"]["action"] == "add_markets"]
    dels = [o for o in klog if o.get("cmd") == "update_subscription" and o["params"]["action"] == "delete_markets"]
    assert len(conns) == 1, f"expected 1 connection (no cycling), saw {len(conns)}"
    assert len(adds) == 1 and adds[0]["params"]["market_tickers"] == ["KXTESTW2"], adds
    assert adds[0]["params"]["sids"] == [7], adds                       # targets the live sid from the ack
    assert len(dels) == 1 and dels[0]["params"]["market_tickers"] == ["KXTESTW1"], dels
    assert not [s for s in sessions if s.get("event") == "kalshi_resync"], sessions
    assert not [s for s in sessions if s.get("event") == "ws_reconnect" and s.get("venue") == "k"], sessions
    print("OK - A: add_markets (no reconnect, right sid) -> snapshot confirm -> prune delete_markets; "
          "0 resyncs, 0 k reconnects")

    # --- Scenario B: add acked but never snapshotted -> fallback cycles with a censor marker ----
    plan_b = lambda n: [W1] if n == 1 else [W1, W2]
    klog, conns, sessions = asyncio.run(run_scenario("B fallback", False, plan_b, 4.6))
    adds = [o for o in klog if o.get("cmd") == "update_subscription" and o["params"]["action"] == "add_markets"]
    assert len(adds) == 1, f"expected exactly 1 add attempt, saw {len(adds)}"
    assert len(conns) == 2, f"expected fallback cycle -> 2 connections, saw {len(conns)}"
    full = set(conns[1][0]) if conns[1] else set()
    assert full == {"KXTESTW1", "KXTESTW2"}, f"reconnect must subscribe the FULL ticker set, got {full}"
    assert [s for s in sessions if s.get("event") == "ws_reconnect" and s.get("venue") == "k"], \
        "fallback cycle must log a ws_reconnect marker (analysis censors the rebuild window)"
    print("OK - B: un-snapshotted add tripped the fallback cycle; reconnect subscribed the full set; "
          "ws_reconnect marker logged")
    print("\nALL GREEN - no-gap subscription integration test")


if __name__ == "__main__":
    main()
