"""Guard test for the VMC/Machine public surface (VMC public surface
design, section "Rule"; plan: vmc-public-surface, Task 5; vmc-reduction
plan, Task 6 extends the guard to `machine` too, now that
`controller/machine.py`'s `Machine` is the composition root and sole
construction path).

Tests and routes read a collaborator off the VMC or Machine directly
(`vmc.faults`, `vmc.escrow`, `vmc.sale`, `machine.gate`, `machine.lease`,
...) but mutate state only through a VMC/Machine method (`raise_fault`,
`deposit_funds`, `request_refund`, `machine.set_health_monitor`, ...).
Reaching past a method into a private attribute instead bypasses whatever
side effect that method would have run -- a fault alert never published, a
lockout never recorded, money moved without the FIFO ledger's bookkeeping
-- so a test using it proves nothing about the production path it claims
to exercise.

This test walks every `tests/**/*.py` file except itself and fails, listing
`path:line: <matched text>` for each one, if any line matches
`vmc._<name>` or `machine._<name>` where `<name>` starts with a lowercase
letter (so `vmc.__class__` / `vmc.__dict__` -- dunder access used by a few
monkeypatches -- are allowed; the second underscore is not a lowercase
letter).

Adding an exception: there should almost never be one. If a test genuinely
needs to reach a VMC/Machine private (e.g. to drive a defensive branch that
is unreachable through any public timer or event method), put a comment
line containing the literal marker `# private: <reason>` on one of the
three lines immediately above the `vmc._...`/`machine._...` line,
explaining why no public path reaches it. This test only checks for the
marker's presence nearby, not its content -- the reviewer reads the reason.
"""

import re
from pathlib import Path

PRIVATE_ACCESS_RE = re.compile(r"\b(?:vmc|machine)\._[a-z][a-zA-Z0-9_]*")
PRIVATE_MARKER = "# private:"
LOOKBACK_LINES = 3

TESTS_DIR = Path(__file__).resolve().parent
THIS_FILE = Path(__file__).resolve()


def _iter_test_files():
    for path in sorted(TESTS_DIR.rglob("*.py")):
        if path.resolve() == THIS_FILE:
            continue
        yield path


def _is_marked(lines: list[str], match_index: int) -> bool:
    """True if one of the up-to-`LOOKBACK_LINES` lines immediately above
    `lines[match_index]` contains the `# private:` marker."""
    start = max(0, match_index - LOOKBACK_LINES)
    return any(PRIVATE_MARKER in line for line in lines[start:match_index])


def test_no_private_vmc_access_outside_marked_exceptions():
    findings = []
    for path in _iter_test_files():
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        rel = path.relative_to(TESTS_DIR.parent)
        for i, line in enumerate(lines):
            for match in PRIVATE_ACCESS_RE.finditer(line):
                if _is_marked(lines, i):
                    continue
                findings.append(f"{rel}:{i + 1}: {match.group(0)}")

    assert not findings, (
        "Found vmc._<private>/machine._<private> access outside "
        "tests/test_no_private_access.py's marked exceptions -- read a "
        "public collaborator (vmc.faults, vmc.escrow, vmc.sale, "
        "machine.gate, machine.lease, ...) or drive state through a "
        "VMC/Machine method instead. See this file's module docstring for "
        "the `# private:` escape hatch.\n" + "\n".join(findings)
    )
