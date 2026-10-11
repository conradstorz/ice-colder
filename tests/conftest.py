"""Shared fixtures. Isolates every test from the developer's real config.json."""

import asyncio
import json
import logging
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from loguru import logger as _loguru

import services.access as access
import services.config_store as config_store
from config.config_model import ConfigModel, PhysicalDetails
from controller.machine import Machine
from services.access import AccessStore, Role, User
from services.dispensers import DispenserProfiles
from services.inventory_manager import InventoryManager
from tests.dispenser_fixtures import GOOD as DISPENSER_GOOD
from tests.fakes import FakeTaskRunner
from tests.dispenser_fixtures import ICE as DISPENSER_ICE
from tests.dispenser_fixtures import WATER as DISPENSER_WATER
from tests.skip_policy import MISSING_TERMINALREPORTER_MARKER, build_skip_report
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
    machine = Machine(config=cfg)
    vmc = machine.vmc
    inv = InventoryManager([], path=tmp_path / "inventory.json")
    store = AccessStore(path=tmp_path / "access.json")
    store.create_user("Ada", "ada@example.com", Role.owner, "1379")
    store.finalize_setup()

    routes.set_config_object(cfg)
    routes.set_machine_instance(machine)
    routes.set_inventory_manager(inv)
    routes.set_access_store(store)

    yield cfg, vmc, inv, store

    routes.set_access_store(None)
    machine.cancel_pending_tasks()
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


@pytest.fixture
def machine():
    """The composition root (`controller/machine.py`'s `Machine`) built
    fresh for a test, with a real `TaskRunner`. `vmc` below yields
    `machine.vmc` and depends on this fixture, so a test taking both
    shares the same `Machine` instance.
    """
    m = Machine(config=ConfigModel())
    yield m
    m.cancel_pending_tasks()


@pytest.fixture
def vmc(machine):
    """The VMC owned by `machine` above -- for tests that only need the
    sale FSM itself, never a `Machine`-level `set_*`/wiring method."""
    yield machine.vmc


@pytest.fixture
async def machine_fake_time():
    """A `Machine` wired to a `FakeTaskRunner` (`tests/fakes.py`) instead
    of a real event-loop timer (VMC public surface design, section 2):
    yields `(machine, runner)` so a test can arm a timer through an
    ordinary `Machine`/VMC call (`deposit_funds`, `process_payment`,
    `begin_maintenance`, ...) and then fire it by label --
    `runner.fire("dispense_timeout")` -- rather than reaching into a
    private per-timer task handle.
    """
    cfg = ConfigModel()
    runner = FakeTaskRunner()
    m = Machine(config=cfg, tasks=runner)
    m.attach_to_loop(asyncio.get_running_loop())
    yield m, runner
    m.cancel_pending_tasks()


@pytest.fixture
async def vmc_fake_time(machine_fake_time):
    """`(vmc, runner)` over `machine_fake_time` above, for tests that only
    need the VMC itself."""
    machine, runner = machine_fake_time
    yield machine.vmc, runner


@pytest.fixture
def dispenser_profiles(tmp_path, monkeypatch):
    """A loaded `DispenserProfiles` backed by the `GOOD` two-slot
    text (slot 1 bagged ice, slot 2 water fill) and a `ConfigModel` whose
    catalog matches it -- for tests that need a ready profile set without
    rebuilding the fixture text and config themselves."""
    path = tmp_path / "dispensers.toml"
    path.write_text(DISPENSER_GOOD, encoding="utf-8")
    monkeypatch.setenv("ICE_COLDER_DISPENSERS", str(path))

    config = ConfigModel(
        physical=PhysicalDetails(products=[DISPENSER_ICE, DISPENSER_WATER])
    )
    profiles = DispenserProfiles(config)
    profiles.load()
    return profiles


def pytest_sessionfinish(session):
    """Write a machine-readable report of all skipped tests.

    This hook runs at the end of the test session and writes a JSON file
    listing every skipped test's nodeid and reason. The CI workflow uses
    this file (via tests/run_skip_guard.py) to enforce the skip policy:
    only recognised skip reasons are allowed; any other skip fails the
    build.

    The report is written to skip-report.json in the working directory.
    This hook is intentionally a thin adapter: the report-building logic
    lives in the pure, importable `build_skip_report` (tests/skip_policy.py)
    so it can be unit-tested directly, and the two failure modes below are
    handled here rather than left to chance:

    - If pytest's own terminalreporter can't be found (or has no .stats),
      writing "[]" would be indistinguishable from a real run with zero
      skips, and the guard would report a false "Guard OK". Write the
      explicit MISSING_TERMINALREPORTER_MARKER instead so the guard fails
      loudly and names the cause.
    - A failure to write the report file itself (unwritable cwd, a
      transiently locked file -- plausible on Windows) must never fail a
      developer's local `pytest` run, which the brief requires to keep
      passing regardless of this guard. The guard already fails closed in
      CI when the file is simply missing, so swallowing the write error
      here (with a loud terminal warning) still turns a broken CI run red.
    """
    terminalreporter = None
    if hasattr(session, "config") and hasattr(session.config, "pluginmanager"):
        terminalreporter = session.config.pluginmanager.get_plugin("terminalreporter")

    if terminalreporter is not None and hasattr(terminalreporter, "stats"):
        report_data = build_skip_report(terminalreporter.stats)
    else:
        report_data = MISSING_TERMINALREPORTER_MARKER

    report_path = Path("skip-report.json")
    try:
        with open(report_path, "w") as f:
            json.dump(report_data, f, indent=2)
    except OSError as e:
        print(
            f"\nWARNING: could not write {report_path}: {e}. "
            "The skip guard depends on this file in CI; if this happens "
            "there it will fail closed (missing file), but locally this "
            "must not fail your test run, so continuing.",
            file=sys.stderr,
        )
        return

    if isinstance(report_data, list) and report_data:
        print(f"\nWrote {len(report_data)} skipped test(s) to {report_path}")
