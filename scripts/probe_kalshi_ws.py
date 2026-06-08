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

if __name__ == "__main__":
    print("=== Kalshi WS endpoint (unauth existence check) ===")
    endpoint_probe()
    env = load_env(os.path.join(HERE, ".env"))
    key_id, pem = env.get("KALSHI_ACCESS_KEY", ""), env.get("KALSHI_PRIVATE_KEY_PATH", "")
    pem_abs = pem if os.path.isabs(pem) else os.path.join(HERE, pem) if pem else ""
    if not key_id or not pem or not os.path.exists(pem_abs):
        need_creds_msg()
        raise SystemExit(0)
    authed_validation(key_id, pem_abs)
