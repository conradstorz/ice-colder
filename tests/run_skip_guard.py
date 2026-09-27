#!/usr/bin/env python3
"""Guard script to validate the skip report against the skip policy.

This script is meant to be run as a CI step, after pytest has generated
skip-report.json. It reads the JSON, validates it against the policy,
and exits with status 0 if all skips are legitimate, or 1 if any are not.

It fails closed on every way the report can go wrong -- missing file,
unparseable JSON, the explicit "could not collect skips" marker
(tests/skip_policy.py's MISSING_TERMINALREPORTER_MARKER), or a report that
parses but isn't the expected list shape -- rather than treating any of
those as "zero skips" and reporting a false Guard OK.

Usage:
    python tests/run_skip_guard.py [path/to/skip-report.json]

If the path is omitted, defaults to skip-report.json in the current directory.
"""

import json
import sys
from pathlib import Path

# Add the parent directory to sys.path so imports work when run as a script
sys.path.insert(0, str(Path(__file__).parent.parent))

from tests.skip_policy import is_error_marker, validate_skips


def check_report(report_path: Path) -> int:
    """Validate the skip report at *report_path* and return a process exit
    code (0 = all skips legitimate, 1 = guard failure).

    Split out from main() so tests can call it directly against a
    constructed report file, with no sys.argv patching needed.
    """
    if not report_path.exists():
        print(f"Error: {report_path} not found", file=sys.stderr)
        return 1

    try:
        with open(report_path) as f:
            report_data = json.load(f)
    except json.JSONDecodeError as e:
        print(
            f"::error::Guard FAILED: could not parse {report_path}: {e}",
            file=sys.stderr,
        )
        return 1

    if is_error_marker(report_data):
        print(
            "::error::Guard FAILED: the skip report could not be collected "
            f"this run: {report_data.get('detail', report_data)}",
            file=sys.stderr,
        )
        return 1

    if not isinstance(report_data, list):
        print(
            f"::error::Guard FAILED: {report_path} is not a valid skip report "
            f"(expected a JSON list, got {type(report_data).__name__})",
            file=sys.stderr,
        )
        return 1

    # Validate against the policy
    is_valid, illegitimate_skips = validate_skips(report_data)

    if is_valid:
        print(f"Guard OK: All {len(report_data)} skipped test(s) are legitimate")
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


def main() -> int:
    """Validate the skip report and exit with appropriate status."""
    # Get the report path (default to skip-report.json in current directory)
    report_path = Path(sys.argv[1] if len(sys.argv) > 1 else "skip-report.json")
    return check_report(report_path)


if __name__ == "__main__":
    sys.exit(main())
