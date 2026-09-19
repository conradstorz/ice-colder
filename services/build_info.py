# services/build_info.py
"""Which build is this? Resolved once at import, never raises.

1. ICE_COLDER_COMMIT / ICE_COLDER_BUILD_TIME env (set by the Dockerfile from
   CI build args)                                   -> source="image"
2. a git checkout in the repo root                   -> source="git"
3. otherwise                                         -> "unknown" everywhere
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

UNKNOWN = "unknown"
_REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class BuildInfo:
    commit: str
    commit_short: str
    build_time: str
    source: str  # "image" | "git" | "unknown"


def _run_git(cwd: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def resolve_build_info(
    env: Mapping[str, str] | None = None, cwd: Path | None = None
) -> BuildInfo:
    env = os.environ if env is None else env
    cwd = _REPO_ROOT if cwd is None else cwd

    commit = env.get("ICE_COLDER_COMMIT", "").strip()
    if commit:
        return BuildInfo(
            commit=commit,
            commit_short=commit[:7],
            build_time=env.get("ICE_COLDER_BUILD_TIME", "").strip() or UNKNOWN,
            source="image",
        )

    sha = _run_git(cwd, "rev-parse", "HEAD") if cwd.is_dir() else None
    if sha:
        dirty = bool(_run_git(cwd, "status", "--porcelain"))
        commit_time = _run_git(cwd, "log", "-1", "--format=%cI") or UNKNOWN
        return BuildInfo(
            commit=sha,
            commit_short=sha[:7] + ("-dirty" if dirty else ""),
            build_time=commit_time,
            source="git",
        )

    return BuildInfo(UNKNOWN, UNKNOWN, UNKNOWN, UNKNOWN)


BUILD_INFO = resolve_build_info()
