#!/usr/bin/env python3
"""Guard script to validate the skip report against the skip policy.

This script is meant to be run as a CI step, after pytest has generated
skip-report.json. It reads the JSON, validates it against the policy,
and exits with status 0 if all skips are legitimate, or 1 if any are not.

Usage:
    python tests/run_skip_guard.py [path/to/skip-report.json]

If the path is omitted, defaults to skip-report.json in the current directory.
"""

import json
import sys
from pathlib import Path

# Add the parent directory to sys.path so imports work when run as a script
sys.path.insert(0, str(Path(__file__).parent.parent))

from tests.skip_policy import validate_skips


def main():
    """Validate the skip report and exit with appropriate status."""
    # Get the report path (default to skip-report.json in current directory)
    report_path = Path(sys.argv[1] if len(sys.argv) > 1 else "skip-report.json")

    if not report_path.exists():
        print(f"Error: {report_path} not found", file=sys.stderr)
        return 1

    # Read the skip report
    try:
        with open(report_path) as f:
            skips = json.load(f)
    except json.JSONDecodeError as e:
        print(f"Error: Failed to parse {report_path}: {e}", file=sys.stderr)
        return 1

    # Validate against the policy
    is_valid, illegitimate_skips = validate_skips(skips)

    if is_valid:
        print(f"Guard OK: All {len(skips)} skipped test(s) are legitimate")
        return 0
    else:
        print(
            f"::error::Guard FAILED: {len(illegitimate_skips)} unrecognized skip(s) found:",
            file=sys.stderr,
        )
        for nodeid in illegitimate_skips:
            print(f"  {nodeid}", file=sys.stderr)
        print(
            "An unrecognized skip reason breaks the CI build. "
            "Add the skip to the legitimate policy in tests/skip_policy.py if it should be allowed.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
