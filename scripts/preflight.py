#!/usr/bin/env python3
"""FactoryPulse preflight: one REAL Gemini call each for inspection, triage and chat tool-calling.

Works from a laptop (ADC or GEMINI_API_KEY) and from Cloud Run (service-account ADC):

    python scripts/preflight.py                 # must hit live Gemini, exits 1 on any FAIL
    python scripts/preflight.py --allow-mock    # also accept offline results (CI / no credentials)
    python scripts/preflight.py --json          # machine-readable output
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from diagnostics import describe_environment, run_preflight  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--allow-mock", action="store_true", help="treat mock/fallback results as PASS")
    ap.add_argument("--json", action="store_true", help="print JSON instead of a table")
    args = ap.parse_args()

    rows = run_preflight(expect_live=not args.allow_mock)
    if args.json:
        print(json.dumps({"environment": describe_environment(), "rows": rows}, indent=2))
    else:
        print(f"Environment: {describe_environment()}\n")
        cols = ["check", "result", "provenance", "latency_ms", "attempts"]
        widths = {c: max(len(c), *(len(str(r[c])) for r in rows)) for c in cols}
        print("  ".join(c.upper().ljust(widths[c]) for c in cols))
        print("  ".join("-" * widths[c] for c in cols))
        for r in rows:
            print("  ".join(str(r[c]).ljust(widths[c]) for c in cols))
        print()
        for r in rows:
            print(f"[{r['result']}] {r['check']}: {r['detail']}")
    failed = [r for r in rows if r["result"] != "PASS"]
    print(f"\n{'ALL CHECKS PASSED' if not failed else f'{len(failed)} CHECK(S) FAILED'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
