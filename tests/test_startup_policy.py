import pytest
from pydantic import SecretStr

import main as main_mod
from config.config_model import ConfigModel, WebConfig


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
    main_mod.apply_env_overrides(cfg)
    assert cfg.mqtt.broker_host == "mosquitto"
    assert cfg.mqtt.username == "vmc"
    assert cfg.mqtt.password.get_secret_value() == "s3cret-value"
    assert cfg.web.trusted_proxies == ["172.25.0.0/16", "10.0.0.0/8"]


def test_env_overrides_absent_leave_config_alone(monkeypatch):
    for k in (
        "MQTT_BROKER_HOST",
        "MQTT_USERNAME",
        "MQTT_PASSWORD",
        "ICE_COLDER_TRUSTED_PROXIES",
    ):
        monkeypatch.delenv(k, raising=False)
    cfg = ConfigModel()
    main_mod.apply_env_overrides(cfg)
    assert cfg.mqtt.username is None and cfg.mqtt.password is None
    assert cfg.web.trusted_proxies == []


def test_web_config_trusted_proxies_default_empty():
    assert WebConfig().trusted_proxies == []
