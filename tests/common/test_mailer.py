"""Tests for ice_common/mailer.py: the blocking SMTP send, with a fake SMTP class."""

from email.message import EmailMessage

import pytest

from ice_common.mailer import SMTP_TIMEOUT_SECONDS, smtp_send


class FakeSMTP:
    """Stands in for smtplib.SMTP; records every call made on it."""

    def __init__(self, host, port, timeout=None, *, fail_on=None, error=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.calls = []
        self._fail_on = fail_on
        self._error = error or OSError("boom")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.calls.append(("close",))
        return False

    def _maybe_fail(self, name):
        if self._fail_on == name:
            raise self._error

    def starttls(self):
        self._maybe_fail("starttls")
        self.calls.append(("starttls",))

    def login(self, user, password):
        self._maybe_fail("login")
        self.calls.append(("login", user, password))

    def send_message(self, msg):
        self._maybe_fail("send_message")
        self.calls.append(("send_message", msg))


@pytest.fixture
def servers():
    return []


@pytest.fixture
def factory(servers):
    def make(fail_on=None, error=None):
        def build(host, port, timeout=None):
            server = FakeSMTP(host, port, timeout, fail_on=fail_on, error=error)
            servers.append(server)
            return server

        return build

    return make


def test_smtp_send_starttls_login_send(factory, servers):
    msg = EmailMessage()
    smtp_send(
        host="smtp.test.local",
        port=587,
        username="vmc@test.local",
        password="secret",
        use_starttls=True,
        msg=msg,
        smtp_factory=factory(),
    )
    (server,) = servers
    assert (server.host, server.port) == ("smtp.test.local", 587)
    assert server.timeout == SMTP_TIMEOUT_SECONDS == 15.0
    assert server.calls == [
        ("starttls",),
        ("login", "vmc@test.local", "secret"),
        ("send_message", msg),
        ("close",),
    ]


def test_smtp_send_no_login_without_username(factory, servers):
    msg = EmailMessage()
    smtp_send(
        host="smtp.test.local",
        port=25,
        username=None,
        password=None,
        use_starttls=True,
        msg=msg,
        smtp_factory=factory(),
    )
    (server,) = servers
    assert ("login", None, None) not in server.calls
    assert not any(call[0] == "login" for call in server.calls)
    assert ("send_message", msg) in server.calls


def test_smtp_send_no_starttls_when_disabled(factory, servers):
    msg = EmailMessage()
    smtp_send(
        host="smtp.test.local",
        port=25,
        username="u",
        password="p",
        use_starttls=False,
        msg=msg,
        smtp_factory=factory(),
    )
    (server,) = servers
    assert not any(call[0] == "starttls" for call in server.calls)
    assert server.calls == [
        ("login", "u", "p"),
        ("send_message", msg),
        ("close",),
    ]


def test_smtp_send_propagates_errors(factory, servers):
    with pytest.raises(OSError, match="relay refused"):
        smtp_send(
            host="smtp.test.local",
            port=587,
            username="u",
            password="p",
            use_starttls=True,
            msg=EmailMessage(),
            smtp_factory=factory(
                fail_on="send_message", error=OSError("relay refused")
            ),
        )
    (server,) = servers
    # The connection is still closed on the way out of the failure.
    assert server.calls[-1] == ("close",)
