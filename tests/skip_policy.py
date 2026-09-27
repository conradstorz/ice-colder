"""Skip policy: defines which skip reasons are legitimate in CI.

The policy is default-deny: any skip reason not in this policy fails the guard.
This ensures future tests cannot silently skip without an explicit policy entry.

The declared legitimate-skip policy has exactly two categories:
- MQTT integration skips when no broker is reachable on localhost:1883
- POSIX-only file-mode skips on a non-POSIX host

Note: The browser opt-in tests are deliberately NOT in this policy. CI sets
ICE_COLDER_BROWSER_TESTS=1 and installs Chrome + Node, so they MUST run in CI.
If they skip there, the build should fail — the guard ensures this.
"""

import re


# Legitimate skip reasons grouped by category, with reason patterns for matching.
# Each pattern is a compiled regex that must match the full skip reason string.
# Note: pytest prepends "Skipped: " to skip reasons in the terminalreporter.
LEGITIMATE_SKIP_REASONS = {
    "mqtt_broker_unavailable": re.compile(
        r"^Skipped: MQTT broker not available on localhost:1883$"
    ),
    "posix_file_modes_only": re.compile(r"^Skipped: POSIX file modes only$"),
}


def is_skip_legitimate(skip_reason: str) -> bool:
    """Check if a skip reason is in the legitimate skip policy.

    Args:
        skip_reason: The skip reason string to validate.

    Returns:
        True if the reason matches a legitimate skip pattern, False otherwise.
    """
    for pattern in LEGITIMATE_SKIP_REASONS.values():
        if pattern.fullmatch(skip_reason):
            return True
    return False


def validate_skips(skips: list[dict]) -> tuple[bool, list[str]]:
    """Validate a list of skip records against the policy.

    Args:
        skips: List of dicts with 'nodeid' and 'reason' keys.

    Returns:
        Tuple of (is_valid, error_messages).
        is_valid is True if all skips are legitimate.
        error_messages lists any illegitimate skip nodeids and reasons.
    """
    illegitimate = []
    for skip in skips:
        if not isinstance(skip, dict):
            # A malformed entry (not even a dict) must fail closed, not be
            # silently skipped over or crash the guard.
            illegitimate.append(f"<malformed skip entry: {skip!r}>")
            continue
        reason = skip.get("reason", "")
        if not is_skip_legitimate(reason):
            illegitimate.append(skip.get("nodeid", "<unknown>"))

    if illegitimate:
        return False, illegitimate

    return True, []


# Written by tests/conftest.py's pytest_sessionfinish hook in place of a real
# skip list when it cannot see pytest's own stats (terminalreporter missing,
# or missing .stats). Writing "[]" in that situation would be indistinguishable
# from a clean run with zero skips and let run_skip_guard.py report
# "Guard OK: All 0 skipped test(s) are legitimate" -- a false green with no
# investigation, which is exactly the defect class this guard exists to
# eliminate. The marker is a dict (never a valid skip-report shape, which is
# always a list) carrying an explicit key so it cannot be mistaken for one.
MISSING_TERMINALREPORTER_MARKER = {
    "__skip_guard_error__": "terminalreporter_unavailable",
    "detail": (
        "pytest_sessionfinish could not obtain the terminalreporter plugin "
        "(or it had no .stats attribute), so skip data was not collected "
        "this run. This is a build failure, not a report of zero skips."
    ),
}


def is_error_marker(report_data: object) -> bool:
    """True if *report_data* is the "could not collect skips" marker rather
    than a normal skip-report list (or anything else)."""
    return isinstance(report_data, dict) and "__skip_guard_error__" in report_data


def build_skip_report(stats: dict) -> list[dict]:
    """Build the skip-report list from pytest's terminalreporter.stats mapping.

    Pure and filesystem-free so it can be unit-tested directly against
    constructed stand-in report objects, with no dependency on the pytest
    session or its ordering -- `tests/conftest.py`'s `pytest_sessionfinish`
    hook is a thin adapter that calls this and writes the result to disk.

    Args:
        stats: pytest terminalreporter.stats mapping (category -> list of
            report objects). Only the "skipped" category is read; each
            report is expected to expose `.nodeid` and `.longrepr` the way
            pytest's own TestReport does, but a report missing either is
            tolerated (falls back to "<unknown>" / "").

    Returns:
        A list of {"nodeid": str, "reason": str} dicts, one per skipped test.
    """
    skips = []
    for report in stats.get("skipped", []):
        skip_reason = ""
        longrepr = getattr(report, "longrepr", None)
        if isinstance(longrepr, tuple) and len(longrepr) >= 3:
            skip_reason = longrepr[2]
        elif isinstance(longrepr, str):
            skip_reason = longrepr
        elif longrepr is not None:
            skip_reason = str(longrepr)

        skips.append(
            {
                "nodeid": getattr(report, "nodeid", "<unknown>"),
                "reason": skip_reason,
            }
        )
    return skips
