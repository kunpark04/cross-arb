"""Step 2a — AUTHENTICATED polymarket.us WebSocket handshake: confirm the signed-string for the
WS upgrade and capture a live market-data snapshot. READ-ONLY (market-data subscribe; no orders).

The one residual unknown from step 1 (decision 0005): the REST scheme signs "{ts}{method}{path}" —
does the WS upgrade sign the same string? This probe tries the canonical format first, then a couple
of variants, and reports which (if any) yields a 101 handshake. On success it subscribes to one live
market and prints the snapshot top-of-book (masked creds; secret/signature never printed).

Creds from scripts/.env (PMUS_ACCESS_KEY / PMUS_SECRET). See research/polymarketus-api-auth.md §3c.
"""
import os, sys, time, json, base64, urllib.request
try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows cp1252 consoles
except Exception: pass
import websocket  # websocket-client (synchronous)
from cryptography.hazmat.primitives.asymmetric import ed25519

HERE = os.path.dirname(__file__)
WS_URL = "wss://api.polymarket.us/v1/ws/markets"
WS_PATH = "/v1/ws/markets"

def load_env(p):
    d = {}
    for line in open(p):
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            k, v = s.split("=", 1); d[k.strip()] = v.strip()
    return d

env = load_env(os.path.join(HERE, ".env"))
KEY_ID, SECRET = env.get("PMUS_ACCESS_KEY"), env.get("PMUS_SECRET")
assert KEY_ID and SECRET, "PMUS_ACCESS_KEY / PMUS_SECRET missing from scripts/.env"
priv = ed25519.Ed25519PrivateKey.from_private_bytes(base64.b64decode(SECRET)[:32])
mask = KEY_ID[:8] + "..." + KEY_ID[-4:]
print(f"key id {mask} (secret len {len(SECRET)}, not shown)\n")

def headers_for(sign_msg):
    ts = str(int(time.time() * 1000))
    sig = base64.b64encode(priv.sign(sign_msg(ts).encode())).decode()
    return [f"X-PM-Access-Key: {KEY_ID}", f"X-PM-Timestamp: {ts}", f"X-PM-Signature: {sig}"]

# Candidate signed-string formats to try, most-likely first (REST format == canonical).
CANDIDATES = [
    ("REST format  {ts}GET{path}",        lambda ts: f"{ts}GET{WS_PATH}"),
    ("lowercase method {ts}get{path}",    lambda ts: f"{ts}get{WS_PATH}"),
    ("trailing slash {ts}GET{path}/",     lambda ts: f"{ts}GET{WS_PATH}/"),
]

def public_book(slug):
    """PUBLIC (no-auth) REST book — used to pick a slug that currently HAS live depth."""
    try:
        req = urllib.request.Request(f"https://gateway.polymarket.us/v1/markets/{slug}/book",
                                     headers={"User-Agent": "cross-arb/1.0"})
        md = json.load(urllib.request.urlopen(req, timeout=15)).get("marketData", {})
        return md.get("bids", []), md.get("offers", [])
    except Exception:
        return [], []

def pick_slug():
    """Return (slug, n_bids, n_offers) for an OPEN market whose PUBLIC book has depth NOW — so the WS
    subscribe has something live to stream. Throttled to respect the 20 req/s IP cap."""
    # NB: ?active=true returns stale backfilled markets; ?closed=false is the live filter.
    # Query today's weather (climate) markets — the co-listed universe most likely 2-sided now.
    req = urllib.request.Request("https://gateway.polymarket.us/v1/markets?categories[]=climate&closed=false&limit=60",
                                 headers={"User-Agent": "cross-arb/1.0"})
    mkts = (json.load(urllib.request.urlopen(req, timeout=20)).get("markets") or [])
    mkts.sort(key=lambda m: bool(m.get("closed")))  # open (closed=False) first
    checked = 0
    for m in mkts[:20]:                      # cap candidates; throttle to avoid 429
        s = m.get("slug")
        if not s:
            continue
        b, o = public_book(s); checked += 1
        if b or o:
            return s, len(b), len(o)
        time.sleep(0.2)
    print(f"  [diag] scanned {len(mkts)} active markets, checked {checked} books, none with depth.")
    print(f"  [diag] sample: {[(m.get('slug'), m.get('category'), m.get('state', m.get('marketState'))) for m in mkts[:5]]}")
    return None, 0, 0

def try_connect(label, sign_msg):
    try:
        ws = websocket.create_connection(WS_URL, timeout=15, header=headers_for(sign_msg))
        return ws
    except Exception as e:
        code = getattr(e, "status_code", None)
        print(f"  [{code or 'ERR'}] {label}  ->  {type(e).__name__}: {str(e)[:90]}")
        return None

print("=== handshake: trying candidate signed-strings ===")
ws, used = None, None
for label, fn in CANDIDATES:
    ws = try_connect(label, fn)
    if ws:
        used = label
        print(f"  [101] {label}  ->  HANDSHAKE ACCEPTED")
        break

if not ws:
    raise SystemExit("\nRESULT: no candidate signed-string was accepted — see statuses above.")

print(f"\nVERIFIED signed-string for WS upgrade: '{used}'")

# --- subscribe to one live market & read the snapshot (try camelCase then snake_case) ---
slug, nb, no = pick_slug()
if not slug:
    print("\nNo live-book market found to test subscribe — but the HANDSHAKE (signed-string) is verified above.")
    try: ws.close()
    except Exception: pass
    raise SystemExit("RESULT: HANDSHAKE VERIFIED; subscribe untested (no live slug this run).")
print(f"\n=== subscribe SUBSCRIPTION_TYPE_MARKET_DATA  slug={slug}  (public book now: {nb} bids / {no} offers) ===")
subs = [
    {"subscribe": {"requestId": "md-1", "subscriptionType": "SUBSCRIPTION_TYPE_MARKET_DATA",
                   "marketSlugs": [slug]}},
    {"subscribe": {"request_id": "md-1", "subscription_type": "SUBSCRIPTION_TYPE_MARKET_DATA",
                   "market_slugs": [slug]}},
]
ws.settimeout(8)
got = False
for sub in subs:
    ws.send(json.dumps(sub))
    for _ in range(6):
        try:
            msg = ws.recv()
        except Exception as e:
            print(f"  recv stop: {type(e).__name__}"); break
        if not msg:
            continue
        print(f"  <- {str(msg)[:220]}")
        try:
            obj = json.loads(msg)
        except Exception:
            continue
        md = obj.get("marketData") or obj.get("market_data") or {}
        if md:
            bids, offers = md.get("bids", []), md.get("offers", [])
            tb = bids[0]["px"]["value"] if bids else "-"
            ta = offers[0]["px"]["value"] if offers else "-"
            print(f"  SNAPSHOT {md.get('marketSlug', slug)}  state={md.get('state')}  "
                  f"bestBid={tb} bestAsk={ta}  bids={len(bids)} offers={len(offers)}  "
                  f"eof={obj.get('eof')}")
            got = True
        else:
            print(f"  msg keys: {list(obj.keys())}  {json.dumps(obj)[:140]}")
        if obj.get("eof"):
            break
    if got:
        break

try: ws.close()
except Exception: pass
print("\nRESULT:", "PASS - WS auth + market-data subscribe VERIFIED" if got
      else "PARTIAL — handshake OK but no snapshot parsed (check subscribe field casing above)")
