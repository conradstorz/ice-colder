import socket

import pytest

from ice_edge.app.startup_config import check_web_bind


def test_check_web_bind_passes_on_free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    check_web_bind("127.0.0.1", port)
    # The probe socket was closed at once: the port can be bound again.
    with socket.socket() as again:
        again.bind(("127.0.0.1", port))


def test_check_web_bind_exits_1_when_port_held(caplog):
    with socket.socket() as holder:
        holder.bind(("127.0.0.1", 0))
        holder.listen()
        port = holder.getsockname()[1]
        caplog.set_level("ERROR")
        with pytest.raises(SystemExit) as excinfo:
            check_web_bind("127.0.0.1", port)
    assert excinfo.value.code == 1
    text = " ".join(r.message for r in caplog.records)
    assert "127.0.0.1" in text and str(port) in text and "errno" in text
