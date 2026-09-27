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
        reason = skip.get("reason", "")
        if not is_skip_legitimate(reason):
            illegitimate.append(skip.get("nodeid", "<unknown>"))

    if illegitimate:
        return False, illegitimate

    return True, []
