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
ice_edge.web.routes.health import _faults_with_age`): it reaches past
whatever that module considers its public surface.

This was originally a hardcoded check for `vmc._<name>`/`machine._<name>`
only. It is now a general ratchet over **every** `<object>._<name>` access
(except `self._x`/`cls._x`, which are a class's own fields, and dunder
access like `vmc.__class__`, used by a few monkeypatches) and every private
`from ... import _name`, across all of `tests/**/*.py`.

`count_private_access(path)` returns one `"path:line: text"` entry per
unmarked hit in a file (an `ast` attribute access or `getattr`-style string, see `_private_attr_hits`,
or a private name imported via an `ast.ImportFrom` node -- every `alias`
whose *imported* name starts with `_[a-z]`, regardless of any `asname`,
including names spread across a multi-line parenthesised import).
`current_budget()` sums those per file into `{rel_posix_path: count}`,
omitting files with zero hits.

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
   marker above the line and explain why. The marker must carry
   non-empty reason text (a bare `# private:` exempts nothing); the
   reviewer reads the reason.
5. `self._x`, `cls._x`, and dunder access (`vmc.__class__`) are never
   hits. A plain `import x as _y` is never a hit (it names a module, not
   an imported name). A `from <module> import <name>` is a hit whenever
   the *imported* name starts with `_[a-z]`, regardless of any `as`
   alias -- so `from m import _foo` and `from m import _foo as bar` are
   both hits, `from loguru import logger as _loguru` is not, and this
   holds across a multi-line parenthesised import exactly as it does for
   a single-line one.

The VMC import boundary that used to be Guard 2 here is enforced by
import-linter contracts 4-5 in the root `pyproject.toml`.

"""

import ast
import importlib.util
import re
import sys
from pathlib import Path

try:
    from tests.paths import TESTS_ROOT
except ModuleNotFoundError:  # run as `python tests/.../test_no_private_access.py`
    for _parent in Path(__file__).resolve().parents:
        if (_parent / "pyproject.toml").is_file():
            sys.path.insert(0, str(_parent))
            break
    from tests.paths import TESTS_ROOT

#: An attribute name that counts as private: a single leading underscore
#: followed by a lowercase letter (dunders such as `__class__` never match).
PRIVATE_NAME_RE = re.compile(r"^_[a-z]")

#: Builtins whose second argument names an attribute: `getattr(vmc, "_x")`.
ATTR_BUILTINS = frozenset({"getattr", "setattr", "hasattr", "delattr"})

#: A private imported name, matched against each `ast.ImportFrom` alias's
#: *imported* name (never its `asname`) -- so `from loguru import logger
#: as _loguru` is not a hit (the imported name is `logger`), but `from m
#: import _foo` and `from m import _foo as bar` both are. Mirrors
#: PRIVATE_NAME_RE's own leading-character rule so a dunder import
#: (`from m import __version__`) is not a hit either.
PRIVATE_IMPORT_NAME_RE = re.compile(r"^_[a-z][A-Za-z0-9_]*$")

#: `# private: <reason>` -- the reason must contain at least one
#: non-space character, so a bare marker exempts nothing.
PRIVATE_MARKER_RE = re.compile(r"# private:\s*\S")
LOOKBACK_LINES = 3

TESTS_DIR = TESTS_ROOT  # tests/ root, whichever subdirectory this file lives in
THIS_FILE = Path(__file__).resolve()
REPO_ROOT = TESTS_ROOT.parent

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
    `lines[match_index]` carries a `# private: <reason>` marker (a bare
    `# private:` with no reason text does not count)."""
    start = max(0, match_index - LOOKBACK_LINES)
    return any(PRIVATE_MARKER_RE.search(line) for line in lines[start:match_index])


def _private_attr_hits(tree: ast.AST) -> list[tuple[int, str]]:
    """`(lineno, text)` for every private attribute access in `tree`: an
    `ast.Attribute` whose `attr` matches `PRIVATE_NAME_RE` and whose value
    is not the bare Name `self`/`cls` (so call chains, subscripts and
    parenthesised expressions all count), plus `getattr`/`setattr`/
    `hasattr`/`delattr` calls whose second argument is a private-looking
    string constant. Comments and docstrings are never parsed as code, so
    they never count."""
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and PRIVATE_NAME_RE.match(node.attr):
            if isinstance(node.value, ast.Name) and node.value.id in ("self", "cls"):
                continue
            hits.append((node.end_lineno or node.lineno, ast.unparse(node)))
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in ATTR_BUILTINS
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
            and PRIVATE_NAME_RE.match(node.args[1].value)
        ):
            hits.append((node.lineno, ast.unparse(node)))
    return hits


def count_private_access(path: Path) -> list[str]:
    """Return one `"rel:line: text"` entry per unmarked private-access hit
    in `path`: a private attribute access found by `_private_attr_hits`, or a
    private name imported via an `ast.ImportFrom` alias (matched on the
    alias's *imported* name, never its `asname`, via
    `PRIVATE_IMPORT_NAME_RE` -- so this catches a multi-line parenthesised
    import exactly like a single-line one). A `# private: <reason>`
    marker on one of the three lines above a hit still exempts that line
    entirely, for both kinds of hit."""
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    try:
        rel = path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        # A sample file outside the repo (e.g. pytest's tmp_path, used by
        # this module's own sample-based tests) -- fall back to its own
        # path rather than raising.
        rel = path.as_posix()
    tree = ast.parse(text, filename=str(path))
    hits: list[tuple[int, str]] = [
        (lineno, snippet)
        for lineno, snippet in _private_attr_hits(tree)
        if not _is_marked(lines, lineno - 1)
    ]
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        module = ("." * node.level) + (node.module or "")
        for alias in node.names:
            if not PRIVATE_IMPORT_NAME_RE.match(alias.name):
                continue
            lineno = alias.lineno
            if _is_marked(lines, lineno - 1):
                continue
            hits.append((lineno, f"from {module} import {alias.name}"))

    hits.sort(key=lambda item: item[0])
    return [f"{rel}:{lineno}: {text}" for lineno, text in hits]


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


def _hits(tmp_path, source):
    tmp = tmp_path / "sample.py"
    tmp.write_text(source, encoding="utf-8")
    return count_private_access(tmp)


def test_ignores_self_cls_and_dunder(tmp_path):
    source = (
        "class A:\n"
        "    def f(self):\n"
        "        self._x = 1\n"
        "        return self._x\n"
        "    @classmethod\n"
        "    def g(cls):\n"
        "        return cls._y()\n"
        "vmc.__class__\n"
        "vmc.__dict__\n"
        "machine.__class__.__name__\n"
    )
    assert _hits(tmp_path, source) == []


def test_matches_object_private_attr(tmp_path):
    assert len(_hits(tmp_path, "sim._fault_state\n")) == 1


def test_getattr_family_with_private_string_counts(tmp_path):
    source = (
        'getattr(vmc, "_x")\n'
        'setattr(vmc, "_x", 1)\n'
        'hasattr(vmc, "_x")\n'
        'delattr(vmc, "_x")\n'
        'getattr(vmc, "public")\n'
        'getattr(vmc, "__class__")\n'
    )
    assert len(_hits(tmp_path, source)) == 4


def test_call_chain_subscript_and_parenthesised_access_count(tmp_path):
    source = "f()._x\nxs[0]._y\n(a or b)._z\n"
    assert len(_hits(tmp_path, source)) == 3


def test_multiline_chain_hit_is_reported_on_the_attribute_line(tmp_path):
    findings = _hits(tmp_path, "value = (\n    make()\n    ._x\n)\n")
    assert len(findings) == 1
    assert ":3:" in findings[0]


def test_comments_and_docstrings_are_not_counted(tmp_path):
    source = (
        '"""Mentions vmc._x in a docstring."""\n'
        "# a comment about sim._y\n"
        'text = "obj._z inside a string"\n'
    )
    assert _hits(tmp_path, source) == []


def test_bare_marker_does_not_exempt(tmp_path):
    source = "# private:\nsim._a\n# private:   \nsim._b\n"
    assert len(_hits(tmp_path, source)) == 2


def test_marker_with_reason_exempts_new_hit_kinds(tmp_path):
    source = (
        "# private: driving a defensive branch\n"
        'getattr(vmc, "_x")\n'
        "# private: same\n"
        "f()._y\n"
    )
    assert _hits(tmp_path, source) == []


def test_private_import_detection_via_ast(tmp_path):
    """`count_private_access`'s AST-based import scan: a multi-line
    parenthesised private import, a single-line private import, an
    aliased *module* import (`import logger as _loguru`, not a hit --
    it is `ast.Import`, never `ast.ImportFrom`), and a `from m import _x
    as y` import (a hit, keyed on the imported name `_x`, not the
    `asname` `y`)."""
    tmp = tmp_path / "import_sample.py"
    tmp.write_text(
        "sim._fault_state\n"
        "from ice_edge.web.routes.health import _fault_gate\n"
        "import loguru as _loguru\n"
        "from services.widgets import _hidden as visible\n"
        "from tests.test_vmc_flows import (\n"
        "    RecordingClient,\n"
        "    # private: driving a defensive branch, see brief\n"
        "    _profiles_tmp_base_dir,\n"
        "    make_machine2,\n"
        "    filler_one,\n"
        "    _another_private,\n"
        ")\n",
        encoding="utf-8",
    )
    findings = count_private_access(tmp)
    assert len(findings) == 4
    assert any("sim._fault_state" in f for f in findings)
    assert any("_fault_gate" in f for f in findings)
    assert not any("_loguru" in f for f in findings)
    assert any("_hidden" in f for f in findings)
    # `_profiles_tmp_base_dir` sits 1 line below its marker (within the
    # 3-line lookback) and is exempted; `_another_private` sits 4 lines
    # below the same marker (outside the lookback) and still counts --
    # proving the marker applies per alias line inside a multi-line
    # import, bounded the same way as an ordinary attribute hit, not to
    # the `from` statement as a whole.
    assert not any("_profiles_tmp_base_dir" in f for f in findings)
    assert any("_another_private" in f for f in findings)
    assert not any("RecordingClient" in f for f in findings)
    assert not any("make_machine2" in f for f in findings)
    assert not any("filler_one" in f for f in findings)


def test_marker_exempts_line_within_three_line_lookback(tmp_path):
    """A `# private:` marker exactly `LOOKBACK_LINES` (3) lines above a hit
    -- two blank lines between the marker and the hit -- still exempts it."""
    tmp = tmp_path / "marker_sample.py"
    tmp.write_text(
        "sim._fault_state\n"
        "# private: needed for defensive branch\n"
        "\n"
        "\n"
        "sim._other_private\n",
        encoding="utf-8",
    )
    findings = count_private_access(tmp)
    assert len(findings) == 1
    assert "sim._fault_state" in findings[0]
    assert not any("sim._other_private" in f for f in findings)


def test_marker_does_not_exempt_line_outside_three_line_lookback(tmp_path):
    """A marker 4 lines above a hit -- one line past LOOKBACK_LINES -- no
    longer exempts it. Proves the lookback window is bounded, not merely
    `>= 1`."""
    tmp = tmp_path / "marker_sample.py"
    tmp.write_text(
        "sim._fault_state\n"
        "# private: needed for defensive branch\n"
        "\n"
        "\n"
        "\n"
        "sim._other_private\n",
        encoding="utf-8",
    )
    findings = count_private_access(tmp)
    assert len(findings) == 2
    assert any("sim._fault_state" in f for f in findings)
    assert any("sim._other_private" in f for f in findings)


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


if __name__ == "__main__":
    print(current_budget())
