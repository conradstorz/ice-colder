"""Static hygiene checks for the Dockerfiles and compose files (no Docker needed)."""

from __future__ import annotations

import re

import yaml

from tests.repo_root import REPO_ROOT

DOCKERFILES = [
    REPO_ROOT / "Dockerfile",
    REPO_ROOT / "packages" / "cloud" / "Dockerfile",
]
LOG_COMPOSE_FILES = [
    REPO_ROOT / "docker-compose.yml",
    REPO_ROOT / "docker" / "docker-compose.prod.yml",
]
EXPECTED_LOGGING = {
    "driver": "json-file",
    "options": {"max-size": "10m", "max-file": "3"},
}


def _load(path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_uv_is_pinned_to_a_version() -> None:
    for path in DOCKERFILES:
        text = path.read_text(encoding="utf-8")
        images = re.findall(r"--from=(ghcr\.io/astral-sh/uv:\S+)", text)
        assert images, f"{path} does not copy uv"
        for image in images:
            tag = image.split(":", 1)[1]
            assert tag != "latest", f"{path} uses uv:latest"
            assert re.fullmatch(r"\d+\.\d+\.\d+", tag), f"{path}: {image}"


def test_cmd_execs_python_directly() -> None:
    for path in DOCKERFILES:
        cmds = re.findall(
            r"^CMD\s+(\[.*\])\s*$", path.read_text(encoding="utf-8"), re.M
        )
        assert len(cmds) == 1, path
        assert not cmds[0].startswith('["uv"'), f"{path} CMD starts with uv"
        assert cmds[0].startswith('["python"'), path


def test_compose_services_do_not_use_uv_run() -> None:
    for path in LOG_COMPOSE_FILES:
        for name, svc in _load(path)["services"].items():
            command = svc.get("command")
            if isinstance(command, list):
                assert command[:1] != ["uv"], f"{path.name}:{name} runs uv"


def test_every_service_has_bounded_logs() -> None:
    for path in LOG_COMPOSE_FILES:
        for name, svc in _load(path)["services"].items():
            assert svc.get("logging") == EXPECTED_LOGGING, f"{path.name}:{name}"


def test_vmc_and_cloud_have_a_grace_period() -> None:
    vmc = _load(LOG_COMPOSE_FILES[0])["services"]["vmc"]
    assert vmc["stop_grace_period"] == "30s"
    cloud = _load(REPO_ROOT / "docker" / "docker-compose.cloud.yml")["services"][
        "cloud"
    ]
    assert cloud["stop_grace_period"] == "30s"
