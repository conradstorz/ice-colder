import json
import sqlite3
import time

import pytest

import main as main_mod
from services import startup_config
from config.config_model import ConfigModel, WebConfig
from contracts.vending_machine import FaultCode
from controller.vmc import VMC
from services.access import AccessStore, Role
from services.config_store import save_config
from services.event_recorder import EventRecorder
from services import event_recorder as event_recorder_module
from services.startup_recovery import reconcile_sales_journal_faults


def test_env_overrides_mqtt_credentials_and_trusted_proxies(monkeypatch):
    monkeypatch.setenv("MQTT_BROKER_HOST", "mosquitto")
    monkeypatch.setenv("MQTT_USERNAME", "vmc")
    monkeypatch.setenv("MQTT_PASSWORD", "s3cret-value")
    monkeypatch.setenv("ICE_COLDER_TRUSTED_PROXIES", "172.25.0.0/16, 10.0.0.0/8")
    cfg = ConfigModel()
    overrides = startup_config.apply_env_overrides(cfg)

    # Returned overrides carry the env values...
    assert overrides.mqtt.broker_host == "mosquitto"
    assert overrides.mqtt.username == "vmc"
    assert overrides.mqtt.password.get_secret_value() == "s3cret-value"
    assert overrides.trusted_proxies == ["172.25.0.0/16", "10.0.0.0/8"]

    # ...but the live config passed in is never mutated.
    assert cfg.mqtt.broker_host != "mosquitto"
    assert cfg.mqtt.username is None
    assert cfg.mqtt.password is None
    assert cfg.web.trusted_proxies == []


def test_env_overrides_absent_leave_config_alone(monkeypatch):
    for k in (
        "MQTT_BROKER_HOST",
        "MQTT_USERNAME",
        "MQTT_PASSWORD",
        "ICE_COLDER_TRUSTED_PROXIES",
    ):
        monkeypatch.delenv(k, raising=False)
    cfg = ConfigModel()
    overrides = startup_config.apply_env_overrides(cfg)

    # No env set: overrides fall back to the config's own (default) values...
    assert overrides.mqtt.username is None
    assert overrides.mqtt.password is None
    assert overrides.trusted_proxies == []

    # ...and the config itself is untouched either way.
    assert cfg.mqtt.username is None and cfg.mqtt.password is None
    assert cfg.web.trusted_proxies == []


def test_mqtt_env_overrides_reports_only_configured_values(monkeypatch):
    monkeypatch.setenv("MQTT_BROKER_HOST", "mosquitto")
    monkeypatch.setenv("MQTT_USERNAME", "vmc")
    monkeypatch.setenv("MQTT_PASSWORD", "secret")

    assert startup_config.mqtt_env_overrides() == {
        "broker_host": "mosquitto",
        "username": "vmc",
        "password": "secret",
    }


def test_env_password_never_reaches_saved_config(tmp_path, monkeypatch):
    monkeypatch.setenv("MQTT_PASSWORD", "env-only-secret-value")
    for k in ("MQTT_BROKER_HOST", "MQTT_USERNAME", "ICE_COLDER_TRUSTED_PROXIES"):
        monkeypatch.delenv(k, raising=False)

    cfg = ConfigModel()
    startup_config.apply_env_overrides(cfg)

    save_config(cfg, tmp_path / "config.json")
    assert "env-only-secret-value" not in (tmp_path / "config.json").read_text()


def test_web_config_trusted_proxies_default_empty():
    assert WebConfig().trusted_proxies == []


def test_warn_if_setup_mode_warns_with_no_owner(tmp_path, caplog):
    """No owner yet: warn_if_setup_mode logs a warning pointing at /setup and
    never exits — a dashboard concern must never stop the machine selling."""
    store = AccessStore(path=tmp_path / "access.json")
    caplog.set_level("WARNING")

    main_mod.warn_if_setup_mode(store)  # must not raise SystemExit

    messages = [r.message for r in caplog.records]
    assert any("setup mode" in m and "/setup" in m for m in messages), messages


def test_warn_if_setup_mode_silent_once_owner_exists(tmp_path, caplog):
    store = AccessStore(path=tmp_path / "access.json")
    store.create_user(name="Owner", email=None, role=Role.owner, pin="48213")
    caplog.set_level("WARNING")
    caplog.clear()

    main_mod.warn_if_setup_mode(store)

    messages = [r.message for r in caplog.records]
    assert not any("setup mode" in m for m in messages)


def test_warn_if_setup_mode_logs_error_on_corrupt_store_and_never_exits(
    tmp_path, caplog
):
    """A corrupt access.json is a dashboard-only failure: log at error level,
    say the VMC and MQTT client keep running, and never call sys.exit."""
    bad = tmp_path / "access.json"
    bad.write_text("{not valid json", encoding="utf-8")
    store = AccessStore(path=bad)
    assert store.corrupt
    caplog.set_level("WARNING")

    main_mod.warn_if_setup_mode(store)  # must not raise SystemExit

    messages = [r.message for r in caplog.records]
    assert any(
        "corrupt" in m.lower() and ("VMC" in m or "MQTT" in m) for m in messages
    ), messages


def test_reconcile_replays_nonempty_journal_and_clears_data_101(tmp_path, monkeypatch):
    """A non-empty journal must be drained, its row inserted, and DATA-101 —
    pre-existing from before this boot, since _machine_faults is in-memory
    and does not survive a restart — cleared once nothing is left stuck."""
    journal_path = tmp_path / "sales-journal.jsonl"
    monkeypatch.setattr(event_recorder_module, "JOURNAL_PATH", journal_path)
    journal_path.write_text(
        json.dumps(
            {
                "ts": time.time(),
                "sku": "ICE-1",
                "name": "Ice Bag",
                "slot": 0,
                "price": 2.50,
                "methods": {"cash_bill": 2.50},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    db_path = tmp_path / "events.db"
    recorder = EventRecorder(db_path=str(db_path))

    vmc = VMC(config=ConfigModel())
    # Simulate the fault already being set (e.g. a health-monitor alert that
    # outlived the process) so this test actually exercises "clear", not just
    # "never got raised in the first place".
    vmc.raise_data_fault(FaultCode.DATA_101, outcome="pre-existing")
    assert "DATA-101" in {f["code"] for f in vmc.active_faults()}

    reconcile_sales_journal_faults(vmc, recorder)

    assert "DATA-101" not in {f["code"] for f in vmc.active_faults()}
    # The journal must be drained -- absent or empty, not merely "replay
    # returned > 0" (see reconcile_sales_journal_faults' own docstring).
    assert (
        not journal_path.exists()
        or not journal_path.read_text(encoding="utf-8").strip()
    )

    with sqlite3.connect(str(db_path)) as conn:
        rows = conn.execute(
            "SELECT sku, name, slot, price, methods FROM sales"
        ).fetchall()
    assert len(rows) == 1
    sku, name, slot, price, methods_json = rows[0]
    assert sku == "ICE-1"
    assert name == "Ice Bag"
    assert slot == 0
    assert price == pytest.approx(2.50)
    assert json.loads(methods_json) == {"cash_bill": 2.50}


def test_reconcile_replay_returning_zero_does_not_wrongly_clear_data_101(
    tmp_path, monkeypatch
):
    """replay_sales_journal()'s return value is only the count *inserted this
    call* -- 0 both when there is nothing to do and when every row was
    rejected, so a caller must never key clearing DATA-101 off it being > 0.
    A malformed row (violates the sales table's NOT NULL sku) is set aside as
    rejected evidence, replay returns 0, and the journal still drains -- so
    DATA-101 must still clear here, proving the code checks drainage and not
    the integer.
    """
    journal_path = tmp_path / "sales-journal.jsonl"
    monkeypatch.setattr(event_recorder_module, "JOURNAL_PATH", journal_path)
    journal_path.write_text(
        json.dumps(
            {
                "ts": time.time(),
                "sku": None,  # violates the NOT NULL column -> rejected, not inserted
                "name": "Broken",
                "slot": 0,
                "price": 1.00,
                "methods": {"cash": 1.00},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    recorder = EventRecorder(db_path=str(tmp_path / "events.db"))

    vmc = VMC(config=ConfigModel())
    vmc.raise_data_fault(FaultCode.DATA_101, outcome="pre-existing")

    reconcile_sales_journal_faults(vmc, recorder)

    assert "DATA-101" not in {f["code"] for f in vmc.active_faults()}
    assert (
        not journal_path.exists()
        or not journal_path.read_text(encoding="utf-8").strip()
    )
    # The rejected row was preserved as evidence, not silently dropped.
    rejected_path = journal_path.with_name("sales-journal.rejected.jsonl")
    assert rejected_path.exists()


def test_reconcile_corrupt_db_raises_data_102_and_vmc_stays_usable(
    tmp_path, monkeypatch
):
    """A corrupt events.db must not stop the process: DATA-102 is raised and
    the VMC (and by extension the MQTT client, started from the same main()
    regardless of this call) keeps running."""
    journal_path = tmp_path / "sales-journal.jsonl"
    monkeypatch.setattr(event_recorder_module, "JOURNAL_PATH", journal_path)
    db_path = tmp_path / "events.db"
    db_path.write_bytes(b"this is not a valid sqlite database, just garbage bytes")

    recorder = EventRecorder(db_path=str(db_path))  # quarantines + recreates
    assert recorder.db_was_corrupt is True  # proves the corrupt branch was entered

    vmc = VMC(config=ConfigModel())

    reconcile_sales_journal_faults(vmc, recorder)  # must not raise/exit

    assert "DATA-102" in {f["code"] for f in vmc.active_faults()}
    # The VMC keeps working: its fault registry and FSM are untouched by the
    # data-layer problem (DATA-102 is deliberately absent from
    # PAYMENT_BLOCKING_FAULTS -- see contracts/vending_machine.py).
    assert vmc.state == "idle"
    assert vmc.clear_fault("DATA-102") is True
    assert "DATA-102" not in {f["code"] for f in vmc.active_faults()}


def test_reconcile_never_raises_on_recorder_failure(monkeypatch):
    """Neither fault may ever escape as an exception and unwind main() --
    a reports/history problem must never stop the VMC or MQTT client."""

    class ExplodingRecorder:
        db_was_corrupt = False

        def replay_sales_journal(self):
            raise RuntimeError("disk exploded")

    vmc = VMC(config=ConfigModel())
    reconcile_sales_journal_faults(vmc, ExplodingRecorder())  # must not raise
    assert vmc.state == "idle"  # completely unaffected


def test_reconcile_raises_data_101_when_replay_commits_but_journal_rewrite_fails(
    tmp_path, monkeypatch
):
    """If replay_sales_journal() commits its row(s) to `sales` but then
    raises before it can rewrite/truncate the journal (e.g. the final
    os.replace cannot complete -- a full or read-only volume), the journal
    file is still non-empty. reconcile_sales_journal_faults must not let
    that exception escape to its own outer swallow-everything handler
    before checking the journal's state: DATA-101 must still be raised (or
    retained) so the operator gets an alert instead of the next boot
    silently rediscovering the same stuck journal.
    """
    journal_path = tmp_path / "sales-journal.jsonl"
    monkeypatch.setattr(event_recorder_module, "JOURNAL_PATH", journal_path)
    payload = {
        "ts": time.time(),
        "sku": "ICE-1",
        "name": "Ice Bag",
        "slot": 0,
        "price": 2.50,
        "methods": {"cash_bill": 2.50},
    }
    journal_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    db_path = tmp_path / "events.db"
    real_recorder = EventRecorder(db_path=str(db_path))

    class RewriteFailsAfterCommitRecorder:
        """Wraps a real recorder: replay genuinely inserts the row (so
        `sales` reflects a real commit, like the scenario under test) but
        then raises instead of truncating the journal -- reproducing "the
        insert succeeded, the final journal rewrite did not" without
        needing to fake os.replace internals."""

        db_was_corrupt = False

        def replay_sales_journal(self):
            real_recorder.record_sale(
                payload["sku"],
                payload["name"],
                payload["slot"],
                payload["price"],
                payload["methods"],
                ts=payload["ts"],
                idempotent=True,
            )
            raise OSError("journal rewrite: os.replace could not complete")

    vmc = VMC(config=ConfigModel())

    reconcile_sales_journal_faults(
        vmc, RewriteFailsAfterCommitRecorder()
    )  # must not raise/exit

    assert "DATA-101" in {f["code"] for f in vmc.active_faults()}

    # The row genuinely landed in `sales` -- this is "committed but not
    # drained", not merely "nothing happened".
    with sqlite3.connect(str(db_path)) as conn:
        count = conn.execute("SELECT COUNT(*) FROM sales").fetchone()[0]
    assert count == 1
    # And the journal file itself is still non-empty, proving the alert
    # matches reality rather than being raised blindly.
    assert journal_path.read_text(encoding="utf-8").strip()
