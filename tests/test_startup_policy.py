import main as main_mod
from config.config_model import ConfigModel, WebConfig
from services.access import AccessStore, Role
from services.config_store import save_config


def test_env_overrides_mqtt_credentials_and_trusted_proxies(monkeypatch):
    monkeypatch.setenv("MQTT_BROKER_HOST", "mosquitto")
    monkeypatch.setenv("MQTT_USERNAME", "vmc")
    monkeypatch.setenv("MQTT_PASSWORD", "s3cret-value")
    monkeypatch.setenv("ICE_COLDER_TRUSTED_PROXIES", "172.25.0.0/16, 10.0.0.0/8")
    cfg = ConfigModel()
    overrides = main_mod.apply_env_overrides(cfg)

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
    overrides = main_mod.apply_env_overrides(cfg)

    # No env set: overrides fall back to the config's own (default) values...
    assert overrides.mqtt.username is None
    assert overrides.mqtt.password is None
    assert overrides.trusted_proxies == []

    # ...and the config itself is untouched either way.
    assert cfg.mqtt.username is None and cfg.mqtt.password is None
    assert cfg.web.trusted_proxies == []


def test_env_password_never_reaches_saved_config(tmp_path, monkeypatch):
    monkeypatch.setenv("MQTT_PASSWORD", "env-only-secret-value")
    for k in ("MQTT_BROKER_HOST", "MQTT_USERNAME", "ICE_COLDER_TRUSTED_PROXIES"):
        monkeypatch.delenv(k, raising=False)

    cfg = ConfigModel()
    main_mod.apply_env_overrides(cfg)

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
