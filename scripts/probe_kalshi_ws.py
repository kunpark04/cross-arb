"""Validate the Kalshi orderbook_delta WebSocket (the second half of the dual-stream monitor).
READ-ONLY: subscribes to market data; places no orders.

Kalshi's WS requires auth (RSA-PSS) even for orderbook data — unlike its public REST market data.
Endpoint CONFIRMED 2026-06-08 by unauth probe:
  • wss://api.elections.kalshi.com/trade-api/ws/v2  -> 401 token_authentication_failure (live, auth-gated)
  • legacy trading-api.kalshi.com  -> "API has been moved to api.elections.kalshi.com"  (dead)
  • external-api-ws.kalshi.com/  -> 404                                                   (audit drift)

Needs KALSHI_ACCESS_KEY + KALSHI_PRIVATE_KEY_PATH in scripts/.env (create a key at
https://kalshi.com/account/profile -> API Keys, download the RSA PEM). Auth scheme: sign
"{ts_ms}GET/trade-api/ws/v2" with RSA-PSS-SHA256 (salt=digest length), send 3 KALSHI-ACCESS-* headers
on the handshake. See research/kalshi-venue-audit.md §1.2.
"""
import os, sys, time, json, base64, ssl, http.client, urllib.request
try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass

WS_HOST, WS_PATH = "api.elections.kalshi.com", "/trade-api/ws/v2"
REST = "https://api.elections.kalshi.com/trade-api/v2"
HERE = os.path.dirname(__file__)

def load_env(p):
    d = {}
    for line in open(p):
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            k, v = s.split("=", 1); d[k.strip()] = v.strip()
    return d

def endpoint_probe():
    """Unauth WS handshake — confirms the endpoint is live + auth-gated (no creds needed)."""
    try:
        c = http.client.HTTPSConnection(WS_HOST, timeout=15, context=ssl.create_default_context())
        c.request("GET", WS_PATH, headers={"Host": WS_HOST, "Upgrade": "websocket",
                  "Connection": "Upgrade", "Sec-WebSocket-Version": "13",
                  "Sec-WebSocket-Key": base64.b64encode(os.urandom(16)).decode()})
        r = c.getresponse(); body = r.read(160).decode("utf-8", "ignore"); c.close()
        print(f"endpoint  wss://{WS_HOST}{WS_PATH}  ->  [{r.status} {r.reason}]  {body.strip()[:90]}")
        return r.status
    except Exception as e:
        print(f"endpoint probe error: {type(e).__name__}: {str(e)[:90]}")
        return None

def need_creds_msg():
    print("\nKalshi creds NOT set in scripts/.env — cannot do the authenticated handshake yet.")
    print("To finish validation:")
    print("  1. https://kalshi.com/account/profile -> API Keys -> Create New API Key (KYC'd account).")
    print("  2. Download the RSA private key PEM (shown once); note the Key ID.")
    print("  3. In scripts/.env set:  KALSHI_ACCESS_KEY=<key id>   KALSHI_PRIVATE_KEY_PATH=./kalshi_key.pem")
    print("  4. Re-run:  python scripts/probe_kalshi_ws.py")

def rsa_pss_sign(pem_path, msg):
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    key = serialization.load_pem_private_key(open(pem_path, "rb").read(), password=None)
    sig = key.sign(msg.encode(),
                   padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
                   hashes.SHA256())
    return base64.b64encode(sig).decode()

def pick_ticker():
    """A liquid open market ticker from the PUBLIC REST API (no auth) to subscribe to."""
    req = urllib.request.Request(f"{REST}/markets?status=open&limit=100",
                                 headers={"User-Agent": "cross-arb/1.0"})
    mkts = json.load(urllib.request.urlopen(req, timeout=20)).get("markets", [])
    mkts.sort(key=lambda m: -(m.get("volume") or 0))           # busiest first
    for m in mkts:
        if m.get("ticker") and (m.get("yes_bid") or m.get("volume")):
            return m["ticker"]
    return mkts[0]["ticker"] if mkts else None

def authed_validation(key_id, pem_path):
    import websocket  # websocket-client
    ts = str(int(time.time() * 1000))
    sig = rsa_pss_sign(pem_path, f"{ts}GET{WS_PATH}")
    headers = [f"KALSHI-ACCESS-KEY: {key_id}", f"KALSHI-ACCESS-TIMESTAMP: {ts}",
               f"KALSHI-ACCESS-SIGNATURE: {sig}"]
    print(f"\nconnecting (RSA-PSS signed handshake, key {key_id[:6]}...)")
    ws = websocket.create_connection(f"wss://{WS_HOST}{WS_PATH}", timeout=15, header=headers)
    print("  [101] HANDSHAKE ACCEPTED")
    ticker = pick_ticker()
    print(f"  subscribe orderbook_delta  market={ticker}")
    ws.send(json.dumps({"id": 1, "cmd": "subscribe",
                        "params": {"channels": ["orderbook_delta"], "market_tickers": [ticker]}}))
    ws.settimeout(8); seen = set()
    for _ in range(8):
        try:
            obj = json.loads(ws.recv())
        except Exception as e:
            print(f"  recv stop: {type(e).__name__}"); break
        t = obj.get("type", obj.get("cmd", "?")); seen.add(t)
        print(f"  <- type={t}  {json.dumps(obj)[:160]}")
        if t == "orderbook_delta":
            break
    ws.close()
    ok = {"subscribed", "orderbook_snapshot"} & seen or "orderbook_delta" in seen
    print("\nRESULT:", "PASS - Kalshi WS auth + orderbook_delta VERIFIED" if ok
          else "PARTIAL - handshake OK; check subscribe reply types above")

# --- multi-subscription semantics probe (todo 2026-06-10) ---------------------------------------
# Answers the three UNVERIFIED questions behind bot/monitor.py's single-subscription invariant
# (kalshi_stream cycles the WHOLE connection on every discovery add; prune never unsubscribes):
#   Q1  a SECOND `subscribe` on the same channel+connection -> error, or a 2nd sid?
#   Q2  is `seq` per-connection or per-sid? (does sid2 restart at 1? do counters interleave?)
#   Q3  `update_subscription` add_markets / delete_markets on the live sid -> ack shape? snapshot
#       for the added ticker? does the sid's seq stay CONTIGUOUS across the update (no-gap add)?
# READ-ONLY: market-data subscriptions only; no orders. Raw frames -> _data/kalshi_multisub_probe.jsonl

def _pick_tickers(n=5):
    """The n busiest OPEN markets (most delta traffic), skipping any that close within 15 min."""
    from datetime import datetime, timezone
    req = urllib.request.Request(f"{REST}/markets?status=open&limit=200",
                                 headers={"User-Agent": "cross-arb/1.0"})
    mkts = json.load(urllib.request.urlopen(req, timeout=20)).get("markets", [])
    mkts.sort(key=lambda m: -(m.get("volume") or 0))
    out = []
    for m in mkts:
        t = m.get("ticker")
        if not t or t in out:
            continue
        try:                                      # skip markets that could close mid-probe
            ct = datetime.fromisoformat(m["close_time"].replace("Z", "+00:00"))
            if (ct - datetime.now(timezone.utc)).total_seconds() < 900:
                continue
        except Exception:
            pass
        out.append(t)
        if len(out) == n:
            break
    return out

def multisub_probe(phase_secs=8.0):
    import websocket
    sys.path.insert(0, os.path.join(HERE, "..", "bot"))
    from kalshi_book import kalshi_ws_headers     # the bot's signer — no drifted private copy (L15)
    tks = _pick_tickers(5)
    if len(tks) < 4:
        print("not enough open markets to probe"); return
    A, B, C, D = tks[:4]
    print(f"markets: A={A}  B={B}  C={C}  D={D}")
    os.makedirs(os.path.join(HERE, "_data"), exist_ok=True)
    sink_path = os.path.join(HERE, "_data", "kalshi_multisub_probe.jsonl")
    sink = open(sink_path, "a", encoding="utf-8")
    sink.write(json.dumps({"_probe_start": time.time(), "tickers": tks}) + "\n")
    frames = []                                   # every parsed frame, arrival order, tagged _phase
    phase = ["P0"]
    last_sent_id = [0]

    def send(ws, obj):
        last_sent_id[0] = obj.get("id", last_sent_id[0])
        print(f"  -> {json.dumps(obj)}")
        sink.write(json.dumps({"_sent": obj, "_t": time.time(), "_phase": phase[0]}) + "\n")
        ws.send(json.dumps(obj))

    def collect(ws, secs, label):
        phase[0] = label
        end = time.time() + secs
        got = []
        while time.time() < end:
            try:
                raw = ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            except Exception as e:
                print(f"  recv error: {type(e).__name__}: {e}"); break
            try:
                o = json.loads(raw)
            except Exception:
                continue
            if not isinstance(o, dict):
                continue
            o["_t"] = time.time(); o["_phase"] = label
            got.append(o); frames.append(o)
            sink.write(json.dumps(o) + "\n")
            if o.get("type") not in ("orderbook_delta", "orderbook_snapshot"):  # control frames only
                print(f"  <- {json.dumps({k: v for k, v in o.items() if not k.startswith('_')})[:220]}")
        return got

    def digest(got, label):
        by = {}
        for f in got:
            m = f.get("msg") if isinstance(f.get("msg"), dict) else {}
            by.setdefault((f.get("type"), f.get("sid")), []).append((f.get("seq"), m.get("market_ticker")))
        print(f"  [{label}] digest:")
        for (t, sid), rows in sorted(by.items(), key=lambda kv: (str(kv[0][0]), str(kv[0][1]))):
            ss = [s for s, _ in rows if s is not None]
            tks_ = sorted({tk for _, tk in rows if tk})
            rng = f"  seq {min(ss)}..{max(ss)}" if ss else ""
            print(f"    {t:<20} sid={sid}  x{len(rows)}{rng}  {tks_[:4]}")

    def acks(got, want_id):
        ok = err = None
        for f in got:
            if f.get("id") == want_id:
                if f.get("type") == "error": err = f
                else: ok = f
        return ok, err

    hdrs = kalshi_ws_headers()
    print(f"\nconnecting wss://{WS_HOST}{WS_PATH} (bot signer)")
    ws = websocket.create_connection(f"wss://{WS_HOST}{WS_PATH}", timeout=15, header=hdrs)
    ws.settimeout(0.5)
    print("[101] HANDSHAKE ACCEPTED")

    print(f"\n[P1] baseline subscribe orderbook_delta [A,B] — {phase_secs:.0f}s")
    send(ws, {"id": 1, "cmd": "subscribe",
              "params": {"channels": ["orderbook_delta"], "market_tickers": [A, B]}})
    p1 = collect(ws, phase_secs, "P1"); digest(p1, "P1")
    sub1, err1 = acks(p1, 1)
    sid1 = (sub1 or {}).get("msg", {}).get("sid")
    print(f"  sid1={sid1}")
    if sid1 is None:
        print("ABORT: no sid from baseline subscribe"); ws.close(); sink.close(); return

    print(f"\n[P2] SECOND subscribe, same channel [C] — {phase_secs:.0f}s")
    send(ws, {"id": 2, "cmd": "subscribe",
              "params": {"channels": ["orderbook_delta"], "market_tickers": [C]}})
    p2 = collect(ws, phase_secs, "P2"); digest(p2, "P2")
    sub2, err2 = acks(p2, 2)
    sid2 = (sub2 or {}).get("msg", {}).get("sid")

    print(f"\n[P3] update_subscription ADD [D] on sid1={sid1} — {phase_secs:.0f}s")
    add_ok = add_err = None; add_params_used = None
    for i, params in enumerate([
            {"sids": [sid1], "market_tickers": [D], "action": "add_markets"},
            {"sid": sid1, "market_tickers": [D], "action": "add_markets"}]):
        send(ws, {"id": 3 + i, "cmd": "update_subscription", "params": params})
        got = collect(ws, phase_secs if i == 0 else 5.0, "P3")
        add_ok, add_err = acks(got, 3 + i)
        if add_ok:
            add_params_used = params; break
        if add_err:
            print(f"  variant {i} rejected: {json.dumps(add_err.get('msg'))[:160]}")
    p3_tail = collect(ws, 4.0, "P3")              # let post-add traffic accumulate
    digest([f for f in frames if f.get("_phase") == "P3"], "P3")

    if add_ok:
        print(f"\n[P3b] OVERLAP add [A] (already subscribed) on sid1 — 4s")
        send(ws, {"id": 6, "cmd": "update_subscription",
                  "params": dict(add_params_used, market_tickers=[A])})
        p3b = collect(ws, 4.0, "P3b"); digest(p3b, "P3b")

    print(f"\n[P4] update_subscription DELETE [B] on sid1 — {phase_secs:.0f}s")
    del_params = dict(add_params_used or {"sids": [sid1], "action": "add_markets"},
                      market_tickers=[B], action="delete_markets")
    send(ws, {"id": 7, "cmd": "update_subscription", "params": del_params})
    p4 = collect(ws, phase_secs, "P4"); digest(p4, "P4")
    del_ok, del_err = acks(p4, 7)
    ws.close(); sink.close()

    # ---- verdicts ------------------------------------------------------------------------------
    def contig(seqs):
        return all(b == a + 1 for a, b in zip(seqs, seqs[1:]))
    per_sid = {}
    for f in frames:
        if "seq" in f:
            per_sid.setdefault(f.get("sid"), []).append(f["seq"])
    all_seqs = [f["seq"] for f in frames if "seq" in f]
    d_snap = next((f for f in frames if f.get("type") == "orderbook_snapshot"
                   and isinstance(f.get("msg"), dict) and f["msg"].get("market_ticker") == D), None)
    del_t = next((f["_t"] for f in frames if f.get("id") == 7 and f.get("type") != "error"), None)
    b_after_del = [f for f in frames if del_t and f.get("_t", 0) > del_t + 1.0
                   and isinstance(f.get("msg"), dict) and f["msg"].get("market_ticker") == B]

    print("\n" + "=" * 78)
    print("Q1 second subscribe same channel:",
          f"NEW SID (sid2={sid2})" if sid2 is not None else
          f"ERROR ({json.dumps((err2 or {}).get('msg'))[:120]})" if err2 else "NO REPLY SEEN")
    for sid, seqs in sorted(per_sid.items(), key=lambda kv: str(kv[0])):
        print(f"Q2 sid={sid}: {len(seqs)} seq'd frames, first={seqs[0]} last={seqs[-1]}, "
              f"contiguous={contig(seqs)}")
    print(f"Q2 GLOBAL interleaved contiguous={contig(all_seqs)}  "
          f"(per-sid={'YES' if len(per_sid) > 1 and all(contig(s) for s in per_sid.values()) and not contig(all_seqs) else 'n/a or single-sid'})")
    print("Q3 add_markets ack:", f"type={add_ok.get('type')}  params={json.dumps(add_params_used)}"
          if add_ok else f"FAILED ({json.dumps((add_err or {}).get('msg'))[:120]})")
    print("Q3 snapshot for added ticker:",
          f"YES (sid={d_snap.get('sid')}, seq={d_snap.get('seq')})" if d_snap else "NOT SEEN")
    print("Q3 sid1 seq contiguous across add:", contig(per_sid.get(sid1, [])))
    print("Q4 delete_markets ack:", f"type={del_ok.get('type')}" if del_ok
          else f"FAILED ({json.dumps((del_err or {}).get('msg'))[:120]})" if del_err else "NO REPLY")
    print("Q4 deleted-ticker frames >1s after ack:", len(b_after_del))
    safe = bool(add_ok) and bool(d_snap) and contig(per_sid.get(sid1, []))
    print("-" * 78)
    print("VERDICT:", "NO-GAP ADD SAFE — update_subscription acks, snapshots the added ticker, and "
                      "sid seq stays contiguous" if safe else
                      "NO-GAP ADD NOT VERIFIED — keep the cycle-on-add invariant")
    print(f"raw frames appended to {sink_path}")

if __name__ == "__main__":
    if "--multisub" in sys.argv:
        try:
            secs = float(sys.argv[sys.argv.index("--secs") + 1]) if "--secs" in sys.argv else 8.0
        except Exception:
            secs = 8.0
        env = load_env(os.path.join(HERE, ".env"))
        if not env.get("KALSHI_ACCESS_KEY") or not env.get("KALSHI_PRIVATE_KEY_PATH"):
            need_creds_msg(); raise SystemExit(0)
        multisub_probe(phase_secs=secs)
        raise SystemExit(0)
    print("=== Kalshi WS endpoint (unauth existence check) ===")
    endpoint_probe()
    env = load_env(os.path.join(HERE, ".env"))
    key_id, pem = env.get("KALSHI_ACCESS_KEY", ""), env.get("KALSHI_PRIVATE_KEY_PATH", "")
    pem_abs = pem if os.path.isabs(pem) else os.path.join(HERE, pem) if pem else ""
    if not key_id or not pem or not os.path.exists(pem_abs):
        need_creds_msg()
        raise SystemExit(0)
    authed_validation(key_id, pem_abs)
