"""Empirically confirm whether polymarket.us exposes a retail WebSocket (persistence-layer step 1).

Docs (docs.polymarket.us) document `wss://api.polymarket.us/v1/ws/markets`. This probe verifies the
endpoint is REAL at the network layer WITHOUT credentials or a WS client library: send an unauth'd
HTTP Upgrade handshake and read the status. A real WS endpoint rejects with 401 (auth required) or
426/400/101 (upgrade semantics) — NOT 404. A bogus control path should 404. Read-only; no orders,
no secret used. See research/polymarketus-api-auth.md."""
import http.client, ssl, os, base64

HOST = "api.polymarket.us"
WS_PATH = "/v1/ws/markets"
CONTROL = "/v1/ws/__nonexistent_probe_xyz__"

def handshake(path):
    """Send a bare WebSocket upgrade GET (no auth) and return (status, reason, interesting headers)."""
    key = base64.b64encode(os.urandom(16)).decode()
    conn = http.client.HTTPSConnection(HOST, timeout=20, context=ssl.create_default_context())
    try:
        conn.request("GET", path, headers={
            "Host": HOST, "Upgrade": "websocket", "Connection": "Upgrade",
            "Sec-WebSocket-Version": "13", "Sec-WebSocket-Key": key,
            "User-Agent": "cross-arb-ws-probe/1.0",
        })
        r = conn.getresponse()
        body = r.read(200).decode("utf-8", "ignore")
        hdrs = {k: v for k, v in r.getheaders()
                if k.lower() in ("www-authenticate", "sec-websocket-accept", "upgrade", "connection")}
        return r.status, r.reason, hdrs, body
    finally:
        conn.close()

def verdict(status):
    if status == 404: return "NOT FOUND (endpoint does not exist)"
    if status == 101: return "SWITCHING PROTOCOLS (WS accepted unauth — unexpected but exists)"
    if status == 401: return "EXISTS, auth required (expected for the markets WS)"
    if status in (426, 400, 403): return f"EXISTS (HTTP {status} on bad/unauth upgrade)"
    return f"indeterminate (HTTP {status})"

print(f"probing  wss://{HOST}{WS_PATH}  (unauthenticated handshake)\n")
for label, path in (("WS markets ", WS_PATH), ("control 404", CONTROL)):
    try:
        st, reason, hdrs, body = handshake(path)
        print(f"[{st} {reason}] {label}  {path}")
        print(f"    verdict: {verdict(st)}")
        if hdrs: print(f"    headers: {hdrs}")
        if body.strip(): print(f"    body: {body.strip()[:160]}")
    except Exception as e:
        print(f"[ERR] {label}  {path}  {type(e).__name__}: {str(e)[:120]}")
    print()

print("INTERPRETATION: markets path non-404 (esp. 401/426) AND control = 404  =>  WS endpoint is REAL.")
