"""Prove each workspace member imports using only its own declared dependencies.

For every package under ``packages/``, run ``uv run --isolated --no-dev
--package <name>`` (an environment holding only that member and what it
declares) and import every module found under its ``src/`` tree. Exits
non-zero naming the first failure.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGES = REPO_ROOT / "packages"


def _modules(src: Path) -> list[str]:
    names: list[str] = []
    for path in sorted(src.rglob("*.py")):
        rel = path.relative_to(src).with_suffix("")
        parts = list(rel.parts)
        if parts[-1] == "__init__":
            parts.pop()
        elif parts[-1] == "__main__":
            continue  # running it is not importing it; the package is covered
        if parts:
            names.append(".".join(parts))
    return names


def main() -> int:
    for pkg_dir in sorted(p for p in PACKAGES.iterdir() if p.is_dir()):
        src = pkg_dir / "src"
        modules = _modules(src) if src.is_dir() else []
        if not modules:
            continue
        project = f"ice-{pkg_dir.name}"
        code = "import importlib; [importlib.import_module(m) for m in %r]" % (modules,)
        result = subprocess.run(
            [
                "uv",
                "run",
                "--isolated",
                "--no-dev",
                "--package",
                project,
                "python",
                # -P: do not prepend cwd, so legacy top-level dirs cannot mask a
                # member that wrongly imports them.
                "-P",
                "-c",
                code,
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(f"FAIL {project}: cannot import with only its declared deps")
            print(result.stdout + result.stderr)
            return 1
        print(f"ok   {project} ({len(modules)} modules)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
