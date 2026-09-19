# tests/test_build_info.py
"""Build identity: image env vars win, git checkout is the fallback, else unknown."""

import subprocess
from pathlib import Path

from services.build_info import BuildInfo, resolve_build_info


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def test_env_wins():
    info = resolve_build_info(
        env={
            "ICE_COLDER_COMMIT": "abcdef1234567890",
            "ICE_COLDER_BUILD_TIME": "2026-09-18T00:00:00Z",
        },
        cwd=Path("/nonexistent"),
    )
    assert info == BuildInfo(
        commit="abcdef1234567890",
        commit_short="abcdef1",
        build_time="2026-09-18T00:00:00Z",
        source="image",
    )


def test_env_commit_without_time():
    info = resolve_build_info(
        env={"ICE_COLDER_COMMIT": "abcdef1"}, cwd=Path("/nonexistent")
    )
    assert info.source == "image" and info.build_time == "unknown"


def test_git_fallback(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(
        tmp_path,
        "-c",
        "user.email=t@t",
        "-c",
        "user.name=t",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "x",
    )
    sha = _git(tmp_path, "rev-parse", "HEAD")
    info = resolve_build_info(env={}, cwd=tmp_path)
    assert info.source == "git"
    assert info.commit == sha
    assert info.commit_short == sha[:7]
    assert info.build_time != "unknown"


def test_git_dirty_suffix(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(
        tmp_path,
        "-c",
        "user.email=t@t",
        "-c",
        "user.name=t",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "x",
    )
    (tmp_path / "scratch.txt").write_text("x", encoding="utf-8")
    info = resolve_build_info(env={}, cwd=tmp_path)
    assert info.commit_short.endswith("-dirty")


def test_unknown_when_nothing(tmp_path):
    info = resolve_build_info(env={}, cwd=tmp_path)  # not a git repo
    assert info == BuildInfo("unknown", "unknown", "unknown", "unknown")


def test_vmc_status_carries_version():
    from services.build_info import BUILD_INFO
    from services.mqtt_messages import VMCStatus

    assert VMCStatus(state="idle").version == BUILD_INFO.commit_short
