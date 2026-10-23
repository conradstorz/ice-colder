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


EDGE_OVERLAY = REPO_ROOT / "docker" / "docker-compose.edge.yml"


def test_edge_image_has_a_healthcheck_on_healthz() -> None:
    text = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    checks = re.findall(r"^HEALTHCHECK\b.*$", text, re.M)
    assert len(checks) == 1
    assert "/healthz" in text.split("HEALTHCHECK", 1)[1]


def test_edge_overlay_vmc_is_pinned_and_unwatched() -> None:
    vmc = _load(EDGE_OVERLAY)["services"]["vmc"]
    assert "build" not in vmc
    labels = vmc.get("labels", [])
    assert not any("watchtower" in label for label in labels)
    assert "ICE_COLDER_IMAGE_TAG" in vmc["image"]
    assert vmc["stop_grace_period"] == "30s"
    assert vmc["logging"] == EXPECTED_LOGGING


def test_edge_overlay_has_no_simulator_service() -> None:
    services = _load(EDGE_OVERLAY)["services"]
    assert not [name for name in services if name.startswith("sim")]
    for svc in services.values():
        assert "ice_sims" not in str(svc.get("command", ""))
