"""Two guards for the VMC/Machine boundary (VMC public surface design,
section "Rule"; vmc-sale-fsm-core design).

**Guard 1: a per-file ratchet on private access from tests.** Tests and
routes read a collaborator off an object directly (`vmc.faults`,
`vmc.escrow`, `vmc.sale`, `machine.gate`, `machine.lease`, `sim.fault_defs`,
...) but mutate state only through a public method. Reaching past a method
into a private attribute instead bypasses whatever side effect that method
would have run -- a fault alert never published, a lockout never recorded,
money moved without the FIFO ledger's bookkeeping -- so a test using it
proves nothing about the production path it claims to exercise. The same
goes for importing a private name directly out of another module (`from
web_interface.routes.health import _fault_gate`): it reaches past whatever
that module considers its public surface.

This was originally a hardcoded check for `vmc._<name>`/`machine._<name>`
only. It is now a general ratchet over **every** `<object>._<name>` access
(except `self._x`/`cls._x`, which are a class's own fields, and dunder
access like `vmc.__class__`, used by a few monkeypatches) and every private
`from ... import _name`, across all of `tests/**/*.py`.

`count_private_access(path)` returns one `"path:line: text"` entry per
unmarked hit in a file (an attribute-access match from `PRIVATE_ATTR_RE`,
or a flagged name from a `from ... import ...` line matched by
`PRIVATE_IMPORT_RE`). `current_budget()` sums those per file into
`{rel_posix_path: count}`, omitting files with zero hits.

`test_budget_is_exact` checks the *current* counts against the committed
`tests/private_access_budget.py::BUDGET`:

1. A file not listed in `BUDGET` has an implicit budget of 0 -- there is no
   special case for `vmc`/`machine` any more; they simply have zero hits
   and so never appear in `BUDGET`.
2. A file whose current count **exceeds** its budget fails the test,
   listing every `path:line: text` hit so the reviewer can see exactly
   what to fix.
3. A file whose current count is **below** its budget also fails, with
   "lower BUDGET[...] to N" -- budgets only ever move down. **To lower
   one:** fix the tests in that file until its count drops (promote a
   private name to a public seam, or stop reaching past one), run `uv run
   python tests/test_no_private_access.py` to get the new count, then edit
   that one number in `tests/private_access_budget.py`. A brand-new file
   starts at budget 0 and must stay there until it earns a nonzero number
   the same way the first time it needs one.
4. A `# private: <reason>` marker on one of the three lines immediately
   above a hit still exempts that line from being counted at all -- the
   mechanism is unchanged from the original guard. This is the escape
   hatch for a test that genuinely needs to reach a private (e.g. to drive
   a defensive branch unreachable through any public seam): put the
   marker above the line and explain why. This test only checks for the
   marker's presence, not its content -- the reviewer reads the reason.
5. `self._x`, `cls._x`, and dunder access (`vmc.__class__`) are never
   hits. An aliased import (`import logger as _loguru`) is not a hit;
   `from m import _foo` is.

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
import importlib.util
import re
from pathlib import Path

#: Any `<object>._name` access, except `self._x`/`cls._x` (a class's own
#: fields) and dunder access (`vmc.__class__` -- the second underscore is
#: not a lowercase letter so it never matches).
PRIVATE_ATTR_RE = re.compile(
    r"\b(?!self\b|cls\b)[A-Za-z_][A-Za-z0-9_]*\._[a-z][A-Za-z0-9_]*"
)

#: A `from <module> import <names>` line. Matching this alone does not
#: make a hit -- count_private_access still splits `<names>` on ',', takes
#: the part before ' as ', and only counts a name that starts with '_'
#: (so `from loguru import logger as _loguru` is not a hit, but `from m
#: import _foo` is).
PRIVATE_IMPORT_RE = re.compile(r"^\s*from\s+\S+\s+import\s+(.+)$")

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


#: Data file, not a test; it has nothing to count and importing it as a
#: module from inside the walk would be pointless.
BUDGET_FILE = TESTS_DIR / "private_access_budget.py"


def _load_budget() -> dict[str, int]:
    """Load `BUDGET` from `BUDGET_FILE` by path rather than `import
    tests.private_access_budget`, so this module works identically run
    under pytest (rootdir-based import) or directly as a script (`uv run
    python tests/test_no_private_access.py`, where the repo root is not
    on `sys.path`)."""
    spec = importlib.util.spec_from_file_location("private_access_budget", BUDGET_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.BUDGET


BUDGET = _load_budget()


def _iter_test_files():
    for path in sorted(TESTS_DIR.rglob("*.py")):
        if path.resolve() == THIS_FILE:
            continue
        if path.resolve() == BUDGET_FILE.resolve():
            continue
        yield path


def _is_marked(lines: list[str], match_index: int) -> bool:
    """True if one of the up-to-`LOOKBACK_LINES` lines immediately above
    `lines[match_index]` contains the `# private:` marker."""
    start = max(0, match_index - LOOKBACK_LINES)
    return any(PRIVATE_MARKER in line for line in lines[start:match_index])


def count_private_access(path: Path) -> list[str]:
    """Return one `"rel:line: text"` entry per unmarked private-access hit
    in `path`: an attribute access matched by `PRIVATE_ATTR_RE`, or a
    flagged name (starts with `_` once any ` as alias` is stripped) out of
    a `from ... import ...` line matched by `PRIVATE_IMPORT_RE`. A `#
    private: <reason>` marker on one of the three lines above a hit still
    exempts that line entirely, for both kinds of hit."""
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    try:
        rel = path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        # A sample file outside the repo (e.g. pytest's tmp_path, used by
        # this module's own sample-based tests) -- fall back to its own
        # path rather than raising.
        rel = path.as_posix()
    findings = []
    for i, line in enumerate(lines):
        if _is_marked(lines, i):
            continue
        for match in PRIVATE_ATTR_RE.finditer(line):
            findings.append(f"{rel}:{i + 1}: {match.group(0)}")
        import_match = PRIVATE_IMPORT_RE.match(line)
        if import_match:
            for name in import_match.group(1).split(","):
                base = name.strip().split(" as ")[0].strip()
                if base.startswith("_"):
                    findings.append(f"{rel}:{i + 1}: {line.strip()}")
    return findings


def current_budget() -> dict[str, int]:
    """`{rel_posix_path: unmarked hit count}` across every file
    `_iter_test_files` walks, omitting any file with zero hits."""
    budget: dict[str, int] = {}
    for path in _iter_test_files():
        count = len(count_private_access(path))
        if count:
            rel = path.resolve().relative_to(REPO_ROOT).as_posix()
            budget[rel] = count
    return dict(sorted(budget.items()))


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


def test_regex_ignores_self_cls_and_dunder():
    samples = [
        "self._x = 1",
        "cls._y()",
        "vmc.__class__",
        "vmc.__dict__",
        "machine.__class__.__name__",
    ]
    for sample in samples:
        assert not PRIVATE_ATTR_RE.search(sample), sample


def test_regex_matches_object_private_and_private_import(tmp_path):
    assert PRIVATE_ATTR_RE.search("sim._fault_state")
    assert PRIVATE_IMPORT_RE.search(
        "from web_interface.routes.health import _fault_gate"
    )

    tmp = tmp_path / "import_sample.py"
    tmp.write_text(
        "sim._fault_state\n"
        "from web_interface.routes.health import _fault_gate\n"
        "from loguru import logger as _loguru\n",
        encoding="utf-8",
    )
    findings = count_private_access(tmp)
    assert len(findings) == 2
    assert any("sim._fault_state" in f for f in findings)
    assert any("_fault_gate" in f for f in findings)
    assert not any("_loguru" in f for f in findings)


def test_marker_exempts_line(tmp_path):
    tmp = tmp_path / "marker_sample.py"
    tmp.write_text(
        "sim._fault_state\n"
        "# private: needed for defensive branch\n"
        "sim._other_private\n",
        encoding="utf-8",
    )
    findings = count_private_access(tmp)
    assert len(findings) == 1
    assert "sim._fault_state" in findings[0]
    assert not any("sim._other_private" in f for f in findings)


def test_budget_is_exact():
    current = current_budget()
    over = []
    under = []
    for rel, budgeted in sorted(BUDGET.items()):
        count = current.get(rel, 0)
        if count > budgeted:
            path = REPO_ROOT / rel
            findings = count_private_access(path)
            over.append(
                f"{rel}: {count} > budget {budgeted}\n  " + "\n  ".join(findings)
            )
        elif count < budgeted:
            under.append(f"lower BUDGET[{rel!r}] to {count}")
    for rel, count in sorted(current.items()):
        if rel not in BUDGET and count > 0:
            path = REPO_ROOT / rel
            findings = count_private_access(path)
            over.append(
                f"{rel}: {count} > budget 0 (not in BUDGET)\n  " + "\n  ".join(findings)
            )

    assert not over, "Private-access budget exceeded:\n" + "\n".join(over)
    assert not under, "Private-access budget can ratchet down:\n" + "\n".join(under)


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


if __name__ == "__main__":
    print(current_budget())
