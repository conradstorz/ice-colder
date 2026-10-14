"""The autouse fixture must shield tests from host-exported config env vars."""

import os
import subprocess
import sys
from tests.paths import REPO_ROOT as ROOT
from tests.paths import TESTS_ROOT


def test_exported_config_env_does_not_redirect_saves(tmp_path):
    sentinel = tmp_path / "sentinel" / "config.json"
    sentinel.parent.mkdir()
    env = dict(os.environ, ICE_COLDER_CONFIG=str(sentinel))
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            str(TESTS_ROOT / "_env_isolation_probe.py"),
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert not sentinel.exists()
