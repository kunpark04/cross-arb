"""scripts/cli_revisions.py — measure the NWS CLI daily-max REVISION rate (settlement residual risk).

Quantifies the one open settlement risk from research/settlement-verification.md: the weather pair grades
off the same morning NWS Climatological Report (CLI) daily max on BOTH venues, but Kalshi can delay to
11 AM ET and take a *downward* morning correction while pmus locks at 8 AM -> on a bucket-boundary day a
downward CLI revision could split the two venues into opposite buckets (lose both legs). The size of that
risk is an empirical question: HOW OFTEN does the published daily max actually move after first issuance,
and how often DOWNWARD, and by how much?

`bot/monitor.py`'s cli_stream logs every distinct (station, report_date, max) it sees to
`_data/cli.jsonl`. This script reconstructs, per (station, report_date), the sequence of published maxes
and reports the revision rate, the downward-revision rate (the settlement-relevant direction), and the
drop-magnitude distribution.

  UPPER BOUND, read this caveat: a logged max change is a revision *anywhere across the day's issuances*,
  which is a SUPERSET of the narrow settlement window. The true divergence needs a downward correction
  landing specifically in the 8-11 AM ET window AND straddling a listed bucket boundary. So the
  "ever-downward" rate here OVER-states the settlement-loss rate -- it bounds it from above. Treat a low
  number as reassuring and a high number as "go measure the window precisely."

READ-ONLY. Pure stdlib. `--selftest` runs the offline check on synthetic rows.

  python scripts/cli_revisions.py --selftest
  python scripts/cli_revisions.py [--data PATH] [--station NYC]
"""
import os, sys, json, argparse
from collections import defaultdict

DEFAULT_DATA = os.path.join(os.path.dirname(__file__), "_data", "cli.jsonl")


def load(path):
    """Read cli.jsonl -> list of records. Skips blank/corrupt lines (a partial last line on a live tail)."""
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path, "r", encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                rows.append(json.loads(ln))
            except (ValueError, json.JSONDecodeError):
                continue
    return rows


def chains(rows):
    """Group rows by (station, report_date); within each, order by log time t and collapse consecutive
    duplicate maxes (process restarts re-log the current value) -> the distinct-max chain in time order.

    Returns {(station, report_date): [(t, max, issued), ...]} with no two adjacent maxes equal."""
    groups = defaultdict(list)
    for r in rows:
        if "station" not in r or "report_date" not in r or "max" not in r:
            continue
        groups[(r["station"], r["report_date"])].append((r.get("t", 0), r["max"], r.get("issued", "")))
    out = {}
    for key, seq in groups.items():
        seq.sort(key=lambda x: x[0])
        chain = []
        for t, mx, iss in seq:
            if chain and chain[-1][1] == mx:      # same max as the previous distinct entry -> not a revision
                continue
            chain.append((t, mx, iss))
        out[key] = chain
    return out


def stat_group(chain):
    """Per (station, report_date) revision stats from its distinct-max chain (time-ordered)."""
    maxes = [mx for _, mx, _ in chain]
    n_distinct = len(set(maxes))
    revised = n_distinct > 1
    # downward = any later max strictly below an earlier one (the settlement-relevant direction)
    ever_down = False
    max_drop = 0
    running_hi = maxes[0] if maxes else None
    for mx in maxes:
        if running_hi is not None and mx < running_hi:
            ever_down = True
            max_drop = max(max_drop, running_hi - mx)
        if running_hi is None or mx > running_hi:
            running_hi = mx
    return {
        "n_obs": len(maxes),
        "n_distinct": n_distinct,
        "revised": revised,
        "first": maxes[0] if maxes else None,
        "last": maxes[-1] if maxes else None,
        "net": (maxes[-1] - maxes[0]) if maxes else 0,
        "ever_down": ever_down,
        "max_drop": max_drop,
    }


def analyze(rows):
    """Aggregate revision stats across all (station, report_date) groups."""
    ch = chains(rows)
    groups = {k: stat_group(v) for k, v in ch.items()}
    n = len(groups)
    revised = [k for k, g in groups.items() if g["revised"]]
    downward = [k for k, g in groups.items() if g["ever_down"]]
    drops = sorted((groups[k]["max_drop"] for k in downward), reverse=True)
    return {
        "n_station_days": n,
        "n_revised": len(revised),
        "n_downward": len(downward),
        "revised_rate": (len(revised) / n) if n else 0.0,
        "downward_rate": (len(downward) / n) if n else 0.0,
        "drops": drops,
        "groups": groups,
        "chains": ch,
    }


def report(rows, station=None):
    if station:
        rows = [r for r in rows if r.get("station") == station]
    a = analyze(rows)
    L = []
    L.append("=" * 68)
    L.append("NWS CLI daily-max revision rate  (settlement residual-risk gauge)")
    L.append("=" * 68)
    n = a["n_station_days"]
    if n == 0:
        L.append("No CLI observations yet. The logger writes _data/cli.jsonl as it runs;")
        L.append("revisions accrue over days. Re-run once data has accumulated.")
        return "\n".join(L)
    L.append(f"station-days observed : {n}")
    L.append(f"  revised (max moved) : {a['n_revised']:>4}   ({a['revised_rate']*100:5.1f}%)")
    L.append(f"  DOWNWARD (>=1 drop) : {a['n_downward']:>4}   ({a['downward_rate']*100:5.1f}%)  <- settlement-relevant")
    if a["drops"]:
        L.append(f"  downward drops (F)  : max {a['drops'][0]}, "
                 f"median {a['drops'][len(a['drops'])//2]}, n={len(a['drops'])}")
    L.append("")
    L.append("note: 'downward' counts ANY intra-day downward max move -- an UPPER BOUND on the")
    L.append("      settlement-loss rate, which also needs the move in the 8-11am ET window AND")
    L.append("      across a listed bucket boundary. Low here = reassuring; high = measure precisely.")
    # show the revised groups (the interesting ones) in detail
    rev = [(k, g) for k, g in a["groups"].items() if g["revised"]]
    if rev:
        L.append("")
        L.append("revised station-days:")
        for (st, dt), g in sorted(rev):
            arrow = "DOWN" if g["ever_down"] else "up"
            ch = a["chains"][(st, dt)]
            seq = " -> ".join(str(mx) for _, mx, _ in ch)
            L.append(f"  {st} {dt}: {seq}   [{arrow}, net {g['net']:+d}F, drop {g['max_drop']}F]")
    else:
        L.append("")
        L.append("No max revisions observed in this window (every station-day published a stable max).")
    return "\n".join(L)


def _selftest():
    # synthetic cli.jsonl rows: NYC stable, LAX downward correction, MDW upward, MIA restart-dup (no revision)
    rows = [
        {"t": 100, "station": "NYC", "report_date": "2026-06-08", "max": 75, "issued": "a"},
        {"t": 200, "station": "NYC", "report_date": "2026-06-08", "max": 75, "issued": "b"},  # dup -> ignored
        {"t": 100, "station": "LAX", "report_date": "2026-06-08", "max": 72, "issued": "a"},
        {"t": 300, "station": "LAX", "report_date": "2026-06-08", "max": 70, "issued": "b"},  # DOWN 2F
        {"t": 100, "station": "MDW", "report_date": "2026-06-08", "max": 80, "issued": "a"},
        {"t": 300, "station": "MDW", "report_date": "2026-06-08", "max": 83, "issued": "b"},  # up 3F
        {"t": 100, "station": "MIA", "report_date": "2026-06-08", "max": 91, "issued": "a"},
        {"t": 250, "station": "MIA", "report_date": "2026-06-08", "max": 91, "issued": "a"},  # restart dup
    ]
    a = analyze(rows)
    assert a["n_station_days"] == 4, a["n_station_days"]
    assert a["n_revised"] == 2, a["n_revised"]                 # LAX + MDW
    assert a["n_downward"] == 1, a["n_downward"]               # LAX only
    assert a["drops"] == [2], a["drops"]
    g = a["groups"][("LAX", "2026-06-08")]
    assert g["ever_down"] and g["max_drop"] == 2 and g["net"] == -2, g
    g = a["groups"][("MDW", "2026-06-08")]
    assert g["revised"] and not g["ever_down"] and g["net"] == 3, g
    g = a["groups"][("MIA", "2026-06-08")]
    assert not g["revised"] and g["n_obs"] == 1, g             # restart dup collapsed
    # ordering robustness: out-of-order log times still chain by t
    rows2 = [
        {"t": 300, "station": "SFO", "report_date": "2026-06-08", "max": 60, "issued": "b"},
        {"t": 100, "station": "SFO", "report_date": "2026-06-08", "max": 65, "issued": "a"},  # earlier -> 65 first
    ]
    g = analyze(rows2)["groups"][("SFO", "2026-06-08")]
    assert g["first"] == 65 and g["last"] == 60 and g["ever_down"] and g["max_drop"] == 5, g
    print("OK - cli_revisions: chain dedup, downward/upward classify, drop magnitude, t-ordering")


def main(argv=None):
    ap = argparse.ArgumentParser(description="NWS CLI daily-max revision rate (settlement residual risk).")
    ap.add_argument("--data", default=DEFAULT_DATA, help="path to cli.jsonl (default: scripts/_data/cli.jsonl)")
    ap.add_argument("--station", default=None, help="filter to one station (e.g. NYC)")
    ap.add_argument("--selftest", action="store_true", help="run the offline self-test and exit")
    args = ap.parse_args(argv)
    if args.selftest:
        _selftest()
        return
    rows = load(args.data)
    print(report(rows, station=args.station))


if __name__ == "__main__":
    main()
