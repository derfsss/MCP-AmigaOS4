"""Run any combination of validation rounds against an arbitrary
endpoint. Wraps the standalone roundN_validation.py scripts (which
read $MCPD_ENDPOINT for their target).

Usage:
    python scripts/validate.py [--endpoint host:port] [--rounds 1,2,3,4,5]

Defaults:
    --endpoint  $MCPD_ENDPOINT, or required if env unset
    --rounds    1,2,3,4,5

Examples:
    python scripts/validate.py --endpoint 192.168.0.10:4322
    python scripts/validate.py --endpoint 127.0.0.1:4422 --rounds 1,3
    python scripts/validate.py --rounds 1,2,3,4,5,1,2,3,4,5
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _resolve_round_script(round_num: int) -> Path | None:
    """Look up the validation script for a round number. Round 5's
    script has a longer name (`round5_serverpush_validation.py`); this
    resolver hides that from callers."""
    candidates = [
        HERE / f"round{round_num}_validation.py",
        HERE / f"round{round_num}_serverpush_validation.py",
    ]
    for path in candidates:
        if path.exists():
            return path
    return None


def run_one_round(round_num: int, endpoint: str) -> int:
    src = _resolve_round_script(round_num)
    if src is None:
        print(f"  [skip] no script found for round{round_num}")
        return 0

    print(f"\n===== round{round_num} against {endpoint} =====")
    env = {**os.environ, "MCPD_ENDPOINT": endpoint}
    rc = subprocess.run(
        [sys.executable, str(src)],
        cwd=str(HERE.parent),
        text=True, env=env,
    )
    return rc.returncode


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--endpoint", default=os.environ.get("MCPD_ENDPOINT"),
        help="MCPd endpoint to test (host:port). "
             "Default: $MCPD_ENDPOINT.",
    )
    p.add_argument(
        "--rounds", default="1,2,3,4,5",
        help="Comma-separated round numbers to run",
    )
    args = p.parse_args()
    if not args.endpoint:
        p.error("--endpoint or $MCPD_ENDPOINT is required")

    rounds = [int(x) for x in args.rounds.split(",") if x.strip()]
    failures = 0
    for r in rounds:
        rc = run_one_round(r, args.endpoint)
        if rc != 0:
            failures += 1
            print(f"  FAIL round{r} rc={rc}")

    print()
    print(f"[summary] endpoint={args.endpoint}  "
          f"rounds={rounds}  failures={failures}/{len(rounds)}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
