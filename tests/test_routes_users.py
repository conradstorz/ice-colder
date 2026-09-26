"""Tests for the Users levels (web_interface/routes/users.py): /users,
/users/{id}, /users/new, /devices, /users/codes, /users/ownership and their
POSTs.

This is task-12 of the dashboard-v2-shell plan: the largest and most
security-sensitive area move in part 2. Every guard here is a real bug part
1 found and fixed (see each test's docstring for the Copilot-review
history); tests/test_web_routes.py keeps its own retargeted coverage of the
same guarantees (see task-12-report.md for the itemised arithmetic) — this
file adds the shell-specific coverage: gating per level, the read-only/
self-lockout template halves, sub-tile absence, and the new profile-edit
route.
"""

import re

import pytest

from services.access import Role
from tests.conftest import make_client
from web_interface import auth as web_auth

_CODE_VALUE_RE = re.compile(r'class="[^"]*\bcode-value\b[^"]*">(\d{8})<')


def _codes_in(html: str) -> list[str]:
    return _CODE_VALUE_RE.findall(html)


class TestGateMatrix:
    """manage_users (owner, secretary) gates /users, /users/{id}, /users/new
    and /devices; manage_ownership (owner only) additionally gates
    /users/codes and /users/ownership (task-12 brief resolution 5)."""

    @pytest.mark.parametrize("role", [Role.owner, Role.secretary])
    @pytest.mark.parametrize("path", ["/users", "/users/new", "/devices"])
    def test_manage_users_roles_get_200(self, login_as, wired, role, path):
        worker = login_as(role)
        assert worker.get(path).status_code == 200

    @pytest.mark.parametrize("role", [Role.tech, Role.loader])
    @pytest.mark.parametrize("path", ["/users", "/users/new", "/devices"])
    def test_tech_and_loader_get_403(self, login_as, wired, role, path):
        worker = login_as(role)
        assert worker.get(path).status_code == 403

    def test_owner_gets_200_on_codes_and_ownership(self, client):
        assert client.get("/users/codes").status_code == 200
        assert client.get("/users/ownership").status_code == 200

    @pytest.mark.parametrize("role", [Role.secretary, Role.tech, Role.loader])
    @pytest.mark.parametrize("path", ["/users/codes", "/users/ownership"])
    def test_non_owner_roles_get_403_on_codes_and_ownership(
        self, login_as, wired, role, path
    ):
        worker = login_as(role)
        assert worker.get(path).status_code == 403

    def test_user_detail_page_is_200_for_manage_users_roles(self, login_as, wired):
        _cfg, _vmc, _inv, store = wired
        owner = login_as(Role.owner)
        secretary = login_as(Role.secretary)
        target = store.create_user("Target", "target@example.com", Role.loader, "5297")
        assert owner.get(f"/users/{target.id}").status_code == 200
        assert secretary.get(f"/users/{target.id}").status_code == 200

    @pytest.mark.parametrize("role", [Role.tech, Role.loader])
    def test_user_detail_page_403_for_tech_and_loader(self, login_as, wired, role):
        _cfg, _vmc, _inv, store = wired
        worker = login_as(role)
        owner = store.owner()
        assert worker.get(f"/users/{owner.id}").status_code == 403


class TestPostRequiresHtmx:
    """Every mutating route declares context.require_htmx (rule 1)."""

    @pytest.mark.parametrize(
        "path,data",
        [
            ("/users/new", {"name": "X", "email": "", "role": "loader", "pin": "5297"}),
            ("/users/codes/regenerate", {"pin": "1379"}),
            ("/users/report", {}),
            (
                "/users/transfer",
                {"pin": "1379", "emergency_code": "00000000"},
            ),
            ("/users/transfer/cancel", {"pin": "1379"}),
        ],
    )
    def test_post_without_htmx_header_is_403(self, client, path, data):
        resp = client.post(path, headers={"HX-Request": ""}, data=data)
        assert resp.status_code == 403

    def test_user_id_posts_without_htmx_header_are_403(self, login_as, wired):
        _cfg, _vmc, _inv, store = wired
        owner = login_as(Role.owner)
        target = store.create_user("T", "t@example.com", Role.loader, "5297")
        for path, data in [
            (f"/users/{target.id}/disable", {}),
            (f"/users/{target.id}/enable", {}),
            (f"/users/{target.id}/reset-pin", {"pin": "5297"}),
            (f"/users/{target.id}/delete", {}),
        ]:
            resp = owner.post(path, headers={"HX-Request": ""}, data=data)
            assert resp.status_code == 403

    def test_device_posts_without_htmx_header_are_403(self, client, wired):
        _cfg, _vmc, _inv, store = wired
        device_id = next(iter(store.devices))
        assert (
            client.post(
                f"/devices/{device_id}/forget", headers={"HX-Request": ""}
            ).status_code
            == 403
        )
        assert (
            client.post(
                f"/devices/{device_id}/shared", headers={"HX-Request": ""}
            ).status_code
            == 403
        )


class TestOwnerRowReadOnlyForSecretary:
    """§4.1 / task-12 brief resolution 6: a secretary opening the owner's
    /users/{id} sees plain read-only text, no Disable/Delete/Reset-PIN
    control, and the server refuses those writes regardless."""

    def test_secretary_sees_no_edit_form_or_action_controls(self, login_as, wired):
        _cfg, _vmc, _inv, store = wired
        secretary = login_as(Role.secretary)
        owner = store.owner()
        html = secretary.get(f"/users/{owner.id}").text
        assert f'name="name" value="{owner.name}"' not in html
        assert f"/users/{owner.id}/disable" not in html
        assert f"/users/{owner.id}/delete" not in html
        assert f"/users/{owner.id}/reset-pin" not in html
        assert owner.name in html  # still shown, just as text

    @pytest.mark.parametrize(
        "action,extra",
        [("disable", {}), ("delete", {}), ("reset-pin", {"pin": "5297"})],
    )
    def test_secretary_403_on_owner_targeting_writes(
        self, login_as, wired, action, extra
    ):
        _cfg, _vmc, _inv, store = wired
        secretary = login_as(Role.secretary)
        owner = store.owner()
        resp = secretary.post(f"/users/{owner.id}/{action}", data=extra)
        assert resp.status_code == 403
        refreshed = store.get_user(owner.id)
        assert refreshed.disabled is False

    def test_secretary_403_editing_owner_profile(self, login_as, wired):
        _cfg, _vmc, _inv, store = wired
        secretary = login_as(Role.secretary)
        owner = store.owner()
        resp = secretary.post(
            f"/users/{owner.id}",
            data={"name": "Hijacked", "email": "", "role": "owner"},
        )
        assert resp.status_code == 403
        assert store.get_user(owner.id).name == owner.name


class TestOwnerSelfLockout:
    """Part 1's 2dd8c41: the owner cannot disable or delete themselves, and
    those controls are not rendered for their own row. Both halves tested."""

    def test_no_disable_or_delete_control_on_own_row(self, client, wired):
        _cfg, _vmc, _inv, store = wired
        owner = store.owner()
        html = client.get(f"/users/{owner.id}").text
        assert f"/users/{owner.id}/disable" not in html
        assert f"/users/{owner.id}/delete" not in html
        # Reset PIN carries no lockout risk and must still be offered.
        assert f"/users/{owner.id}/reset-pin" in html

    @pytest.mark.parametrize("action", ["disable", "delete"])
    def test_server_refuses_self_disable_and_delete(self, client, wired, action):
        _cfg, _vmc, _inv, store = wired
        owner = store.owner()
        resp = client.post(f"/users/{owner.id}/{action}")
        assert resp.status_code == 403
        assert store.get_user(owner.id) is not None
        assert store.get_user(owner.id).disabled is False

    def test_owner_may_still_reset_their_own_pin(self, client, wired):
        _cfg, _vmc, _inv, store = wired
        owner = store.owner()
        resp = client.post(f"/users/{owner.id}/reset-pin", data={"pin": "8642"})
        assert resp.status_code == 200
        assert store.verify_user_pin(owner.id, "8642") is True


class TestDeviceOwnerGuard:
    """Part 1's 84eaed2: a device write targeting a device trusting the
    owner needs manage_ownership. Both the template and the server."""

    def test_owners_device_shows_no_controls_to_secretary(self, login_as, wired):
        _cfg, _vmc, _inv, store = wired
        login_as(Role.owner)
        secretary = login_as(Role.secretary)
        device_id = next(
            d.id
            for d in store.devices.values()
            if store.owner().id in d.trusted_user_ids
        )
        html = secretary.get("/devices").text
        assert f"/devices/{device_id}/forget" not in html
        assert f"/devices/{device_id}/shared" not in html

    def test_secretary_403_forgetting_owners_device(self, login_as, wired):
        _cfg, _vmc, _inv, store = wired
        owner = login_as(Role.owner)
        secretary = login_as(Role.secretary)
        device_id = next(
            d.id
            for d in store.devices.values()
            if store.owner().id in d.trusted_user_ids
        )
        resp = secretary.post(f"/devices/{device_id}/forget")
        assert resp.status_code == 403
        assert device_id in store.devices
        assert owner.get("/devices").status_code == 200


class TestResetPinDropsTrustAndSessions:
    """Part 1's 12a1115: reset-pin drops device trust AND ends the user's
    live sessions. Both effects tested."""

    def test_reset_pin_untrusts_every_device_and_ends_sessions(self, login_as, wired):
        _cfg, _vmc, _inv, store = wired
        owner = login_as(Role.owner)
        loader_client, loader = make_client(store, Role.loader, name="Loafer")
        with loader_client:
            old_hash = loader.pin_hash
            session_id = loader_client.cookies.get(web_auth.SESSION_COOKIE)
            assert store.resolve_session(session_id) is not None

            resp = owner.post(f"/users/{loader.id}/reset-pin", data={"pin": "5297"})
            assert resp.status_code == 200

            assert store.get_user(loader.id).pin_hash != old_hash
            assert all(
                loader.id not in d.trusted_user_ids for d in store.devices.values()
            )
            assert store.resolve_session(session_id) is None


class TestPlaintextCodesAreNoStore:
    """Part 1's 7fa2c14, extended to every response rendering a plaintext
    code: header only, never the body."""

    def test_regenerate_result_is_no_store(self, client, wired):
        _cfg, _vmc, _inv, store = wired
        resp = client.post("/users/codes/regenerate", data={"pin": "1379"})
        assert resp.headers["cache-control"] == "no-store"

    def test_transfer_start_result_is_no_store(self, client, wired):
        _cfg, _vmc, _inv, store = wired
        codes = store.generate_emergency_codes()
        resp = client.post(
            "/users/transfer", data={"pin": "1379", "emergency_code": codes[0]}
        )
        assert resp.headers["cache-control"] == "no-store"

    def test_regenerate_shows_twenty_codes_exactly_once(self, client, wired):
        resp = client.post("/users/codes/regenerate", data={"pin": "1379"})
        assert resp.status_code == 200
        assert len(_codes_in(resp.text)) == 20
        # A reload of the codes page must not show them again.
        reload_resp = client.get("/users/codes")
        assert _codes_in(reload_resp.text) == []


class TestWrongTransferPinConsumesNoCode:
    def test_wrong_pin_leaves_pool_untouched(self, client, wired):
        _cfg, _vmc, _inv, store = wired
        codes = store.generate_emergency_codes()
        resp = client.post(
            "/users/transfer", data={"pin": "0000", "emergency_code": codes[0]}
        )
        assert resp.status_code == 200
        assert store.pending_transfer is None
        assert store.unused_emergency_code_count() == 20


class TestTransferLeavesOwnerInControl:
    def test_started_transfer_leaves_owner_in_control_and_users_shows_pending(
        self, client, wired
    ):
        _cfg, _vmc, _inv, store = wired
        codes = store.generate_emergency_codes()
        resp = client.post(
            "/users/transfer", data={"pin": "1379", "emergency_code": codes[0]}
        )
        assert resp.status_code == 200
        assert store.owner().name == "Ada"
        assert client.get("/status").status_code == 200
        assert client.get("/users").status_code == 200

        users_page = client.get("/users").text
        assert "transfer" in users_page.lower()
        assert "pending" in users_page.lower()


class TestForgetDevice:
    def test_forget_removes_device_and_ends_its_sessions(self, login_as, wired):
        _cfg, _vmc, _inv, store = wired
        owner = login_as(Role.owner)
        loader_client, loader = make_client(store, Role.loader, name="Loafer2")
        with loader_client:
            device_id = next(
                d.id for d in store.devices.values() if loader.id in d.trusted_user_ids
            )
            session_id = loader_client.cookies.get(web_auth.SESSION_COOKIE)
            assert store.resolve_session(session_id) is not None

            resp = owner.post(f"/devices/{device_id}/forget")
            assert resp.status_code == 200
            assert device_id not in store.devices
            assert store.resolve_session(session_id) is None


class TestSubTileGating:
    """partials/tile.html (Task 5): a tile the role lacks is absent from
    the HTML entirely, not merely hidden (task-12 brief resolution 7)."""

    def test_secretary_response_omits_codes_and_ownership_urls(self, login_as, wired):
        secretary = login_as(Role.secretary)
        html = secretary.get("/users").text
        assert "/users/codes" not in html
        assert "/users/ownership" not in html
        assert "/devices" in html

    def test_owner_response_includes_every_sub_tile_url(self, client):
        html = client.get("/users").text
        assert "/users/codes" in html
        assert "/users/ownership" in html
        assert "/devices" in html


class TestUnknownIdIs404:
    """task-12 brief resolution 12: an unknown user or device id is a shell
    404 (HTTPException rendering error.html), on GET and POST alike."""

    def test_unknown_user_get_is_404(self, client):
        resp = client.get("/users/no-such-user")
        assert resp.status_code == 404
        assert "bar" in resp.text or "<header" in resp.text

    @pytest.mark.parametrize(
        "suffix,data",
        [
            ("", {"name": "X", "email": "", "role": "loader"}),
            ("/disable", {}),
            ("/enable", {}),
            ("/reset-pin", {"pin": "5297"}),
            ("/delete", {}),
        ],
    )
    def test_unknown_user_post_is_404(self, client, suffix, data):
        resp = client.post(f"/users/no-such-user{suffix}", data=data)
        assert resp.status_code == 404

    def test_unknown_device_get_actions_are_404(self, client):
        assert client.post("/devices/no-such-device/forget").status_code == 404
        assert client.post("/devices/no-such-device/shared").status_code == 404


class TestProfileEdit:
    """POST /users/{id}: update_user() already exists on AccessStore; the
    Produces list's "Edit name/email/role" line on GET /users/{id} implies
    this route even though it is not separately itemised — see
    task-12-report.md for the full reasoning."""

    def test_owner_edits_a_loaders_profile(self, login_as, wired):
        _cfg, _vmc, _inv, store = wired
        owner = login_as(Role.owner)
        target = store.create_user("Old Name", "old@example.com", Role.loader, "5297")
        resp = owner.post(
            f"/users/{target.id}",
            data={"name": "New Name", "email": "new@example.com", "role": "tech"},
        )
        assert resp.status_code == 200
        updated = store.get_user(target.id)
        assert updated.name == "New Name"
        assert updated.email == "new@example.com"
        assert updated.role == Role.tech

    def test_secretary_may_edit_a_non_owner(self, login_as, wired):
        _cfg, _vmc, _inv, store = wired
        secretary = login_as(Role.secretary)
        target = store.create_user("Old", "old@example.com", Role.loader, "5297")
        resp = secretary.post(
            f"/users/{target.id}",
            data={"name": "New", "email": "", "role": "loader"},
        )
        assert resp.status_code == 200
        assert store.get_user(target.id).name == "New"
