"""Tests for the Settings levels (web_interface/routes/settings.py):
/settings, /settings/machine, /settings/contacts, /settings/payments,
/settings/comms (+ /settings/comms/test), /settings/mqtt, /settings/web.

Part 1's two @pytest.mark.skip'd tests for /config/payments and
/config/comms (the OLD routes/legacy.py endpoints, unrelated URLs from the
new /settings/payments and /settings/comms this file covers) live in
tests/test_web_routes.py, which this task may not edit (owned by another
task in this wave — see .superpowers/sdd/part2/task-13-brief.md, executor
resolution 2). They are intentionally left in place; this file's coverage
is the "real coverage" the brief says replaces them, it just cannot make
the skip count in that other file's suite drop on its own.
"""

import pytest

from config.config_model import Channel
from services.access import Role
from web_interface.routes import settings as settings_routes

SECRET_MASK = settings_routes.SECRET_MASK

# The literal default secret values baked into config.config_model — none
# of these must ever appear as a rendered *value* in a Payments or Comms
# page (brief resolution 4 / the task's "grep for real default secret
# values" check). "paypal_client_id"/"paypal_client_secret" are excluded
# from this plain substring list on purpose: those two default secret
# *values* happen to be spelled identically to their own field's HTML
# id/name attribute (config.config_model's dummy PayPalConfig defaults),
# so a bare substring search would flag the harmless
# id="paypal_client_secret" markup as a false positive. They are checked
# separately below as a rendered `value="..."` instead, which only the
# real secret text (not the field's id/name) could match.
_REAL_DEFAULT_SECRETS = [
    "sk_test_xxx",
    "whsec_xxx",
    "your_auth_token",
    "ACxxxxxxxxxxxxxxxxxxx",
]


class TestSettingsHomeGating:
    """GET /settings: gate is edit_contacts or edit_secrets (owner and
    secretary only per ROLE_PERMISSIONS); tiles are filtered per-role."""

    @pytest.mark.parametrize(
        "role,expect_status",
        [
            (Role.owner, 200),
            (Role.secretary, 200),
            (Role.tech, 403),
            (Role.loader, 403),
        ],
    )
    def test_settings_home_status_per_role(self, login_as, wired, role, expect_status):
        worker = login_as(role)
        resp = worker.get("/settings")
        assert resp.status_code == expect_status

    def test_owner_sees_all_six_tiles(self, login_as, wired):
        owner = login_as(Role.owner)
        resp = owner.get("/settings")
        assert resp.status_code == 200
        for url in (
            "/settings/machine",
            "/settings/contacts",
            "/settings/payments",
            "/settings/comms",
            "/settings/mqtt",
            "/settings/web",
        ):
            assert url in resp.text

    def test_secretary_sees_only_machine_and_contacts(self, login_as, wired):
        """Brief resolution 7: a role lacking edit_secrets must not have
        the Payments/Comms/MQTT/Web URLs anywhere in the HTML — absent,
        not merely hidden by CSS."""
        secretary = login_as(Role.secretary)
        resp = secretary.get("/settings")
        assert resp.status_code == 200
        assert "/settings/machine" in resp.text
        assert "/settings/contacts" in resp.text
        for url in (
            "/settings/payments",
            "/settings/comms",
            "/settings/mqtt",
            "/settings/web",
        ):
            assert url not in resp.text


class TestMachinePage:
    @pytest.mark.parametrize(
        "role,expect_status",
        [
            (Role.owner, 200),
            (Role.secretary, 200),
            (Role.tech, 403),
            (Role.loader, 403),
        ],
    )
    def test_get_status_per_role(self, login_as, wired, role, expect_status):
        worker = login_as(role)
        resp = worker.get("/settings/machine")
        assert resp.status_code == expect_status

    def test_post_requires_htmx_header(self, client):
        resp = client.post(
            "/settings/machine",
            headers={"HX-Request": ""},
            data={"name": "x", "address": "y", "notes": "z"},
        )
        assert resp.status_code == 403

    def test_machine_id_renders_read_only(self, client, wired):
        cfg, _vmc, _inv, _store = wired
        resp = client.get("/settings/machine")
        assert resp.status_code == 200
        assert f'value="{cfg.machine_id}"' in resp.text
        assert 'id="machine_id" value="{}" disabled'.format(cfg.machine_id) in resp.text

    def test_post_ignores_machine_id(self, client, wired):
        """Brief resolution 10: read-only means the handler ignores it, a
        disabled <input> is not a guard on its own — reachable by direct
        POST."""
        cfg, _vmc, _inv, _store = wired
        original = cfg.machine_id
        resp = client.post(
            "/settings/machine",
            data={
                "name": "New Name",
                "address": "1 New St",
                "notes": "",
                "machine_id": "HACKED-ID",
            },
        )
        assert resp.status_code == 200
        assert cfg.machine_id == original

    def test_post_round_trips_through_save_config(self, client, wired, tmp_path):
        cfg, _vmc, _inv, _store = wired
        resp = client.post(
            "/settings/machine",
            data={"name": "New Name", "address": "42 New Ave", "notes": "quiet corner"},
        )
        assert resp.status_code == 200
        assert cfg.physical.common_name == "New Name"
        assert cfg.physical.location.address == "42 New Ave"
        assert cfg.physical.location.notes == "quiet corner"

        saved = (tmp_path / "config.json").read_text(encoding="utf-8")
        assert "New Name" in saved
        assert "42 New Ave" in saved

    def test_save_config_failure_returns_form_with_error_and_does_not_revert(
        self, client, wired, monkeypatch
    ):
        """Spec §5 / brief resolution 5: the opposite of the rollback
        instinct — the in-memory model keeps what the user submitted even
        though the write to disk failed."""
        cfg, _vmc, _inv, _store = wired

        def _raise(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(settings_routes.config_store, "save_config", _raise)

        resp = client.post(
            "/settings/machine",
            data={"name": "Submitted Name", "address": "Submitted Addr", "notes": ""},
        )
        assert resp.status_code == 200
        assert "Could not save changes" in resp.text
        # Not reverted: the live model still holds what was submitted.
        assert cfg.physical.common_name == "Submitted Name"
        assert cfg.physical.location.address == "Submitted Addr"
        # And the re-rendered form shows the submitted value, not the old one.
        assert 'value="Submitted Name"' in resp.text


class TestContactsPage:
    @pytest.mark.parametrize(
        "role,expect_status",
        [
            (Role.owner, 200),
            (Role.secretary, 200),
            (Role.tech, 403),
            (Role.loader, 403),
        ],
    )
    def test_get_status_per_role(self, login_as, wired, role, expect_status):
        worker = login_as(role)
        resp = worker.get("/settings/contacts")
        assert resp.status_code == expect_status

    def test_post_requires_htmx_header(self, client):
        resp = client.post(
            "/settings/contacts",
            headers={"HX-Request": ""},
            data={
                "owner_name": "A",
                "owner_email": "a@example.com",
                "loc_name": "B",
                "loc_email": "b@example.com",
            },
        )
        assert resp.status_code == 403

    def test_post_round_trips_through_save_config(self, client, wired, tmp_path):
        cfg, _vmc, _inv, _store = wired
        resp = client.post(
            "/settings/contacts",
            data={
                "owner_name": "Ada Owner",
                "owner_email": "ada.owner@example.com",
                "owner_phone": "555-1000",
                "owner_address": "1 Owner Way",
                "owner_notes": "",
                "owner_channels": ["email", "sms"],
                "loc_name": "Lou Cation",
                "loc_email": "lou@example.com",
                "loc_phone": "",
                "loc_address": "",
                "loc_notes": "",
                "loc_channels": ["email"],
            },
        )
        assert resp.status_code == 200
        assert cfg.physical.people.machine_owner.name == "Ada Owner"
        assert cfg.physical.people.machine_owner.email == "ada.owner@example.com"
        assert cfg.physical.people.machine_owner.preferred_comm == [
            Channel.email,
            Channel.sms,
        ]
        assert cfg.physical.people.location_owner.name == "Lou Cation"

        saved = (tmp_path / "config.json").read_text(encoding="utf-8")
        assert "Ada Owner" in saved
        assert "Lou Cation" in saved


class TestPaymentsPage:
    """edit_secrets is owner-only per ROLE_PERMISSIONS."""

    @pytest.mark.parametrize(
        "role,expect_status",
        [
            (Role.owner, 200),
            (Role.secretary, 403),
            (Role.tech, 403),
            (Role.loader, 403),
        ],
    )
    def test_get_status_per_role(self, login_as, wired, role, expect_status):
        worker = login_as(role)
        resp = worker.get("/settings/payments")
        assert resp.status_code == expect_status

    def test_post_requires_htmx_header(self, client):
        resp = client.post("/settings/payments", headers={"HX-Request": ""}, data={})
        assert resp.status_code == 403

    def test_rendered_page_never_contains_real_secret_values(self, client):
        resp = client.get("/settings/payments")
        assert resp.status_code == 200
        for secret in _REAL_DEFAULT_SECRETS:
            assert secret not in resp.text
        assert 'value="paypal_client_id"' not in resp.text
        assert 'value="paypal_client_secret"' not in resp.text
        assert resp.text.count(SECRET_MASK) >= 4  # stripe x2, paypal x2

    @pytest.mark.parametrize(
        "form_field,config_getter",
        [
            (
                "stripe_api_key",
                lambda cfg: cfg.payment.stripe.api_key,
            ),
            (
                "stripe_webhook_secret",
                lambda cfg: cfg.payment.stripe.webhook_secret,
            ),
            (
                "paypal_client_id",
                lambda cfg: cfg.payment.paypal.client_id,
            ),
            (
                "paypal_client_secret",
                lambda cfg: cfg.payment.paypal.client_secret,
            ),
        ],
    )
    def test_unchanged_mask_leaves_secret_intact(
        self, client, wired, form_field, config_getter
    ):
        cfg, _vmc, _inv, _store = wired
        original = config_getter(cfg).get_secret_value()
        data = {
            "stripe_api_key": SECRET_MASK,
            "stripe_webhook_secret": SECRET_MASK,
            "paypal_client_id": SECRET_MASK,
            "paypal_client_secret": SECRET_MASK,
        }
        resp = client.post("/settings/payments", data=data)
        assert resp.status_code == 200
        assert config_getter(cfg).get_secret_value() == original

    @pytest.mark.parametrize(
        "form_field,config_getter",
        [
            ("stripe_api_key", lambda cfg: cfg.payment.stripe.api_key),
            ("stripe_webhook_secret", lambda cfg: cfg.payment.stripe.webhook_secret),
            ("paypal_client_id", lambda cfg: cfg.payment.paypal.client_id),
            ("paypal_client_secret", lambda cfg: cfg.payment.paypal.client_secret),
        ],
    )
    def test_new_value_overwrites_secret(
        self, client, wired, form_field, config_getter
    ):
        cfg, _vmc, _inv, _store = wired
        data = {
            "stripe_api_key": SECRET_MASK,
            "stripe_webhook_secret": SECRET_MASK,
            "paypal_client_id": SECRET_MASK,
            "paypal_client_secret": SECRET_MASK,
        }
        data[form_field] = "brand-new-secret-value"
        resp = client.post("/settings/payments", data=data)
        assert resp.status_code == 200
        assert config_getter(cfg).get_secret_value() == "brand-new-secret-value"

    def test_paypal_none_renders_not_configured_and_post_is_a_noop(self, client, wired):
        cfg, _vmc, _inv, _store = wired
        cfg.payment.paypal = None
        resp = client.get("/settings/payments")
        assert resp.status_code == 200
        assert "PayPal is not configured" in resp.text

        resp = client.post(
            "/settings/payments",
            data={
                "stripe_api_key": SECRET_MASK,
                "stripe_webhook_secret": SECRET_MASK,
                "paypal_client_id": "whatever",
                "paypal_client_secret": "whatever",
            },
        )
        assert resp.status_code == 200
        assert cfg.payment.paypal is None

    def test_save_config_failure_does_not_revert(self, client, wired, monkeypatch):
        cfg, _vmc, _inv, _store = wired

        def _raise(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(settings_routes.config_store, "save_config", _raise)

        resp = client.post(
            "/settings/payments",
            data={
                "stripe_api_key": "fresh-key",
                "stripe_webhook_secret": SECRET_MASK,
                "paypal_client_id": SECRET_MASK,
                "paypal_client_secret": SECRET_MASK,
            },
        )
        assert resp.status_code == 200
        assert "Could not save changes" in resp.text
        assert cfg.payment.stripe.api_key.get_secret_value() == "fresh-key"


class TestCommsPage:
    @pytest.mark.parametrize(
        "role,expect_status",
        [
            (Role.owner, 200),
            (Role.secretary, 403),
            (Role.tech, 403),
            (Role.loader, 403),
        ],
    )
    def test_get_status_per_role(self, login_as, wired, role, expect_status):
        worker = login_as(role)
        resp = worker.get("/settings/comms")
        assert resp.status_code == expect_status

    def test_post_requires_htmx_header(self, client):
        resp = client.post(
            "/settings/comms",
            headers={"HX-Request": ""},
            data={"smtp_server": "x", "smtp_port": "587"},
        )
        assert resp.status_code == 403

    def test_rendered_page_never_contains_real_secret_values(self, client):
        resp = client.get("/settings/comms")
        assert resp.status_code == 200
        for secret in _REAL_DEFAULT_SECRETS:
            assert secret not in resp.text
        assert resp.text.count(SECRET_MASK) >= 3  # email password, sid, token

    def _comms_data(self, **overrides):
        data = {
            "smtp_server": "smtp.example.com",
            "smtp_port": "587",
            "smtp_username": "user@example.com",
            "smtp_password": SECRET_MASK,
            "default_from": "user@example.com",
            "sms_account_sid": SECRET_MASK,
            "sms_auth_token": SECRET_MASK,
            "sms_from_number": "+1234567890",
        }
        data.update(overrides)
        return data

    def test_unchanged_masks_leave_secrets_intact(self, client, wired):
        cfg, _vmc, _inv, _store = wired
        original_password = cfg.communication.email_gateway.password.get_secret_value()
        original_sid = cfg.communication.sms_gateway.account_sid.get_secret_value()
        original_token = cfg.communication.sms_gateway.auth_token.get_secret_value()

        resp = client.post("/settings/comms", data=self._comms_data())
        assert resp.status_code == 200
        assert (
            cfg.communication.email_gateway.password.get_secret_value()
            == original_password
        )
        assert (
            cfg.communication.sms_gateway.account_sid.get_secret_value() == original_sid
        )
        assert (
            cfg.communication.sms_gateway.auth_token.get_secret_value()
            == original_token
        )

    def test_new_password_overwrites(self, client, wired):
        cfg, _vmc, _inv, _store = wired
        resp = client.post(
            "/settings/comms", data=self._comms_data(smtp_password="new-smtp-pw")
        )
        assert resp.status_code == 200
        assert (
            cfg.communication.email_gateway.password.get_secret_value() == "new-smtp-pw"
        )

    def test_new_sms_secrets_overwrite(self, client, wired):
        cfg, _vmc, _inv, _store = wired
        resp = client.post(
            "/settings/comms",
            data=self._comms_data(
                sms_account_sid="new-sid", sms_auth_token="new-token"
            ),
        )
        assert resp.status_code == 200
        assert cfg.communication.sms_gateway.account_sid.get_secret_value() == "new-sid"
        assert (
            cfg.communication.sms_gateway.auth_token.get_secret_value() == "new-token"
        )

    def test_non_secret_fields_round_trip(self, client, wired, tmp_path):
        cfg, _vmc, _inv, _store = wired
        resp = client.post(
            "/settings/comms",
            data=self._comms_data(smtp_server="smtp.real-provider.test"),
        )
        assert resp.status_code == 200
        assert cfg.communication.email_gateway.smtp_server == "smtp.real-provider.test"
        saved = (tmp_path / "config.json").read_text(encoding="utf-8")
        assert "smtp.real-provider.test" in saved

    def test_save_config_failure_does_not_revert(self, client, wired, monkeypatch):
        cfg, _vmc, _inv, _store = wired

        def _raise(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(settings_routes.config_store, "save_config", _raise)

        resp = client.post(
            "/settings/comms",
            data=self._comms_data(smtp_server="submitted.example.net"),
        )
        assert resp.status_code == 200
        assert "Could not save changes" in resp.text
        assert cfg.communication.email_gateway.smtp_server == "submitted.example.net"

    def test_send_test_email_reports_failure_inline_when_unconfigured(
        self, client, wired
    ):
        """Brief resolution 8: an unconfigured gateway (still pointing at
        the blank-config placeholder host) must report failure inline
        without raising or 500ing — checked before any real network call."""
        cfg, _vmc, _inv, _store = wired
        assert not cfg.communication.email_gateway.is_configured
        resp = client.post("/settings/comms/test")
        assert resp.status_code == 200
        assert "not configured" in resp.text
        assert "text-red-600" in resp.text

    def test_send_test_email_reports_success_addressed_to_owner(
        self, client, wired, monkeypatch
    ):
        cfg, _vmc, _inv, _store = wired
        cfg.communication.email_gateway.smtp_server = "smtp.real-provider.test"
        assert cfg.communication.email_gateway.is_configured

        sent_to = {}

        async def _fake_send_email(email_config, to, subject, body):
            sent_to["to"] = to
            return True

        monkeypatch.setattr(settings_routes, "send_email", _fake_send_email)

        resp = client.post("/settings/comms/test")
        assert resp.status_code == 200
        assert sent_to["to"] == cfg.physical.people.machine_owner.email
        assert cfg.physical.people.machine_owner.email in resp.text
        assert "text-green-700" in resp.text

    def test_send_test_email_reports_failure_when_send_fails(
        self, client, wired, monkeypatch
    ):
        cfg, _vmc, _inv, _store = wired
        cfg.communication.email_gateway.smtp_server = "smtp.real-provider.test"

        async def _fake_send_email(*args, **kwargs):
            return False

        monkeypatch.setattr(settings_routes, "send_email", _fake_send_email)

        resp = client.post("/settings/comms/test")
        assert resp.status_code == 200
        assert "failed to send" in resp.text
        assert "text-red-600" in resp.text

    def test_send_test_requires_htmx_header(self, client):
        resp = client.post("/settings/comms/test", headers={"HX-Request": ""})
        assert resp.status_code == 403


class TestMqttPage:
    @pytest.mark.parametrize(
        "role,expect_status",
        [
            (Role.owner, 200),
            (Role.secretary, 403),
            (Role.tech, 403),
            (Role.loader, 403),
        ],
    )
    def test_get_status_per_role(self, login_as, wired, role, expect_status):
        worker = login_as(role)
        resp = worker.get("/settings/mqtt")
        assert resp.status_code == expect_status

    def test_post_requires_htmx_header(self, client):
        resp = client.post(
            "/settings/mqtt",
            headers={"HX-Request": ""},
            data={"broker_host": "x", "broker_port": "1883"},
        )
        assert resp.status_code == 403

    def test_password_masked_when_set(self, client, wired):
        cfg, _vmc, _inv, _store = wired
        cfg.mqtt.password = None
        from pydantic import SecretStr

        cfg.mqtt.password = SecretStr("real-mqtt-secret")
        resp = client.get("/settings/mqtt")
        assert resp.status_code == 200
        assert "real-mqtt-secret" not in resp.text
        assert SECRET_MASK in resp.text

    def test_mqtt_password_env_override_disables_field_and_is_not_persisted(
        self, client, wired, monkeypatch
    ):
        """Brief resolution 6: field disabled, effective value indicated,
        and a save never persists the env value into config.json."""
        cfg, _vmc, _inv, _store = wired
        monkeypatch.setenv("MQTT_PASSWORD", "env-secret-password")

        resp = client.get("/settings/mqtt")
        assert resp.status_code == 200
        assert "env-secret-password" not in resp.text
        assert 'id="password"' in resp.text
        assert "disabled" in resp.text
        assert "set by environment" in resp.text.lower()

        post_resp = client.post(
            "/settings/mqtt",
            data={
                "broker_host": cfg.mqtt.broker_host,
                "broker_port": str(cfg.mqtt.broker_port),
                "username": cfg.mqtt.username or "",
                "password": "attempted-write",
            },
        )
        assert post_resp.status_code == 200
        assert cfg.mqtt.password is None

    def test_broker_host_env_override_disables_field_and_is_not_persisted(
        self, client, wired, monkeypatch
    ):
        cfg, _vmc, _inv, _store = wired
        original_host = cfg.mqtt.broker_host
        monkeypatch.setenv("MQTT_BROKER_HOST", "env-broker.example")

        resp = client.get("/settings/mqtt")
        assert resp.status_code == 200
        assert "env-broker.example" in resp.text  # not a secret, safe to show

        client.post(
            "/settings/mqtt",
            data={
                "broker_host": "attempted-write.example",
                "broker_port": str(cfg.mqtt.broker_port),
                "username": cfg.mqtt.username or "",
                "password": SECRET_MASK,
            },
        )
        assert cfg.mqtt.broker_host == original_host

    def test_post_round_trips_non_overridden_fields(self, client, wired, tmp_path):
        cfg, _vmc, _inv, _store = wired
        resp = client.post(
            "/settings/mqtt",
            data={
                "broker_host": "new-broker.local",
                "broker_port": "1884",
                "username": "newuser",
                "password": "fresh-mqtt-secret",
            },
        )
        assert resp.status_code == 200
        assert cfg.mqtt.broker_host == "new-broker.local"
        assert cfg.mqtt.broker_port == 1884
        assert cfg.mqtt.username == "newuser"
        assert cfg.mqtt.password.get_secret_value() == "fresh-mqtt-secret"


class TestWebPage:
    @pytest.mark.parametrize(
        "role,expect_status",
        [
            (Role.owner, 200),
            (Role.secretary, 403),
            (Role.tech, 403),
            (Role.loader, 403),
        ],
    )
    def test_get_status_per_role(self, login_as, wired, role, expect_status):
        worker = login_as(role)
        resp = worker.get("/settings/web")
        assert resp.status_code == expect_status

    def test_post_requires_htmx_header(self, client):
        resp = client.post(
            "/settings/web", headers={"HX-Request": ""}, data={"trusted_proxies": ""}
        )
        assert resp.status_code == 403

    def test_host_and_port_render_read_only(self, client, wired):
        cfg, _vmc, _inv, _store = wired
        resp = client.get("/settings/web")
        assert resp.status_code == 200
        assert f'value="{cfg.web.host}"' in resp.text
        assert f'value="{cfg.web.port}"' in resp.text
        assert resp.text.count("disabled") >= 2

    def test_post_ignores_host_and_port(self, client, wired):
        cfg, _vmc, _inv, _store = wired
        original_host, original_port = cfg.web.host, cfg.web.port
        resp = client.post(
            "/settings/web",
            data={
                "host": "0.0.0.0",  # attempted change, same value on purpose
                "port": "9999",
                "trusted_proxies": "10.0.0.0/8",
            },
        )
        assert resp.status_code == 200
        assert cfg.web.host == original_host
        assert cfg.web.port == original_port
        assert cfg.web.port != 9999

    def test_trusted_proxies_round_trip(self, client, wired, tmp_path):
        cfg, _vmc, _inv, _store = wired
        resp = client.post(
            "/settings/web",
            data={"trusted_proxies": "10.0.0.0/8\n172.16.0.0/12"},
        )
        assert resp.status_code == 200
        assert cfg.web.trusted_proxies == ["10.0.0.0/8", "172.16.0.0/12"]
        saved = (tmp_path / "config.json").read_text(encoding="utf-8")
        assert "10.0.0.0/8" in saved
