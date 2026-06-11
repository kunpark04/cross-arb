"""scripts/selftest_all.py — run EVERY offline self-test in the repo and report one PASS/FAIL table.

The single entry point for "did I break anything": each bot core module runs its own self-verifying
harness with no args; each analysis script runs `--selftest`. All offline (no network, no creds).
Exit code is non-zero if anything fails — usable as a pre-commit / pre-deploy gate.

  python scripts/selftest_all.py
"""
import os, subprocess, sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

TARGETS = [
    ("bot/ledger.py", []),
    ("bot/kalshi_book.py", []),
    ("bot/monitor.py", []),
    ("bot/colisted_map.py", []),
    ("scripts/test_monitor_nogap.py", []),   # localhost fake-WS integration: no-gap add/delete + fallback
    ("scripts/test_monitor_trades_ladders.py", []),   # wave-2: trade poll dedupe/seed + tr/hb ladders + fee tripwire
    ("scripts/analyze_persistence.py", ["--selftest"]),
    ("scripts/capital_sim.py", ["--selftest"]),
    ("scripts/account_sim.py", ["--selftest"]),
    ("scripts/alloc_policy_experiment.py", ["--selftest"]),
    ("scripts/clip_threshold_test.py", ["--selftest"]),
    ("scripts/shadow_fill.py", ["--selftest"]),
    ("scripts/cli_revisions.py", ["--selftest"]),
    ("scripts/adverse_selection.py", ["--selftest"]),
    ("scripts/exit_liquidity.py", ["--selftest"]),
    ("scripts/capital_velocity.py", ["--selftest"]),
    ("scripts/settle_recon.py", ["--selftest"]),
    ("scripts/latency_probe.py", ["--selftest"]),
    ("scripts/weather_depth.py", ["--selftest"]),
]


def main():
    fails = []
    print(f"running {len(TARGETS)} offline self-tests\n")
    for rel, args in TARGETS:
        r = subprocess.run([sys.executable, os.path.join(ROOT, rel)] + args,
                           capture_output=True, text=True, cwd=ROOT, timeout=300)
        ok = r.returncode == 0
        print(f"  {'PASS' if ok else 'FAIL':4}  {rel}")
        if not ok:
            fails.append(rel)
            tail = "\n".join((r.stdout + "\n" + r.stderr).strip().splitlines()[-12:])
            print("        " + tail.replace("\n", "\n        "))
    print(f"\n{len(TARGETS) - len(fails)}/{len(TARGETS)} passed" + (f"  FAILED: {fails}" if fails else " — ALL GREEN"))
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
