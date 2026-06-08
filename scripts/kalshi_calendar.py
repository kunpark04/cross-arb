"""Kalshi forward calendar per matched series: pull all markets (any status), aggregate
expiration-date x status, and show the soonest OPEN settle date. Backoff on 429.
Determines (a) weather daily-window, (b) whether near-term econ contracts overlap PM.us. Read-only."""
import json, urllib.request, urllib.error, time, collections
from datetime import datetime, timezone

UA = {"User-Agent": "Mozilla/5.0 (cross-arb-audit; research)"}
NOW = datetime.now(timezone.utc)

def get(url, tries=4):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=45) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429 and i < tries - 1:
                time.sleep(3 * (i + 1)); continue
            raise
    return {}

def parse(s):
    try: return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception: return None

def pull_all(tk):
    out, cursor = [], None
    for _ in range(8):  # cap pages
        url = f"https://external-api.kalshi.com/trade-api/v2/markets?series_ticker={tk}&limit=1000"
        if cursor: url += f"&cursor={cursor}"
        d = get(url)
        out.extend(d.get("markets", []))
        cursor = d.get("cursor")
        if not cursor: break
        time.sleep(1.2)
    return out

series = {
    "Weather Miami": "KXHIGHMIA", "Weather Chicago": "KXHIGHCHI",
    "CPI YoY": "KXCPIYOY", "CPI (legacy)": "CPIYOY", "Unemployment U3": "KXU3",
    "Payrolls": "KXPAYROLLS", "Fed funds": "FED", "Fed decision": "FEDDECISION", "GDP": "GDP",
}
for label, tk in series.items():
    try:
        mk = pull_all(tk)
    except Exception as e:
        print(f"\n{label:16}({tk}): ERROR {type(e).__name__}: {e}"); continue
    if not mk:
        print(f"\n{label:16}({tk}): no markets returned"); continue
    # status histogram
    sth = collections.Counter(x.get("status") for x in mk)
    # open markets: soonest/farthest expiration
    opens = [x for x in mk if x.get("status") == "active" or x.get("status") == "open"]
    exp_open = sorted({str(x.get("expiration_time"))[:10] for x in opens if x.get("expiration_time")})
    soonest = exp_open[0] if exp_open else None
    dsoon = None
    if soonest:
        e = parse(soonest + "T00:00:00Z")
        dsoon = (e - NOW).days
    print(f"\n{label:16}({tk}): {len(mk)} mkts  status={dict(sth)}")
    print(f"   OPEN settle-dates: {len(exp_open)} distinct; soonest={soonest} (~{dsoon}d out); farthest={exp_open[-1] if exp_open else None}")
    if exp_open:
        print(f"   next few open settle-dates: {exp_open[:8]}")
