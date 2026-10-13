"""Apply one phase of the reorganisation move map.

    uv run python -m scripts.refactor.codemod --phase N [--only-prefix P] [--dry-run]

Steps, in order: validate, ``git mv`` everything selected, rewrite Python
imports/strings with libcst, rewrite non-Python text, print a summary plus a
review list. The pure functions (``rewrite_source``, ``rewrite_text``,
``select_python_files``, ``plan_moves``, ...) need neither git nor a checkout.

Tooling only: nothing in the application may import this module or libcst.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

import libcst as cst
from libcst.helpers import get_full_name_for_node
from libcst.metadata import MetadataWrapper, PositionProvider

from scripts._repo import repo_root
from scripts.refactor.move_map import (
    FILE_MOVES,
    MODULE_MOVES,
    PACKAGE_ROOTS,
    TEST_MOVES,
    TEXT_REWRITE_FILES,
    FileMove,
    ModuleMove,
    TestMove,
    module_file,
    old_module_file,
)

# Rows exempt from *string* and *text* rewriting. "main" appears everywhere as
# hx-target="main"; its row is effectively a pure `git mv`.
STRING_EXEMPT = frozenset({"main"})

# Directory prefixes (and one file name) the Python pass never touches. The
# refactor package and its test hold the move map itself as string literals.
EXCLUDED_PREFIXES = ("docs/", ".venv/", "colder-docker/", "tmp/", "scripts/refactor/")
EXCLUDED_BASENAME_IN_TESTS = "test_codemod.py"

# A package-row match whose tail is one of these is a file name, not a module
# ("contracts.json").
FILE_SUFFIXES = frozenset(
    "json toml yaml yml css js html htm txt md log db bak example py pyc cfg ini csv sql lock".split()
)

_DOTTED = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*")
_DOTTED_IN_TEXT = re.compile(r"(?<![\w./\-])[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+")
_STATIC_PREFIX = re.compile(r"[A-Za-z_][\w.]*")
_REVIEW = re.compile(
    r"\b(controller|services|web_interface|simulators|hardware|contracts)[./]|\bmain\.py\b"
)


class Hit(NamedTuple):
    move: ModuleMove
    rest: str


@dataclass
class RewriteResult:
    source: str
    review: list[str] = field(default_factory=list)
    imports: int = 0
    strings: int = 0


# --------------------------------------------------------------------- matching


def _index(moves: Sequence[ModuleMove]) -> dict[str, ModuleMove]:
    return {m.old: m for m in moves}


def match_move(
    dotted: str, index: dict[str, ModuleMove], *, strings: bool = False
) -> Hit | None:
    """Longest-prefix match of ``dotted`` against the move keys."""
    parts = dotted.split(".")
    for n in range(len(parts), 0, -1):
        key = ".".join(parts[:n])
        move = index.get(key)
        if move is None:
            continue
        if strings and key in STRING_EXEMPT:
            return None
        return Hit(move, ".".join(parts[n:]))
    return None


def resolve(hit: Hit) -> str:
    return hit.move.new + ("." + hit.rest if hit.rest else "")


def _guard(hit: Hit) -> str:
    """Guard shared by string and text mode against non-module look-alikes.

    Returns "ok", "bare" (a bare package name: silently left alone) or
    "suffix" (left alone but reported). The suffix guard fires only when the
    tail after a package-row key is exactly one component that looks like a
    file suffix ("contracts.json"); a deeper tail such as
    "web_interface.routes.tests_level.log" is a real module and is rewritten.
    """
    if hit.move.package:
        if not hit.rest:
            return "bare"
        if "." not in hit.rest and hit.rest.lower() in FILE_SUFFIXES:
            return "suffix"
    return "ok"


# ------------------------------------------------------------- Python rewriting


class _Rewriter(cst.CSTTransformer):
    METADATA_DEPENDENCIES = (PositionProvider,)

    def __init__(self, moves: Sequence[ModuleMove]) -> None:
        super().__init__()
        self.index = _index(moves)
        self.result = RewriteResult("")

    def _line(self, node: cst.CSTNode) -> int:
        return self.get_metadata(PositionProvider, node).start.line

    def _note(self, node: cst.CSTNode, msg: str) -> None:
        self.result.review.append(f"{self._line(node)}: {msg}")

    # import a.b [as x]
    def leave_Import(
        self, original_node: cst.Import, updated_node: cst.Import
    ) -> cst.Import:
        names: list[cst.ImportAlias] = []
        changed = False
        for alias in updated_node.names:
            dotted = get_full_name_for_node(alias.name)
            hit = match_move(dotted, self.index) if dotted else None
            if hit is None or dotted is None:
                names.append(alias)
                continue
            new_node = cst.parse_expression(resolve(hit))
            if alias.asname is not None:
                alias = alias.with_changes(name=new_node)
            elif "." not in dotted:
                alias = alias.with_changes(
                    name=new_node, asname=cst.AsName(name=cst.Name(dotted))
                )
            else:
                alias = alias.with_changes(name=new_node)
                self._note(
                    original_node,
                    f"unaliased dotted `import {dotted}` -> {resolve(hit)}: "
                    f"check `{dotted}.attr` references in the body",
                )
            changed = True
            names.append(alias)
        if not changed:
            return updated_node
        self.result.imports += 1
        return updated_node.with_changes(names=names)

    # from a.b import c
    def leave_ImportFrom(
        self, original_node: cst.ImportFrom, updated_node: cst.ImportFrom
    ) -> cst.BaseSmallStatement | cst.FlattenSentinel[cst.BaseSmallStatement]:
        if updated_node.relative:
            self._note(original_node, "relative import: check by hand")
            return updated_node
        if updated_node.module is None:
            return updated_node
        modname = get_full_name_for_node(updated_node.module)
        if modname is None:
            return updated_node
        kmod = match_move(modname, self.index)
        names = updated_node.names
        if isinstance(names, cst.ImportStar):
            if kmod is None:
                return updated_node
            self.result.imports += 1
            return updated_node.with_changes(module=cst.parse_expression(resolve(kmod)))

        stay: list[cst.ImportAlias] = []
        groups: dict[str, list[cst.ImportAlias]] = {}
        for alias in names:
            name = get_full_name_for_node(alias.name)
            sub = match_move(f"{modname}.{name}", self.index) if name else None
            is_submodule = sub is not None and (
                kmod is None or sub.move is not kmod.move
            )
            if not is_submodule or sub is None or name is None:
                stay.append(alias)
                continue
            parent, _, base = resolve(sub).rpartition(".")
            asname = alias.asname
            if asname is None and base != name:
                asname = cst.AsName(name=cst.Name(name))
            groups.setdefault(parent, []).append(
                alias.with_changes(name=cst.Name(base), asname=asname)
            )
        if kmod is None and not groups:
            return updated_node

        parts: list[tuple[cst.BaseExpression, list[cst.ImportAlias]]] = []
        if stay:
            module = (
                cst.parse_expression(resolve(kmod)) if kmod else updated_node.module
            )
            parts.append((module, stay))
        for parent, aliases in groups.items():
            parts.append((cst.parse_expression(parent), aliases))

        self.result.imports += 1
        if len(parts) == 1:
            module, aliases = parts[0]
            return updated_node.with_changes(module=module, names=aliases)
        self._note(original_node, "import statement split across new modules")
        statements: list[cst.BaseSmallStatement] = []
        for module, aliases in parts:
            fixed = [
                a.with_changes(
                    comma=cst.Comma(whitespace_after=cst.SimpleWhitespace(" "))
                    if i < len(aliases) - 1
                    else cst.MaybeSentinel.DEFAULT
                )
                for i, a in enumerate(aliases)
            ]
            statements.append(
                cst.ImportFrom(module=module, names=fixed)  # type: ignore[arg-type]
            )
        return cst.FlattenSentinel(statements)

    def leave_SimpleString(
        self, original_node: cst.SimpleString, updated_node: cst.SimpleString
    ) -> cst.SimpleString:
        if "b" in updated_node.prefix.lower():
            return updated_node
        try:
            value = updated_node.evaluated_value
        except Exception:
            return updated_node
        if not isinstance(value, str) or not _DOTTED.fullmatch(value):
            return updated_node
        hit = match_move(value, self.index, strings=True)
        if hit is None:
            return updated_node
        guard = _guard(hit)
        if guard == "suffix":
            self._note(
                original_node,
                f'string "{value}" looks like a file name, not a module: not rewritten',
            )
        if guard != "ok":
            return updated_node
        self.result.strings += 1
        return updated_node.with_changes(
            value=updated_node.value.replace(value, resolve(hit), 1)
        )

    def leave_FormattedString(
        self, original_node: cst.FormattedString, updated_node: cst.FormattedString
    ) -> cst.FormattedString:
        static = ""
        for part in updated_node.parts:
            if not isinstance(part, cst.FormattedStringText):
                break
            static += part.value
        if not static or not _STATIC_PREFIX.fullmatch(static):
            return updated_node
        head = static.rstrip(".")
        hit = match_move(head, self.index, strings=True) if head else None
        partial = static.endswith(".") and any(
            k.startswith(static) for k in self.index if k not in STRING_EXEMPT
        )
        if hit is not None or partial:
            self._note(
                original_node,
                f'f-string with static prefix "{static}" names a moved module: '
                "never rewritten, check by hand",
            )
        return updated_node


def rewrite_source_stats(src: str, moves: Sequence[ModuleMove]) -> RewriteResult:
    try:
        wrapper = MetadataWrapper(cst.parse_module(src))
    except cst.ParserSyntaxError as exc:
        return RewriteResult(src, [f"0: could not parse ({exc.raw_line}): skipped"])
    rewriter = _Rewriter(moves)
    new_module = wrapper.visit(rewriter)
    rewriter.result.source = new_module.code
    return rewriter.result


def rewrite_source(src: str, moves: Sequence[ModuleMove]) -> tuple[str, list[str]]:
    """Rewrite imports and simple string literals; returns (new source, review items)."""
    result = rewrite_source_stats(src, moves)
    return result.source, result.review


def select_python_files(tracked: Sequence[str]) -> list[str]:
    """The tracked ``*.py`` files the rewrite pass may touch."""
    selected = []
    for path in tracked:
        if not path.endswith(".py"):
            continue
        if path.startswith(EXCLUDED_PREFIXES):
            continue
        if (
            path.startswith("tests/")
            and path.rsplit("/", 1)[-1] == EXCLUDED_BASENAME_IN_TESTS
        ):
            continue
        selected.append(path)
    return selected


def rewrite_python_file(
    path: str, src: str, moves: Sequence[ModuleMove]
) -> tuple[str, list[str]]:
    """``rewrite_source`` gated by the CLI's file selection (excluded files come back as-is)."""
    if not select_python_files([path]):
        return src, []
    return rewrite_source(src, moves)


# ------------------------------------------------------------------ text mode


def _path_pairs(
    moves: Sequence[ModuleMove],
    file_moves: Sequence[FileMove],
    test_moves: Sequence[TestMove],
) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for m in moves:
        if m.old in STRING_EXEMPT:
            continue  # `main.py` is reported for review instead
        old_file = old_module_file(m.old, package=m.package).as_posix()
        new_file = module_file(m.new, package=m.package).as_posix()
        pairs.append((old_file, new_file))
        if m.package:
            nested = any(o.old.startswith(m.old + ".") for o in moves) or any(
                f.old.startswith(m.old.replace(".", "/") + "/") for f in file_moves
            )
            if not nested:
                old_dir = m.old.replace(".", "/") + "/"
                new_dir = module_file(m.new, package=True).parent.as_posix() + "/"
                pairs.append((old_dir, new_dir))
    for f in file_moves:
        pairs.append((f.old, f.new))
    for t in test_moves:
        dest = t.dest_dir if t.dest_dir.endswith("/") else t.dest_dir + "/"
        pairs.append((f"tests/{t.file}", dest + t.file))
    pairs.sort(key=lambda p: len(p[0]), reverse=True)
    return pairs


def rewrite_text(
    text: str,
    moves: Sequence[ModuleMove],
    file_moves: Sequence[FileMove],
    test_moves: Sequence[TestMove],
) -> tuple[str, list[str]]:
    """Rewrite dotted refs, slash paths and test paths; list what still needs a human."""
    index = _index(moves)

    blocked: list[str] = []

    def dotted(m: re.Match[str]) -> str:
        hit = match_move(m.group(0), index, strings=True)
        if hit is None:
            return m.group(0)
        guard = _guard(hit)
        if guard == "suffix":
            blocked.append(m.group(0))
        if guard != "ok":
            return m.group(0)
        return resolve(hit)

    text = _DOTTED_IN_TEXT.sub(dotted, text)

    pairs = _path_pairs(moves, file_moves, test_moves)
    if pairs:
        lookup = dict(pairs)
        alternatives = [
            re.escape(old) + ("" if old.endswith("/") else r"(?![\w])")
            for old, _ in pairs
        ]
        path_re = re.compile(r"(?<![\w\-./])(?:" + "|".join(alternatives) + ")")
        # The lookahead is not part of the match, so group(0) is exactly `old`.
        text = path_re.sub(lambda m: lookup[m.group(0)], text)

    review = [
        f"{n}: {line.strip()}"
        for n, line in enumerate(text.splitlines(), start=1)
        if _REVIEW.search(line) or any(tok in line for tok in blocked)
    ]
    return text, review


# -------------------------------------------------------------------- planning


def _selected(new: str, dest_path: str, only_prefix: str | None) -> bool:
    return (
        only_prefix is None
        or new.startswith(only_prefix)
        or dest_path.startswith(only_prefix)
    )


def plan_moves(
    phase: int,
    modules: Sequence[ModuleMove],
    files: Sequence[FileMove],
    tests: Sequence[TestMove],
    tracked: Sequence[str],
    only_prefix: str | None = None,
) -> list[tuple[str, str]]:
    """Every ``git mv`` this phase needs, as (src, dst) repo-relative posix paths.

    Single-module, single-file and test rows are always listed (so an applied
    row can be recognised and skipped). Package rows and tree rows expand over
    ``tracked``; files claimed by a more specific row stay with that row.
    """
    index = _index(modules)
    plan: list[tuple[str, str]] = []

    for m in modules:
        if m.phase != phase:
            continue
        dst_file = module_file(m.new, package=m.package).as_posix()
        if not _selected(m.new, dst_file, only_prefix):
            continue
        if not m.package:
            plan.append((old_module_file(m.old, package=False).as_posix(), dst_file))
            continue
        old_dir = m.old.replace(".", "/")
        new_dir = module_file(m.new, package=True).parent.as_posix()
        for path in tracked:
            if not path.startswith(old_dir + "/"):
                continue
            if any(path == f.old.rstrip("/") or path.startswith(f.old) for f in files):
                continue
            if path.endswith(".py"):
                dotted = path[:-3].replace("/", ".")
                if dotted.endswith(".__init__"):
                    dotted = dotted[: -len(".__init__")]
                owner = match_move(dotted, index)
                if owner is None or owner.move is not m:
                    continue
            plan.append((path, new_dir + path[len(old_dir) :]))

    for f in files:
        if f.phase != phase or not _selected(f.new, f.new, only_prefix):
            continue
        if f.old.endswith("/"):
            for path in tracked:
                if path.startswith(f.old):
                    plan.append((path, f.new + path[len(f.old) :]))
        else:
            plan.append((f.old, f.new))

    for t in tests:
        if t.phase != phase:
            continue
        dest = t.dest_dir if t.dest_dir.endswith("/") else t.dest_dir + "/"
        if not _selected(dest, dest, only_prefix):
            continue
        plan.append((f"tests/{t.file}", dest + t.file))
    return plan


def validate_plan(
    plan: Sequence[tuple[str, str]], exists: Callable[[str], bool]
) -> tuple[list[tuple[str, str]], list[str]]:
    """Split a plan into moves still to do and errors; applied rows are skipped."""
    todo: list[tuple[str, str]] = []
    errors: list[str] = []
    for src, dst in plan:
        src_ok, dst_ok = exists(src), exists(dst)
        if src_ok and not dst_ok:
            todo.append((src, dst))
        elif not src_ok and dst_ok:
            continue  # already applied
        elif src_ok and dst_ok:
            errors.append(f"both source and destination exist: {src} -> {dst}")
        else:
            errors.append(f"source missing and destination missing: {src} -> {dst}")
    seen: dict[str, str] = {}
    for src, dst in todo:
        if dst in seen:
            errors.append(f"two sources for one destination {dst}: {seen[dst]}, {src}")
        seen[dst] = src
    return todo, errors


def check_package_roots(
    moves: Sequence[ModuleMove], dir_exists: Callable[[str], bool]
) -> list[str]:
    errors = []
    for root in sorted({m.new.split(".")[0] for m in moves}):
        path = PACKAGE_ROOTS[root]
        if not dir_exists(path):
            errors.append(f"package root {path} missing")
    return errors


def uncovered_tests(existing: Sequence[str], tests: Sequence[TestMove]) -> list[str]:
    """``tests/test_*.py`` files that no TEST_MOVES row mentions (or mentions twice)."""
    listed = [t.file for t in tests]
    bad = []
    for path in existing:
        name = path.rsplit("/", 1)[-1]
        if (
            path.startswith("tests/")
            and path.count("/") == 1
            and name.startswith("test_")
            and name.endswith(".py")
        ):
            if listed.count(name) != 1:
                bad.append(path)
    return bad


# ------------------------------------------------------------------------- CLI


def _git(root: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "core.quotepath=off", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return done.stdout


def _tracked(root: Path, pattern: str | None = None) -> list[str]:
    args = ["ls-files", "-z"]
    if pattern:
        args += ["--", pattern]
    return [p for p in _git(root, *args).split("\0") if p]


def _ancestor_inits(dst: str) -> list[str]:
    """``__init__.py`` files a moved .py destination needs, from its root downward."""
    if not dst.endswith(".py"):
        return []
    top: str | None = None
    for base in PACKAGE_ROOTS.values():
        if dst.startswith(base + "/"):
            top = base
            break
    if top is None and dst.startswith("tests/"):
        top = "tests"
    if top is None:
        return []
    inits = [f"{top}/__init__.py"]
    parts = dst[len(top) + 1 :].split("/")[:-1]
    current = top
    for part in parts:
        current = f"{current}/{part}"
        inits.append(f"{current}/__init__.py")
    return inits


def _expand_text_files(root: Path) -> list[str]:
    found: list[str] = []
    for pattern in TEXT_REWRITE_FILES:
        for path in sorted(root.glob(pattern)):
            if path.is_file():
                found.append(path.relative_to(root).as_posix())
    return found


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Apply one phase of the reorg move map."
    )
    parser.add_argument("--phase", type=int, required=True)
    parser.add_argument(
        "--only-prefix",
        help="restrict to rows whose new module / destination path starts with this",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    root = repo_root()
    tracked = _tracked(root)
    phase, prefix = args.phase, args.only_prefix

    sel_modules = [
        m
        for m in MODULE_MOVES
        if m.phase == phase
        and _selected(m.new, module_file(m.new, package=m.package).as_posix(), prefix)
    ]
    sel_files = [
        f for f in FILE_MOVES if f.phase == phase and _selected(f.new, f.new, prefix)
    ]
    sel_tests = [
        t
        for t in TEST_MOVES
        if t.phase == phase and _selected(t.dest_dir, t.dest_dir, prefix)
    ]
    if not (sel_modules or sel_files or sel_tests):
        print(
            f"error: no rows selected for phase {phase} prefix {prefix!r}",
            file=sys.stderr,
        )
        return 1

    errors = check_package_roots(sel_modules, lambda p: (root / p).is_dir())
    if sel_tests:
        errors += [
            f"tests/test_*.py not covered exactly once by TEST_MOVES: {p}"
            for p in uncovered_tests(tracked, TEST_MOVES)
        ]
    plan = plan_moves(phase, MODULE_MOVES, FILE_MOVES, TEST_MOVES, tracked, prefix)
    todo, plan_errors = validate_plan(plan, lambda p: (root / p).exists())
    errors += plan_errors
    if errors:
        for e in errors:
            print(f"error: {e}", file=sys.stderr)
        return 1

    mode = "DRY RUN " if args.dry_run else ""
    print(
        f"{mode}phase {phase}: {len(todo)} to move, {len(plan) - len(todo)} already applied"
    )
    if args.dry_run:
        for src, dst in todo:
            print(f"  git mv {src} {dst}")
    else:
        for src, dst in todo:
            (root / dst).parent.mkdir(parents=True, exist_ok=True)
            _git(root, "mv", src, dst)
        for init in sorted({i for _, dst in todo for i in _ancestor_inits(dst)}):
            if not (root / init).exists():
                (root / init).parent.mkdir(parents=True, exist_ok=True)
                (root / init).write_bytes(b"")
                _git(root, "add", "--", init)

    review: list[str] = []
    n_imports = n_strings = n_py = 0
    py_files = select_python_files(_tracked(root, "*.py"))
    for rel in py_files:
        path = root / rel
        if not path.is_file():
            continue
        original = path.read_bytes().decode("utf-8")
        result = rewrite_source_stats(original, sel_modules)
        review += [f"{rel}:{item}" for item in result.review]
        if result.source != original:
            n_py += 1
            n_imports += result.imports
            n_strings += result.strings
            if not args.dry_run:
                path.write_bytes(result.source.encode("utf-8"))

    n_text = 0
    for rel in _expand_text_files(root):
        path = root / rel
        original = path.read_bytes().decode("utf-8")
        new_text, items = rewrite_text(original, sel_modules, sel_files, sel_tests)
        review += [f"{rel}:{item}" for item in items]
        if new_text != original:
            n_text += 1
            if not args.dry_run:
                path.write_bytes(new_text.encode("utf-8"))

    print(
        f"{mode}summary: moved {len(todo)} file(s); {n_imports} import statement(s) and "
        f"{n_strings} string(s) rewritten across {n_py} python file(s); "
        f"{n_text} text file(s) rewritten"
    )
    print(f"review list ({len(review)}):")
    for item in review:
        print(f"  {item}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
