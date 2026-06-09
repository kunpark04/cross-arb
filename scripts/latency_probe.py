"""scripts/latency_probe.py - measure READ-PATH latency to BOTH venues as a LOWER BOUND on order latency.

Why this exists: your order round-trip can't beat your data round-trip. The public REST GET RTT is the
floor for how fast you could ever get an order acknowledged from the same host/network. If that floor is
tens-to-hundreds of ms, then the question "Rust vs Python compute latency" is irrelevant noise next to the
network wall-clock - the strategy is NOT a microsecond game and you don't need a colocated low-level stack.

What it does (READ-ONLY, public endpoints, no auth):
  * Times a cheap REST GET round-trip with time.perf_counter on each venue:
      - Kalshi : GET /markets?limit=1
      - pmus   : GET /markets?limit=1
    ~SAMPLES samples per venue, spaced ~SPACING_MS, interleaved (alternating venues) so transient network
    weather hits both venues evenly rather than biasing one.
  * Reports per venue: p50 / p90 / p99 / max RTT (ms), plus mean/min and error count.
  * Reports the implied TWO-LEG execution floor (assuming order latency == read latency):
      - SERIAL     = Kalshi_rtt + pmus_rtt   (fire leg A, wait ack, then fire leg B)
      - CONCURRENT = max(Kalshi_rtt, pmus_rtt) (fire both legs in parallel, wait for the slower ack)
    computed both at p50 (typical) and p99 (tail) so you see the bad-case floor too.
  * One-line verdict: microsecond game (no) vs tens-to-hundreds-of-ms game.

  python scripts/latency_probe.py            # live (network) - default
  python scripts/latency_probe.py --selftest # offline synthetic check of the stats/floor math
  python scripts/latency_probe.py --samples 40 --spacing-ms 150 --timeout 8
"""
import sys, time, json, argparse, urllib.request, urllib.error

try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass

KAL = "https://api.elections.kalshi.com/trade-api/v2/markets?limit=1"
PM  = "https://gateway.polymarket.us/v1/markets?limit=1"
UA  = {"User-Agent": "cross-arb/1.0", "Accept": "application/json"}

VENUES = [("kalshi", KAL), ("pmus", PM)]


def time_get(url, timeout):
    """Return (rtt_ms, ok). Times the full GET round-trip incl. reading the (tiny) body. ok=False on any
    error (HTTP or network); we still record the elapsed time but exclude it from the latency percentiles."""
    req = urllib.request.Request(url, headers=UA)
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read()  # drain the body so the RTT includes the full response, not just headers
        dt = (time.perf_counter() - t0) * 1000.0
        return dt, True
    except urllib.error.HTTPError as e:
        # got a response (server reachable) but non-2xx; time is still a valid network RTT, but flag it
        dt = (time.perf_counter() - t0) * 1000.0
        return dt, (200 <= e.code < 300)
    except Exception:
        dt = (time.perf_counter() - t0) * 1000.0
        return dt, False


def pct(sorted_vals, p):
    """Nearest-rank percentile on an already-sorted list. p in [0,100]. Empty -> None."""
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    # nearest-rank: rank = ceil(p/100 * n), 1-indexed
    import math
    rank = max(1, math.ceil(p / 100.0 * len(sorted_vals)))
    return sorted_vals[min(rank, len(sorted_vals)) - 1]


def summarize(samples):
    """samples: list of (rtt_ms, ok). Returns a dict of stats over the OK samples only."""
    ok_vals = sorted(r for r, ok in samples if ok)
    errs = sum(1 for _, ok in samples if not ok)
    n = len(ok_vals)
    s = {"n_ok": n, "n_err": errs}
    if n:
        s["min"] = ok_vals[0]
        s["max"] = ok_vals[-1]
        s["mean"] = sum(ok_vals) / n
        s["p50"] = pct(ok_vals, 50)
        s["p90"] = pct(ok_vals, 90)
        s["p99"] = pct(ok_vals, 99)
    return s


def collect(samples, spacing_ms, timeout):
    """Interleave venues: round 1 = kalshi, pmus; round 2 = kalshi, pmus; ... spacing between each call.
    Returns {venue: [(rtt_ms, ok), ...]}."""
    out = {name: [] for name, _ in VENUES}
    spacing_s = spacing_ms / 1000.0
    total = samples * len(VENUES)
    done = 0
    for i in range(samples):
        for name, url in VENUES:
            rtt, ok = time_get(url, timeout)
            out[name].append((rtt, ok))
            done += 1
            sys.stderr.write("\r  probing %d/%d ..." % (done, total))
            sys.stderr.flush()
            if done < total:
                time.sleep(spacing_s)
    sys.stderr.write("\r" + " " * 40 + "\r")
    sys.stderr.flush()
    return out


def fmt(x):
    return "  n/a" if x is None else "%6.1f" % x


def report(stats):
    """stats: {venue: summary_dict}. Prints per-venue table + the two-leg floor. Returns the verdict dict."""
    print("=" * 64)
    print("READ-PATH LATENCY (REST GET RTT, ms) - lower bound on order latency")
    print("=" * 64)
    print("%-8s %7s %7s %7s %7s %7s %7s  %4s/%-4s" %
          ("venue", "p50", "p90", "p99", "max", "min", "mean", "ok", "err"))
    for name, _ in VENUES:
        s = stats[name]
        if not s.get("n_ok"):
            print("%-8s  (no successful samples; %d errors)" % (name, s.get("n_err", 0)))
            continue
        print("%-8s %s %s %s %s %s %s  %4d/%-4d" % (
            name, fmt(s["p50"]), fmt(s["p90"]), fmt(s["p99"]), fmt(s["max"]),
            fmt(s["min"]), fmt(s["mean"]), s["n_ok"], s["n_err"]))

    k, p = stats["kalshi"], stats["pmus"]
    verdict = {}
    if k.get("n_ok") and p.get("n_ok"):
        print()
        print("-" * 64)
        print("TWO-LEG EXECUTION FLOOR  (assumes order latency == read latency)")
        print("-" * 64)
        for tag, key in (("typical (p50)", "p50"), ("tail    (p99)", "p99")):
            serial = k[key] + p[key]
            concurrent = max(k[key], p[key])
            print("  %-14s  SERIAL %7.1f ms   CONCURRENT %7.1f ms" % (tag, serial, concurrent))
            verdict["serial_" + key] = serial
            verdict["concurrent_" + key] = concurrent

        # verdict: worst typical single-leg p50 drives the game-speed classification
        worst_p50 = max(k["p50"], p["p50"])
        print()
        if worst_p50 < 1.0:
            tier = "MICROSECOND game (sub-ms) - compute latency matters"
        elif worst_p50 < 10.0:
            tier = "LOW-MILLISECOND game (<10ms) - fast colo helps"
        else:
            tier = "TENS-TO-HUNDREDS-OF-MS game - network dominates; Rust-vs-Python compute latency is NOISE"
        verdict["tier"] = tier
        verdict["worst_p50"] = worst_p50
        print("VERDICT: %s" % tier)
        print("         (worst-venue p50 single-leg RTT = %.1f ms; concurrent two-leg p50 floor = %.1f ms)"
              % (worst_p50, verdict["concurrent_p50"]))
    else:
        print("\n(insufficient successful samples on one venue to compute the two-leg floor)")
    return verdict


# ============================================================================================
# SELFTEST (offline, synthetic) - validates the percentile + floor math without the network
# ============================================================================================
def selftest():
    print("[selftest] percentile + summary + floor math")
    fails = 0

    # nearest-rank percentile on 1..100: p50->50, p90->90, p99->99, max->100
    vals = list(range(1, 101))
    for p, exp in ((50, 50), (90, 90), (99, 99), (100, 100)):
        got = pct(vals, p)
        ok = got == exp
        fails += not ok
        print("  pct(1..100, %3d) = %3s  expect %3d  %s" % (p, got, exp, "OK" if ok else "FAIL"))

    # single-element and empty
    fails += pct([42.0], 50) != 42.0
    fails += pct([], 50) is not None

    # summarize: 9 ok + 1 err; ok vals 10..90 by 10
    samp = [(float(v), True) for v in range(10, 100, 10)] + [(999.0, False)]
    s = summarize(samp)
    print("  summarize: n_ok=%d n_err=%d min=%.0f max=%.0f p50=%.0f" %
          (s["n_ok"], s["n_err"], s["min"], s["max"], s["p50"]))
    for cond, lbl in ((s["n_ok"] == 9, "n_ok==9"), (s["n_err"] == 1, "n_err==1"),
                      (s["min"] == 10.0, "min==10"), (s["max"] == 90.0, "max==90"),
                      (s["p50"] == 50.0, "p50==50")):
        if not cond:
            print("    FAIL %s" % lbl); fails += 1

    # floor math: serial = sum, concurrent = max
    stats = {"kalshi": summarize([(20.0, True)] * 5), "pmus": summarize([(35.0, True)] * 5)}
    v = report(stats)
    serial_ok = abs(v["serial_p50"] - 55.0) < 1e-6
    conc_ok = abs(v["concurrent_p50"] - 35.0) < 1e-6
    print("  floor: serial_p50=%.1f (exp 55.0 %s)  concurrent_p50=%.1f (exp 35.0 %s)" %
          (v["serial_p50"], "OK" if serial_ok else "FAIL",
           v["concurrent_p50"], "OK" if conc_ok else "FAIL"))
    fails += (not serial_ok) + (not conc_ok)
    # 20/35 ms worst-p50 must classify as tens-to-hundreds tier (network-dominated)
    tier_ok = "NOISE" in v["tier"]
    fails += not tier_ok

    print("\n[selftest] %s" % ("ALL PASS" if fails == 0 else "%d FAILURES" % fails))
    return 0 if fails == 0 else 1


def main():
    ap = argparse.ArgumentParser(description="read-path latency probe (lower bound on order latency)")
    ap.add_argument("--selftest", action="store_true", help="offline synthetic check; no network")
    ap.add_argument("--samples", type=int, default=40, help="samples per venue (default 40)")
    ap.add_argument("--spacing-ms", type=int, default=150, help="ms between calls (default 150)")
    ap.add_argument("--timeout", type=float, default=8.0, help="per-call timeout seconds (default 8)")
    args = ap.parse_args()

    if args.selftest:
        sys.exit(selftest())

    print("probing kalshi + pmus: %d samples/venue, ~%dms spacing, %.0fs timeout (interleaved)..."
          % (args.samples, args.spacing_ms, args.timeout))
    print("  kalshi: %s" % KAL)
    print("  pmus  : %s" % PM)
    print()
    raw = collect(args.samples, args.spacing_ms, args.timeout)
    stats = {name: summarize(raw[name]) for name, _ in VENUES}
    report(stats)


if __name__ == "__main__":
    main()
