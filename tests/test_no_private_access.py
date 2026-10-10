"""Two guards for the VMC/Machine boundary (VMC public surface design,
section "Rule"; vmc-sale-fsm-core design).

**Guard 1: no private access from tests.** Tests and routes read a
collaborator off the VMC or Machine directly (`vmc.faults`, `vmc.escrow`,
`vmc.sale`, `machine.gate`, `machine.lease`, ...) but mutate state only
through a VMC/Machine method (`raise_fault`, `deposit_funds`,
`request_refund`, `machine.set_health_monitor`, ...). Reaching past a
method into a private attribute instead bypasses whatever side effect
that method would have run -- a fault alert never published, a lockout
never recorded, money moved without the FIFO ledger's bookkeeping -- so a
test using it proves nothing about the production path it claims to
exercise.

`test_no_private_vmc_access_outside_marked_exceptions` walks every
`tests/**/*.py` file except itself and fails, listing `path:line: <matched
text>` for each one, if any line matches `vmc._<name>` or `machine._<name>`
where `<name>` starts with a lowercase letter (so `vmc.__class__` /
`vmc.__dict__` -- dunder access used by a few monkeypatches -- are
allowed; the second underscore is not a lowercase letter).

Adding an exception: there should almost never be one. If a test genuinely
needs to reach a VMC/Machine private (e.g. to drive a defensive branch that
is unreachable through any public timer or event method), put a comment
line containing the literal marker `# private: <reason>` on one of the
three lines immediately above the `vmc._...`/`machine._...` line,
explaining why no public path reaches it. This test only checks for the
marker's presence nearby, not its content -- the reviewer reads the reason.

**Guard 2: the VMC's own import boundary.** `controller/vmc.py` is the
sale FSM alone (see `CLAUDE.md`'s "FSM Core" section): it holds no
transport or service handle, and every collaborator it needs is injected
by `controller/machine.py`'s `Machine`. `test_vmc_has_no_forbidden_imports`
parses `controller/vmc.py` with `ast` (so a `TYPE_CHECKING`-guarded import
is caught exactly like an ordinary one -- the VMC must not even *name*
these types) and fails if it imports `controller.machine`,
`controller.maintenance_lease`, `controller.test_sale`,
`services.mqtt_client`, `services.health_monitor`, `services.session_store`,
`services.event_recorder`, `services.command_dispatcher`, or
`services.display_controller`. One deliberate carve-out: `VMC.snapshot()`
constructs and returns a `services.session_store.SessionSnapshot` -- a
plain value object, not a transport/service handle -- so importing exactly
that one name from `services.session_store` is allowed; importing anything
else from it (`SessionStore` itself, say) is not.
`test_only_machine_imports_controller_machine` parses every other
`controller/*.py` module and fails if any of them import `controller.machine`
-- `Machine` is the composition root; nothing under `controller/` may build
one, or even name the module, besides `controller/machine.py` itself.
"""

import ast
import re
from pathlib import Path

PRIVATE_ACCESS_RE = re.compile(r"\b(?:vmc|machine)\._[a-z][a-zA-Z0-9_]*")
PRIVATE_MARKER = "# private:"
LOOKBACK_LINES = 3

TESTS_DIR = Path(__file__).resolve().parent
THIS_FILE = Path(__file__).resolve()
REPO_ROOT = TESTS_DIR.parent
CONTROLLER_DIR = REPO_ROOT / "controller"

#: Modules controller/vmc.py must never import -- transport/service
#: handles, and the composition root itself. See this module's docstring,
#: "Guard 2", for the one deliberate carve-out (SessionSnapshot).
FORBIDDEN_VMC_IMPORTS = {
    "controller.machine",
    "controller.maintenance_lease",
    "controller.test_sale",
    "services.mqtt_client",
    "services.health_monitor",
    "services.session_store",
    "services.event_recorder",
    "services.command_dispatcher",
    "services.display_controller",
}

#: Per forbidden module, the names `from <module> import <name>` may still
#: import -- a value object, never a service handle. Any module not listed
#: here is fully forbidden: no name may be imported from it at all.
ALLOWED_NAMES_FROM_FORBIDDEN_MODULE = {
    "services.session_store": {"SessionSnapshot"},
}


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


def _iter_imports(tree: ast.AST):
    """Yield `(module, names)` for every import in `tree`: `names` is
    `None` for a plain `import x` (nothing to narrow against an allowlist),
    or the list of imported names for a `from x import a, b` -- walking the
    whole tree (not just the top level) so a `TYPE_CHECKING`-guarded import
    is caught exactly like an ordinary one."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, None
        elif isinstance(node, ast.ImportFrom) and node.module:
            yield node.module, [alias.name for alias in node.names]


def _find_forbidden_imports(tree: ast.AST) -> list[str]:
    """Return a human-readable violation string for every forbidden import
    in `tree`, matched two ways: `module` itself in `FORBIDDEN_VMC_IMPORTS`
    (an ordinary `import x`/`from x import ...`), or -- mirroring how
    `test_only_machine_imports_controller_machine` catches `from controller
    import machine` below -- a `from <package> import <name>` whose
    `f"{package}.{name}"` names a forbidden module, which `ast` never
    surfaces as a single dotted `module` string (e.g. `from services
    import session_store`, which names forbidden `services.session_store`
    without ever setting `module == "services.session_store"`)."""
    violations = []
    for module, names in _iter_imports(tree):
        if module in FORBIDDEN_VMC_IMPORTS:
            allowed = ALLOWED_NAMES_FROM_FORBIDDEN_MODULE.get(module)
            if allowed is None:
                violations.append(module if names is None else f"{module} ({names})")
            elif names is None or not set(names) <= allowed:
                violations.append(f"{module} ({names})")
            continue
        if names:
            for name in names:
                dotted = f"{module}.{name}"
                if dotted in FORBIDDEN_VMC_IMPORTS:
                    violations.append(f"from {module} import {name}")
    return violations


def test_vmc_has_no_forbidden_imports():
    """`controller/vmc.py` holds no transport or service handle -- see
    this module's docstring, "Guard 2", for the forbidden list and the
    one `SessionSnapshot` carve-out."""
    path = CONTROLLER_DIR / "vmc.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    violations = _find_forbidden_imports(tree)

    assert not violations, (
        "controller/vmc.py imports a forbidden transport/service module -- "
        "every such collaborator must be injected by controller/machine.py's "
        'Machine instead. See this file\'s module docstring, "Guard 2", for '
        "the allowlist.\n" + "\n".join(violations)
    )


def test_forbidden_import_check_catches_package_submodule_form():
    """Negative self-test for `_find_forbidden_imports`: `from services
    import session_store` and `from controller import maintenance_lease`
    name forbidden modules via the `from <package> import <name>` form,
    not as a single dotted `module` string -- without the package-form
    check in `_find_forbidden_imports`, both slip past undetected."""
    tree = ast.parse(
        "from services import session_store\nfrom controller import maintenance_lease\n"
    )

    violations = _find_forbidden_imports(tree)

    assert violations == [
        "from services import session_store",
        "from controller import maintenance_lease",
    ]


def test_only_machine_imports_controller_machine():
    """`controller/machine.py`'s `Machine` is the composition root; no
    other module under `controller/` may build one, or even name the
    module."""
    violations = []
    for path in sorted(CONTROLLER_DIR.glob("*.py")):
        if path.name == "machine.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for module, names in _iter_imports(tree):
            if module == "controller.machine":
                violations.append(f"{path.name}: from controller.machine import ...")
            elif module == "controller" and names and "machine" in names:
                violations.append(f"{path.name}: from controller import machine")

    assert not violations, (
        "Only controller/machine.py may import controller.machine -- it is "
        "the composition root.\n" + "\n".join(violations)
    )
