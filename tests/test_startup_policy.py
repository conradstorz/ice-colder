import pytest
from pydantic import SecretStr

import main as main_mod
from config.config_model import ConfigModel, WebConfig
from services.config_store import save_config


def _web(host="0.0.0.0", password="changeme"):
    return WebConfig(host=host, admin_password=SecretStr(password))


def test_weak_password_on_public_host_exits(monkeypatch):
    monkeypatch.delenv("ICE_COLDER_ALLOW_WEAK_PASSWORD", raising=False)
    with pytest.raises(SystemExit) as e:
        main_mod.enforce_password_policy(_web())
    assert e.value.code == 1


def test_short_password_on_public_host_exits(monkeypatch):
    monkeypatch.delenv("ICE_COLDER_ALLOW_WEAK_PASSWORD", raising=False)
    with pytest.raises(SystemExit):
        main_mod.enforce_password_policy(_web(password="short-pw"))


def test_weak_password_on_loopback_is_allowed(monkeypatch):
    monkeypatch.delenv("ICE_COLDER_ALLOW_WEAK_PASSWORD", raising=False)
    main_mod.enforce_password_policy(_web(host="127.0.0.1"))


def test_bypass_flag_downgrades_to_warning(monkeypatch):
    monkeypatch.setenv("ICE_COLDER_ALLOW_WEAK_PASSWORD", "1")
    main_mod.enforce_password_policy(_web())


def test_strong_password_passes(monkeypatch):
    monkeypatch.delenv("ICE_COLDER_ALLOW_WEAK_PASSWORD", raising=False)
    main_mod.enforce_password_policy(_web(password="correct-horse-battery"))


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
