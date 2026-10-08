"""Exercise logging setup in subprocesses so pytest's handlers stay intact."""

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def run_logging_script(tmp_path, script):
    return subprocess.run(
        [sys.executable, "-c", script, str(ROOT)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )


def test_import_does_not_reconfigure_logging_or_create_files(tmp_path):
    result = run_logging_script(
        tmp_path,
        """
import sys
from io import StringIO
sys.path.insert(0, sys.argv[1])
from loguru import logger

sink = StringIO()
logger.remove()
logger.add(sink, format="{message}")
from services.logging_setup import setup_logging

logger.info("existing-handler")
assert sink.getvalue() == "existing-handler\\n"
assert "controller.vmc" not in sys.modules
assert "web_interface.server" not in sys.modules
""",
    )
    assert result.stdout == ""
    assert not (tmp_path / "LOGS").exists()


@pytest.mark.parametrize("setup_count", [1, 2])
def test_setup_routes_messages_without_duplicate_handlers(tmp_path, setup_count):
    result = run_logging_script(
        tmp_path,
        f"""
import sys
sys.path.insert(0, sys.argv[1])
from loguru import logger
from services.logging_setup import setup_logging

for _ in range({setup_count}):
    setup_logging()
logger.debug("debug-marker")
logger.info("general-marker")
logger.bind(transaction=True).info("transaction-marker")
logger.bind(ice_maker=True).info("ice-marker")
logger.bind(vending=True).info("vending-marker")
logger.bind(transaction=False, ice_maker=False, vending=False).info("false-marker")
logger.remove()
""",
    )
    assert "debug-marker" not in result.stdout
    for marker in (
        "general-marker",
        "transaction-marker",
        "ice-marker",
        "vending-marker",
        "false-marker",
    ):
        assert result.stdout.count(marker) == 1
    assert "| INFO" in result.stdout

    log_dir = tmp_path / "LOGS"
    general = (log_dir / "vmc.log").read_text()
    assert "debug-marker;DEBUG " in general
    for marker in (
        "general-marker",
        "transaction-marker",
        "ice-marker",
        "vending-marker",
        "false-marker",
    ):
        assert general.count(marker) == 1
        assert f"{marker};INFO " in general

    for filename, marker in (
        ("transactions.log", "transaction-marker"),
        ("ice_maker.log", "ice-marker"),
        ("vending.log", "vending-marker"),
    ):
        lines = (log_dir / filename).read_text().splitlines()
        assert len(lines) == 1
        assert lines[0].endswith(f" | {marker}")
