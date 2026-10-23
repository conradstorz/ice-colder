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


def test_check_web_bind_exits_1_when_port_held_with_reuseaddr():
    with socket.socket() as holder:
        holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        holder.bind(("127.0.0.1", 0))
        holder.listen()
        port = holder.getsockname()[1]
        with pytest.raises(SystemExit) as excinfo:
            check_web_bind("127.0.0.1", port)
    assert excinfo.value.code == 1


def test_check_web_bind_exits_1_on_unresolvable_host(caplog):
    caplog.set_level("ERROR")
    with pytest.raises(SystemExit) as excinfo:
        check_web_bind("no-such-host.invalid", 26123)
    assert excinfo.value.code == 1
    text = " ".join(r.message for r in caplog.records)
    assert "could not resolve host" in text and "errno" not in text


def test_check_web_bind_probes_every_resolved_address(monkeypatch):
    with socket.socket() as holder:
        holder.bind(("127.0.0.1", 0))
        holder.listen()
        port = holder.getsockname()[1]
        free = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.2", port))
        busy = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))
        monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [free, busy])
        with pytest.raises(SystemExit):
            check_web_bind("somehost", port)
