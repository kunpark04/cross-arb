"""One-shot polymarket.us credential + Ed25519 auth-scheme verification.
Fires a SIGNED GET at a private endpoint (no orders). Reads creds from scripts/.env.
NEVER prints the secret or the signature; masks the key id. Read-only."""
import os, time, base64, json, urllib.request, urllib.error

ENV = os.path.join(os.path.dirname(__file__), ".env")
def load_env(p):
    d = {}
    with open(p) as f:
        for line in f:
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s: continue
            k, v = s.split("=", 1); d[k.strip()] = v.strip()
    return d

env = load_env(ENV)
KEY_ID, SECRET = env.get("PMUS_ACCESS_KEY"), env.get("PMUS_SECRET")
assert KEY_ID and SECRET and "PASTE" not in KEY_ID, "PMUS_ACCESS_KEY / PMUS_SECRET not set in scripts/.env"
mask = KEY_ID[:8] + "..." + KEY_ID[-4:]
print(f"loaded key id {mask}  (secret length {len(SECRET)} chars, not shown)")

from cryptography.hazmat.primitives.asymmetric import ed25519
try:
    priv = ed25519.Ed25519PrivateKey.from_private_bytes(base64.b64decode(SECRET)[:32])
except Exception as e:
    raise SystemExit(f"could not load Ed25519 key from secret: {type(e).__name__}: {e}")

def headers(method, path):
    ts = str(int(time.time() * 1000))
    sig = base64.b64encode(priv.sign(f"{ts}{method}{path}".encode())).decode()
    return {"X-PM-Access-Key": KEY_ID, "X-PM-Timestamp": ts, "X-PM-Signature": sig,
            "Content-Type": "application/json", "User-Agent": "cross-arb-verify/1.0"}

HOST = "https://api.polymarket.us"
# try a few plausible private (auth-required) read paths; first 200/403 => auth scheme works
paths = ["/v1/portfolio/positions", "/v1/portfolio/balance", "/v1/portfolio",
         "/v1/account", "/v1/me", "/v1/orders"]
ok = False
for path in paths:
    try:
        req = urllib.request.Request(HOST + path, headers=headers("GET", path))
        with urllib.request.urlopen(req, timeout=20) as r:
            body = r.read()[:240].decode("utf-8", "ignore")
            print(f"[{r.status}] GET {path}  -> AUTH ACCEPTED. body: {body}")
            ok = True; break
    except urllib.error.HTTPError as e:
        msg = e.read()[:180].decode("utf-8", "ignore")
        verdict = ("auth OK (endpoint forbidden/empty)" if e.code in (403,) else
                   "auth REJECTED" if e.code in (401,) else
                   "endpoint not found (auth indeterminate)" if e.code == 404 else "?")
        print(f"[{e.code}] GET {path}  -> {verdict}  {msg}")
        if e.code in (200, 403):
            ok = True; break
    except Exception as e:
        print(f"[ERR] GET {path}  {type(e).__name__}: {str(e)[:90]}")

print("\nRESULT:", "PASS: credentials + Ed25519 signing VERIFIED" if ok else
      "FAIL: no endpoint returned 200/403 - see statuses above (401=bad key/scheme, 404=wrong paths)")
