from pathlib import Path

import pytest

from ice_cloud.settings import CloudSettings, SettingsError

KEY = "k" * 32


def test_from_env_minimal():
    s = CloudSettings.from_env({"ICE_CLOUD_SESSION_KEY": KEY})
    assert s.data_dir == Path("cloud_data")
    assert s.public_url is None
    assert s.google_client_id is None and s.google_client_secret is None
    assert s.admins == frozenset()
    assert s.session_key == KEY.encode()
    assert s.insecure_cookies is False
    assert s.google_configured is False


def test_missing_session_key_is_error():
    with pytest.raises(SettingsError, match="ICE_CLOUD_SESSION_KEY"):
        CloudSettings.from_env({})


def test_short_session_key_is_error():
    with pytest.raises(SettingsError, match="32"):
        CloudSettings.from_env({"ICE_CLOUD_SESSION_KEY": "k" * 31})


def test_google_without_public_url_is_error():
    env = {
        "ICE_CLOUD_SESSION_KEY": KEY,
        "ICE_CLOUD_GOOGLE_CLIENT_ID": "id",
        "ICE_CLOUD_GOOGLE_CLIENT_SECRET": "secret",
    }
    with pytest.raises(SettingsError, match="ICE_CLOUD_PUBLIC_URL"):
        CloudSettings.from_env(env)
    s = CloudSettings.from_env({**env, "ICE_CLOUD_PUBLIC_URL": "https://c.example"})
    assert s.google_configured is True


def test_google_half_configured_is_error():
    env = {
        "ICE_CLOUD_SESSION_KEY": KEY,
        "ICE_CLOUD_PUBLIC_URL": "https://c.example",
        "ICE_CLOUD_GOOGLE_CLIENT_ID": "id",
    }
    with pytest.raises(SettingsError, match="GOOGLE"):
        CloudSettings.from_env(env)


def test_admins_normalised():
    s = CloudSettings.from_env(
        {
            "ICE_CLOUD_SESSION_KEY": KEY,
            "ICE_CLOUD_ADMINS": " A@Example.com, ,b@example.com,,",
            "ICE_CLOUD_INSECURE_COOKIES": "1",
            "ICE_CLOUD_DATA": "somewhere",
        }
    )
    assert s.admins == frozenset({"a@example.com", "b@example.com"})
    assert s.insecure_cookies is True
    assert s.data_dir == Path("somewhere")


def test_trusted_ui_digests_parsed_and_validated():
    from ice_cloud.settings import CloudSettings, SettingsError

    base = {"ICE_CLOUD_SESSION_KEY": "k" * 32}
    assert CloudSettings.from_env(base).trusted_ui_digests == frozenset()
    good = {**base, "ICE_CLOUD_TRUSTED_UI_DIGESTS": f"{'a' * 64}, {'B' * 64} ,"}
    assert CloudSettings.from_env(good).trusted_ui_digests == {"a" * 64, "b" * 64}
    bad = {**base, "ICE_CLOUD_TRUSTED_UI_DIGESTS": "nope"}
    with pytest.raises(SettingsError):
        CloudSettings.from_env(bad)


def test_smtp_unset_is_not_configured():
    s = CloudSettings.from_env({"ICE_CLOUD_SESSION_KEY": KEY})
    assert s.smtp_host is None
    assert s.smtp_configured is False


def test_smtp_host_without_from_is_error():
    with pytest.raises(SettingsError, match="ICE_CLOUD_SMTP_FROM"):
        CloudSettings.from_env(
            {"ICE_CLOUD_SESSION_KEY": KEY, "ICE_CLOUD_SMTP_HOST": "smtp.example"}
        )


def test_smtp_defaults_port_and_starttls():
    s = CloudSettings.from_env(
        {
            "ICE_CLOUD_SESSION_KEY": KEY,
            "ICE_CLOUD_SMTP_HOST": "smtp.example",
            "ICE_CLOUD_SMTP_FROM": "alerts@example.com",
        }
    )
    assert s.smtp_configured is True
    assert s.smtp_port == 587
    assert s.smtp_starttls is True
    assert s.smtp_user is None and s.smtp_password is None
    assert s.smtp_from == "alerts@example.com"


def test_smtp_bad_port_is_error():
    base = {
        "ICE_CLOUD_SESSION_KEY": KEY,
        "ICE_CLOUD_SMTP_HOST": "smtp.example",
        "ICE_CLOUD_SMTP_FROM": "alerts@example.com",
    }
    for bad in ("nope", "0", "65536"):
        with pytest.raises(SettingsError, match="ICE_CLOUD_SMTP_PORT"):
            CloudSettings.from_env({**base, "ICE_CLOUD_SMTP_PORT": bad})
    s = CloudSettings.from_env({**base, "ICE_CLOUD_SMTP_PORT": "2525"})
    assert s.smtp_port == 2525


def test_smtp_starttls_zero_disables():
    base = {
        "ICE_CLOUD_SESSION_KEY": KEY,
        "ICE_CLOUD_SMTP_HOST": "smtp.example",
        "ICE_CLOUD_SMTP_FROM": "alerts@example.com",
    }
    assert (
        CloudSettings.from_env({**base, "ICE_CLOUD_SMTP_STARTTLS": "0"}).smtp_starttls
        is False
    )
    assert (
        CloudSettings.from_env({**base, "ICE_CLOUD_SMTP_STARTTLS": "1"}).smtp_starttls
        is True
    )
