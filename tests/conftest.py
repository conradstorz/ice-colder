"""Shared fixtures. Isolates every test from the developer's real config.json."""

import logging

import pytest
from fastapi.testclient import TestClient
from loguru import logger as _loguru

import services.access as access
import services.config_store as config_store
from config.config_model import ConfigModel
from controller.vmc import VMC
from services.access import AccessStore, Role, User
from services.inventory_manager import InventoryManager
from web_interface import auth as web_auth
from web_interface import routes
from web_interface.server import app

_ISSUED_CLIENTS: list[TestClient] = []


@pytest.fixture(autouse=True)
def isolated_config_path(tmp_path, monkeypatch):
    """Redirect config saves to a temp dir so no test can clobber config.json."""
    monkeypatch.setattr(config_store, "CONFIG_PATH", tmp_path / "config.json")


@pytest.fixture(autouse=True, scope="session")
def _fast_scrypt():
    """Lower the PIN/secret hashing cost for the whole test session.

    services.access._scrypt reads SCRYPT_N from the module at call time, so
    patching the module attribute here is enough to speed up hash_pin,
    verify_pin, hash_secret and verify_secret everywhere — no AccessStore
    constructor plumbing needed. Session-scoped so the (measured) 213.8 ms
    production cost is paid once conceptually, not per test; restored at
    session teardown so nothing after the run — or a production-default
    guard test reading the live attribute — sees the weakened value.
    Tests that need to assert the real shipped default instead read the
    literal out of the source file (see
    TestHashing.test_production_scrypt_n_default_is_unchanged in
    test_access.py), since this fixture patches the value for the whole
    session before any such test can run.
    """
    original = access.SCRYPT_N
    access.SCRYPT_N = 2**4
    yield
    access.SCRYPT_N = original


@pytest.fixture
def caplog(caplog):
    """Bridge loguru records into pytest's caplog (loguru bypasses stdlib logging)."""
    handler_id = _loguru.add(
        lambda msg: logging.getLogger("loguru").handle(
            logging.LogRecord(
                "loguru",
                msg.record["level"].no,
                "",
                0,
                msg.record["message"],
                None,
                None,
            )
        ),
        level="DEBUG",
    )
    yield caplog
    _loguru.remove(handler_id)


@pytest.fixture(autouse=True)
def _close_issued_clients():
    """Close every client `sign_in` handed out, whatever the test did.

    `sign_in` returns its client to the caller, so it cannot use a `with`
    block itself. Tests that go through the `login_as` fixture are covered
    by its own teardown, but several call `make_client(...)` directly and
    never close the result — an unconditional leak. Funnelling every
    issued client through here closes them all, and a second `.close()`
    from `login_as` is harmless.
    """
    yield
    while _ISSUED_CLIENTS:
        _ISSUED_CLIENTS.pop().close()


def sign_in(store: AccessStore, user: User, *, shared: bool = False) -> TestClient:
    """Mint a device, trust *user* on it, open a session, and return a
    client that is fully logged in: it carries vmc_device, vmc_session and
    the HX-Request: true header, the way a real enrolled browser would."""
    device, token = store.create_device(f"{user.name}'s device", shared=shared)
    store.trust_device(device.id, user.id)
    session_id = store.create_session(user.id, device.id)
    store.record_login(user.id)

    client = TestClient(app)
    client.headers["HX-Request"] = "true"
    client.cookies.set(web_auth.DEVICE_COOKIE, token)
    client.cookies.set(web_auth.SESSION_COOKIE, session_id)
    _ISSUED_CLIENTS.append(client)
    return client


def make_client(
    store: AccessStore,
    role: Role = Role.owner,
    *,
    shared: bool = False,
    name: str = "Ada",
) -> tuple[TestClient, User]:
    """Seed a fresh user of *role* and sign them in."""
    email = f"{name.lower().replace(' ', '.')}@example.com"
    user = store.create_user(name, email, role, "2468")
    return sign_in(store, user, shared=shared), user


@pytest.fixture
def wired(tmp_path):
    """A ConfigModel, VMC, InventoryManager and AccessStore, wired into
    routes the way main() wires them. The owner "Ada" is already seeded
    (email ada@example.com, PIN 1379) and setup is finalized, so route
    tests are never in setup mode.

    `web_auth.backoff` is a module-level singleton shared by every test in
    the process, keyed on a real clock — a PIN or emergency-code failure
    left behind by one test can trip a 429 in the next, and only for one
    of them depending on run order. Reset it on both sides so this
    fixture's tests are isolated from whatever ran immediately before or
    after them.
    """
    web_auth.backoff.reset()

    cfg = ConfigModel()
    vmc = VMC(config=cfg)
    inv = InventoryManager([], path=tmp_path / "inventory.json")
    store = AccessStore(path=tmp_path / "access.json")
    store.create_user("Ada", "ada@example.com", Role.owner, "1379")
    store.finalize_setup()

    routes.set_config_object(cfg)
    routes.set_vmc_instance(vmc)
    routes.set_inventory_manager(inv)
    routes.set_access_store(store)

    yield cfg, vmc, inv, store

    routes.set_access_store(None)
    for t in vmc._pending_tasks:
        t.cancel()
    web_auth.backoff.reset()
    web_auth.backoff.set_trusted_proxies([])


@pytest.fixture
def login_as(wired):
    """login_as(role=Role.owner, *, shared=False, name=None) -> TestClient.

    Role.owner returns a client for the fixture's existing owner (the store
    enforces one owner per machine); any other role seeds a fresh user.
    Every client this makes is closed on teardown.
    """
    _cfg, _vmc, _inv, store = wired
    clients: list[TestClient] = []

    def _login_as(
        role: Role = Role.owner, *, shared: bool = False, name: str | None = None
    ) -> TestClient:
        if role == Role.owner:
            client = sign_in(store, store.owner(), shared=shared)
        else:
            client, _user = make_client(
                store, role, shared=shared, name=name or role.value.capitalize()
            )
        clients.append(client)
        return client

    yield _login_as

    for c in clients:
        c.close()


@pytest.fixture
def client(login_as):
    """Every pre-existing test in this file runs through this client,
    authenticated as the owner."""
    return login_as(Role.owner)


@pytest.fixture
def anonymous(wired):
    """A client with no cookies at all, against a wired store that does
    have an owner — for asserting what an unauthenticated visitor gets."""
    c = TestClient(app, follow_redirects=False)
    yield c
    c.close()
