import asyncio

import pytest

from ice_edge import __main__ as edge_main


def test_unhandled_startup_error_exits_nonzero(monkeypatch):
    """An unhandled exception during startup (e.g. a bad data volume) must
    exit 1 so Docker's restart loop shows a failure, not a clean exit 0."""

    def boom():
        raise RuntimeError("startup exploded")

    monkeypatch.setattr(edge_main, "setup_logging", boom)

    with pytest.raises(SystemExit) as excinfo:
        asyncio.run(edge_main.main())

    assert excinfo.value.code == 1
