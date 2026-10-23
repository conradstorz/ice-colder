"""install_exit_handlers: the app's own SIGTERM/SIGINT handlers (R13)."""

import signal

import pytest

from ice_edge.app.exit_signals import install_exit_handlers


@pytest.fixture
def requests():
    calls: list[int] = []
    before_term = signal.getsignal(signal.SIGTERM)
    before_int = signal.getsignal(signal.SIGINT)
    yield calls
    signal.signal(signal.SIGTERM, before_term)
    signal.signal(signal.SIGINT, before_int)


def test_sigterm_handler_requests_exit(requests):
    restore = install_exit_handlers(lambda: requests.append(1))
    try:
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)
        handler(signal.SIGTERM, None)
        assert requests == [1]
    finally:
        restore()


def test_sigint_handler_requests_exit(requests):
    restore = install_exit_handlers(lambda: requests.append(1))
    try:
        signal.getsignal(signal.SIGINT)(signal.SIGINT, None)
        assert requests == [1]
    finally:
        restore()


def test_second_signal_does_not_raise(requests):
    restore = install_exit_handlers(lambda: requests.append(1))
    try:
        handler = signal.getsignal(signal.SIGTERM)
        handler(signal.SIGTERM, None)
        handler(signal.SIGTERM, None)
        assert requests == [1, 1]
    finally:
        restore()


def test_restore_puts_previous_handlers_back(requests):
    before_term = signal.getsignal(signal.SIGTERM)
    before_int = signal.getsignal(signal.SIGINT)
    restore = install_exit_handlers(lambda: requests.append(1))
    assert signal.getsignal(signal.SIGTERM) is not before_term
    restore()
    assert signal.getsignal(signal.SIGTERM) == before_term
    assert signal.getsignal(signal.SIGINT) == before_int
