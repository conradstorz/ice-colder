"""Tests for the Users levels (ice_edge/web/routes/users/): /users,
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

import copy
import json
import re
import uuid

import pytest
from fastapi.testclient import TestClient

from ice_edge.access.access import Role
from ice_edge.web.server import app
from tests.conftest import make_client
from ice_edge.web import auth as web_auth

_CODE_VALUE_RE = re.compile(r'class="[^"]*\bcode-value\b[^"]*">(\d{8})<')


def _codes_in(html: str) -> list[str]:
    return _CODE_VALUE_RE.findall(html)


def _store_state(store) -> dict:
    """Everything a users/devices POST could mutate, read via the public
    surface: the user and device records (dataclass copies), the pending
    transfer, and the persisted emergency-code entries (read from the
    store's public ``path`` since the codes themselves are private)."""
    codes = json.loads(store.path.read_text(encoding="utf-8"))["emergency_codes"]
    return {
        "users": copy.deepcopy(store.users),
        "devices": copy.deepcopy(store.devices),
        "pending_transfer": copy.deepcopy(store.pending_transfer),
        "unused_codes": store.unused_emergency_code_count,
        "emergency_codes": codes,
    }


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
    def test_post_without_htmx_header_is_403(self, client, wired, path, data):
        _cfg, _vmc, _inv, store = wired
        store.generate_emergency_codes()
        before = _store_state(store)
        resp = client.post(path, headers={"HX-Request": ""}, data=data)
        assert resp.status_code == 403
        assert _store_state(store) == before

    def test_user_id_posts_without_htmx_header_are_403(self, login_as, wired):
        _cfg, _vmc, _inv, store = wired
        owner = login_as(Role.owner)
        target = store.create_user("T", "t@example.com", Role.loader, "5297")
        for path, data in [
            (f"/users/{target.id}/disable", {}),
            (f"/users/{target.id}/enable", {}),
            (f"/users/{target.id}/reset-pin", {"pin": "8642"}),
            (f"/users/{target.id}/delete", {}),
        ]:
            before = _store_state(store)
            resp = owner.post(path, headers={"HX-Request": ""}, data=data)
            assert resp.status_code == 403
            assert _store_state(store) == before
        # The target survived every attempt, still enabled, PIN untouched.
        survivor = store.get_user(target.id)
        assert survivor is not None
        assert survivor.disabled is False
        assert store.verify_user_pin(target.id, "5297")

    def test_enable_without_htmx_header_leaves_disabled_user_disabled(
        self, login_as, wired
    ):
        _cfg, _vmc, _inv, store = wired
        owner = login_as(Role.owner)
        target = store.create_user("T", "t@example.com", Role.loader, "5297")
        store.set_user_disabled(target.id, True)
        before = _store_state(store)
        resp = owner.post(
            f"/users/{target.id}/enable", headers={"HX-Request": ""}, data={}
        )
        assert resp.status_code == 403
        assert _store_state(store) == before
        assert store.get_user(target.id).disabled is True

    def test_device_posts_without_htmx_header_are_403(self, client, wired):
        _cfg, _vmc, _inv, store = wired
        device_id = next(iter(store.devices))
        before = _store_state(store)
        shared_resp = client.post(
            f"/devices/{device_id}/shared", headers={"HX-Request": ""}
        )
        assert shared_resp.status_code == 403
        assert _store_state(store) == before
        forget_resp = client.post(
            f"/devices/{device_id}/forget", headers={"HX-Request": ""}
        )
        assert forget_resp.status_code == 403
        assert device_id in store.devices
        assert _store_state(store) == before


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
        [
            ("disable", {}),
            ("enable", {}),
            ("delete", {}),
            ("reset-pin", {"pin": "5297"}),
        ],
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
        assert store.owner().id in store.devices[device_id].trusted_user_ids
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

    @pytest.mark.parametrize("new_role", ["secretary", "tech"])
    def test_owner_cannot_demote_themselves(self, login_as, wired, new_role):
        """Critical defect (task-12 review): POST /users/{owner.id} with a
        role other than owner used to succeed, leaving the store finalized
        but ownerless (store.owner() is None, no one holds
        manage_ownership — emergency codes, transfer and owner-targeted
        writes become permanently unreachable, with no software recovery
        per part 1's design). _guard_owner_self_demotion refuses this in
        the route layer, mirroring _guard_owner_self_lockout. Assert the
        store state, not just the status code — a 403 that still wrote
        would pass a status-only test."""
        _cfg, _vmc, _inv, store = wired
        owner = login_as(Role.owner)
        owner_user = store.owner()
        resp = owner.post(
            f"/users/{owner_user.id}",
            data={
                "name": owner_user.name,
                "email": owner_user.email or "",
                "role": new_role,
            },
        )
        assert resp.status_code == 403
        assert store.owner() is not None
        assert store.owner().id == owner_user.id
        assert store.get_user(owner_user.id).role == Role.owner

    def test_owner_may_still_edit_their_own_name_and_email(self, login_as, wired):
        """The fix must not over-reach: a name/email edit that leaves the
        owner's own role unchanged (the only role the shell's hidden
        <input> ever submits for the owner's own row) still goes
        through."""
        _cfg, _vmc, _inv, store = wired
        owner = login_as(Role.owner)
        owner_user = store.owner()
        resp = owner.post(
            f"/users/{owner_user.id}",
            data={
                "name": "New Owner Name",
                "email": "new-owner@example.com",
                "role": "owner",
            },
        )
        assert resp.status_code == 200
        updated = store.get_user(owner_user.id)
        assert updated.name == "New Owner Name"
        assert updated.email == "new-owner@example.com"
        assert updated.role == Role.owner
        assert store.owner().id == owner_user.id

    def test_secretary_still_refused_on_owners_row(self, login_as, wired):
        """_guard_owner_target's existing behavior (a non-owner can never
        write to the owner's row at all) must be unaffected by the new
        guard."""
        _cfg, _vmc, _inv, store = wired
        secretary = login_as(Role.secretary)
        owner_user = store.owner()
        resp = secretary.post(
            f"/users/{owner_user.id}",
            data={
                "name": owner_user.name,
                "email": owner_user.email or "",
                "role": "secretary",
            },
        )
        assert resp.status_code == 403
        assert store.owner() is not None
        assert store.owner().id == owner_user.id
        assert store.get_user(owner_user.id).role == Role.owner


class TestLinkToCloud:
    """POST /users/ownership/link mints a cloud claim code (owner PIN-gated)."""

    @pytest.fixture
    def cloud_on(self, wired, tmp_path):
        from ice_edge.uplink.identity import MachineIdentity
        from ice_edge.web import context

        cfg, _vmc, _inv, _store = wired
        cfg.cloud.enabled = True
        identity = MachineIdentity.load_or_create(tmp_path / "identity.json")
        context.set_machine_identity(identity)
        return identity

    def test_link_requires_manage_ownership(self, login_as, wired, cloud_on):
        _cfg, _vmc, _inv, store = wired
        tech = login_as(Role.tech)
        resp = tech.post("/users/ownership/link", data={"pin": "1379"})
        assert resp.status_code == 403
        assert store.pending_link is None

    def test_link_wrong_pin_no_code(self, client, wired, cloud_on):
        _cfg, _vmc, _inv, store = wired
        resp = client.post("/users/ownership/link", data={"pin": "0000"})
        assert resp.status_code == 200
        assert "Wrong PIN" in resp.text
        assert _codes_in(resp.text) == []
        assert store.pending_link is None

    def test_link_shows_code_once_with_no_store(self, client, wired, cloud_on):
        _cfg, _vmc, _inv, store = wired
        resp = client.post("/users/ownership/link", data={"pin": "1379"})
        assert resp.status_code == 200
        assert resp.headers["cache-control"] == "no-store"
        assert len(_codes_in(resp.text)) == 1
        assert cloud_on.short_fingerprint in resp.text
        assert store.pending_link is not None
        reload_resp = client.get("/users/ownership")
        assert _codes_in(reload_resp.text) == []
        assert "Cancel link" in reload_resp.text
        assert cloud_on.short_fingerprint in reload_resp.text

    def test_link_refused_while_transfer_pending(self, client, wired, cloud_on):
        _cfg, _vmc, _inv, store = wired
        codes = store.generate_emergency_codes()
        client.post("/users/transfer", data={"pin": "1379", "emergency_code": codes[0]})
        assert store.pending_transfer is not None
        resp = client.post("/users/ownership/link", data={"pin": "1379"})
        assert "transfer is pending" in resp.text.lower()
        assert _codes_in(resp.text) == []
        assert store.pending_link is None

    def test_link_cancel(self, client, wired, cloud_on):
        _cfg, _vmc, _inv, store = wired
        client.post("/users/ownership/link", data={"pin": "1379"})
        assert store.pending_link is not None
        resp = client.post("/users/ownership/link/cancel", data={"pin": "1379"})
        assert resp.status_code == 200
        assert store.pending_link is None

    def test_link_cancel_wrong_pin_keeps_link(self, client, wired, cloud_on):
        _cfg, _vmc, _inv, store = wired
        client.post("/users/ownership/link", data={"pin": "1379"})
        bad = client.post("/users/ownership/link/cancel", data={"pin": "0000"})
        assert "Wrong PIN" in bad.text
        assert store.pending_link is not None

    def test_link_card_hidden_when_cloud_disabled(self, client, wired):
        cfg, _vmc, _inv, _store = wired
        assert cfg.cloud.enabled is False
        resp = client.get("/users/ownership")
        assert "Link to cloud" not in resp.text

    def test_link_card_shown_when_cloud_enabled(self, client, wired, cloud_on):
        resp = client.get("/users/ownership")
        assert "Link to cloud" in resp.text
        assert cloud_on.short_fingerprint in resp.text

    def test_health_shows_short_fingerprint_when_identity_set(
        self, client, wired, cloud_on
    ):
        from ice_edge.monitoring.health_monitor import HealthMonitor
        from ice_edge.web import context

        context.set_health_monitor(HealthMonitor())
        resp = client.get("/health")
        assert cloud_on.short_fingerprint in resp.text


class TestUserManagement:
    """§4.1: a secretary manages everyone except the owner — every write
    whose target is the owner needs manage_ownership, and a secretary may
    never mint one either."""

    def test_owner_sees_the_list_containing_ada(self, client):
        resp = client.get("/users")
        assert resp.status_code == 200
        assert "Ada" in resp.text

    def test_list_shows_the_unused_code_count(self, client, wired):
        _cfg, _vmc, _inv, store = wired
        store.generate_emergency_codes()
        resp = client.get("/users")
        assert resp.status_code == 200
        assert "20" in resp.text

    @pytest.mark.parametrize("role", [Role.tech, Role.loader])
    def test_tech_and_loader_get_403(self, login_as, role):
        worker = login_as(role)
        resp = worker.get("/users")
        assert resp.status_code == 403

    def test_owner_creates_a_loader(self, client, wired):
        _cfg, _vmc, _inv, store = wired
        resp = client.post(
            "/users/new",
            data={"name": "Lonnie", "email": "", "role": "loader", "pin": "5297"},
        )
        assert resp.status_code == 200
        assert "Lonnie" in resp.text
        created = next(u for u in store.users.values() if u.name == "Lonnie")
        assert created.role == Role.loader

    def test_bad_pin_is_refused_with_the_reason_and_creates_nobody(self, client, wired):
        _cfg, _vmc, _inv, store = wired
        before = len(store.users)
        resp = client.post(
            "/users/new",
            data={"name": "Nope", "email": "", "role": "loader", "pin": "1111"},
        )
        assert resp.status_code == 200
        assert "same digit repeated" in resp.text
        assert len(store.users) == before

    def test_secretary_may_not_create_an_owner(self, login_as, wired):
        _cfg, _vmc, _inv, store = wired
        secretary = login_as(Role.secretary)
        original_owner_id = store.owner().id
        resp = secretary.post(
            "/users/new",
            data={"name": "Usurper", "email": "", "role": "owner", "pin": "5297"},
        )
        assert resp.status_code == 403
        assert store.owner().id == original_owner_id
        assert not any(u.name == "Usurper" for u in store.users.values())

    def test_new_user_form_offers_owner_only_to_manage_ownership(self, login_as):
        owner = login_as(Role.owner)
        secretary = login_as(Role.secretary)
        assert 'value="owner"' in owner.get("/users/new").text
        assert 'value="owner"' not in secretary.get("/users/new").text

    def test_secretary_may_disable_and_reenable_a_loader(self, login_as, wired):
        _cfg, _vmc, _inv, store = wired
        secretary = login_as(Role.secretary)
        loader = store.create_user(
            "Loader One", "loader1@example.com", Role.loader, "5297"
        )
        resp = secretary.post(f"/users/{loader.id}/disable")
        assert resp.status_code == 200
        assert store.get_user(loader.id).disabled is True
        resp = secretary.post(f"/users/{loader.id}/enable")
        assert resp.status_code == 200
        assert store.get_user(loader.id).disabled is False

    def test_disable_ends_the_users_live_sessions(self, login_as, wired):
        _cfg, _vmc, _inv, store = wired
        owner = login_as(Role.owner)
        loader_client, loader = make_client(store, Role.loader, name="Loafer")
        with loader_client:
            session_id = loader_client.cookies.get(web_auth.SESSION_COOKIE)
            assert store.resolve_session(session_id) is not None
            resp = owner.post(f"/users/{loader.id}/disable")
            assert resp.status_code == 200
            assert store.resolve_session(session_id) is None

    def test_delete_ends_the_users_live_sessions(self, login_as, wired):
        _cfg, _vmc, _inv, store = wired
        owner = login_as(Role.owner)
        loader_client, loader = make_client(store, Role.loader, name="Loafer3")
        with loader_client:
            session_id = loader_client.cookies.get(web_auth.SESSION_COOKIE)
            resp = owner.post(f"/users/{loader.id}/delete")
            assert resp.status_code == 200
            assert store.resolve_session(session_id) is None

    def test_reset_pin_ends_the_users_live_sessions(self, login_as, wired):
        """set_user_pin only drops device *trust*; resolve_session() doesn't
        re-check it, so without ending sessions here the old session would
        keep working until it idles out — contradicting the spec's "the
        next login re-enrolls" (Copilot review, ice_edge/web/routes.py:1493).
        """
        _cfg, _vmc, _inv, store = wired
        owner = login_as(Role.owner)
        loader_client, loader = make_client(store, Role.loader, name="Loafer4")
        with loader_client:
            session_id = loader_client.cookies.get(web_auth.SESSION_COOKIE)
            assert store.resolve_session(session_id) is not None
            resp = owner.post(f"/users/{loader.id}/reset-pin", data={"pin": "5297"})
            assert resp.status_code == 200
            assert store.resolve_session(session_id) is None

    def test_delete_removes_the_user(self, login_as, wired):
        _cfg, _vmc, _inv, store = wired
        owner = login_as(Role.owner)
        loader = store.create_user("Gone", "gone@example.com", Role.loader, "5297")
        resp = owner.post(f"/users/{loader.id}/delete")
        assert resp.status_code == 200
        assert store.get_user(loader.id) is None


class TestOwnershipTransfer:
    """§3.3 step 1: starting a transfer is owner-PIN + one emergency code,
    checked before anything is consumed, and it must change nothing about
    the current owner's own standing."""

    def test_start_shows_code_records_pending_and_consumes_one_emergency_code(
        self, client, wired
    ):
        _cfg, _vmc, _inv, store = wired
        codes = store.generate_emergency_codes()
        resp = client.post(
            "/users/transfer", data={"pin": "1379", "emergency_code": codes[0]}
        )
        assert resp.status_code == 200
        found = _codes_in(resp.text)
        assert len(found) == 1
        assert store.pending_transfer is not None
        assert store.unused_emergency_code_count() == 19

    def test_transfer_code_survives_a_colliding_user_uuid(
        self, client, wired, monkeypatch
    ):
        """Regression test for Task 19d (the code-value hook): a uuid4's
        first hyphen-delimited group is occasionally eight decimal digits
        (~2.3% of uuids — see the module docstring above `_codes_in`).

        Retargeted for the v2 shell (task-12 brief resolution 3): part 1's
        version asserted the collision was really on the page, because its
        single users-list partial rendered the transfer code and every
        user's id (in hx-post targets and a device-count lookup) in the
        very same response. Task 12 splits that into separate pages —
        POST /users/transfer now renders only users_ownership.html, which
        contains no user ids at all — so that page-wide-scrape hazard the
        original regression targeted cannot occur here regardless of any
        uuid collision. The guarantee this test still owns (the code-value
        hook returns the real transfer code, not a decoy) is unaffected and
        stays covered by the assertions below and by
        tests/test_routes_users.py's own no-store/twenty-codes coverage.
        """
        _cfg, _vmc, _inv, store = wired
        collider = uuid.UUID("12345678-abcd-4abc-8abc-abcdefabcdef")
        monkeypatch.setattr("ice_edge.access.access.uuid.uuid4", lambda: collider)
        colliding_user = store.create_user(
            "Collider", "collider@example.com", Role.tech, "1111"
        )
        monkeypatch.undo()
        assert colliding_user.id == str(collider)

        codes = store.generate_emergency_codes()
        resp = client.post(
            "/users/transfer", data={"pin": "1379", "emergency_code": codes[0]}
        )
        assert resp.status_code == 200

        found = _codes_in(resp.text)
        assert len(found) == 1
        assert store.verify_transfer_code(found[0]) is True

    def test_second_start_while_pending_is_refused_and_consumes_nothing(
        self, client, wired
    ):
        """A re-entrant /users/transfer while one is already pending must be
        refused before checking or consuming anything: no second emergency
        code spent, the existing pending_transfer left alone, and — the
        real-world stake — the transfer code already handed to the incoming
        owner must still work."""
        _cfg, _vmc, _inv, store = wired
        codes = store.generate_emergency_codes()
        first = client.post(
            "/users/transfer", data={"pin": "1379", "emergency_code": codes[0]}
        )
        first_transfer_code = _codes_in(first.text)[0]
        pending_before = dict(store.pending_transfer)
        unused_before = store.unused_emergency_code_count()

        resp = client.post(
            "/users/transfer", data={"pin": "1379", "emergency_code": codes[1]}
        )
        assert resp.status_code == 200
        assert "already pending" in resp.text.lower()
        assert store.unused_emergency_code_count() == unused_before
        assert store.pending_transfer == pending_before
        assert store.verify_transfer_code(first_transfer_code) is True

    def test_started_transfer_records_used_for_transfer(self, client, wired):
        _cfg, _vmc, _inv, store = wired
        codes = store.generate_emergency_codes()
        client.post("/users/transfer", data={"pin": "1379", "emergency_code": codes[0]})
        codes_on_disk = json.loads(store.path.read_text(encoding="utf-8"))[
            "emergency_codes"
        ]
        used = next(e for e in codes_on_disk if e["used_at"] is not None)
        assert used["used_for"] == "transfer"

    def test_owner_keeps_full_control_while_transfer_is_pending(self, client, wired):
        _cfg, _vmc, _inv, store = wired
        codes = store.generate_emergency_codes()
        client.post("/users/transfer", data={"pin": "1379", "emergency_code": codes[0]})
        owner = store.owner()
        assert owner is not None
        assert owner.name == "Ada"
        # The old owner's session still works for an ordinary page.
        resp = client.get("/status")
        assert resp.status_code == 200
        # ... and still reaches the Users area with owner permissions.
        resp = client.get("/users")
        assert resp.status_code == 200

    def test_wrong_pin_starts_nothing_and_consumes_no_code(self, client, wired):
        _cfg, _vmc, _inv, store = wired
        codes = store.generate_emergency_codes()
        resp = client.post(
            "/users/transfer", data={"pin": "0000", "emergency_code": codes[0]}
        )
        assert resp.status_code == 200
        assert store.pending_transfer is None
        assert store.unused_emergency_code_count() == 20

    def test_wrong_emergency_code_starts_nothing(self, client, wired):
        _cfg, _vmc, _inv, store = wired
        store.generate_emergency_codes()
        resp = client.post(
            "/users/transfer", data={"pin": "1379", "emergency_code": "00000000"}
        )
        assert resp.status_code == 200
        assert store.pending_transfer is None
        assert store.unused_emergency_code_count() == 20

    def test_cancel_clears_pending_transfer_and_leaves_owner_in_place(
        self, client, wired
    ):
        _cfg, _vmc, _inv, store = wired
        codes = store.generate_emergency_codes()
        client.post("/users/transfer", data={"pin": "1379", "emergency_code": codes[0]})
        assert store.pending_transfer is not None
        resp = client.post("/users/transfer/cancel", data={"pin": "1379"})
        assert resp.status_code == 200
        assert store.pending_transfer is None
        assert store.owner().name == "Ada"

    def test_cancel_with_wrong_pin_leaves_transfer_pending(self, client, wired):
        _cfg, _vmc, _inv, store = wired
        codes = store.generate_emergency_codes()
        client.post("/users/transfer", data={"pin": "1379", "emergency_code": codes[0]})
        resp = client.post("/users/transfer/cancel", data={"pin": "0000"})
        assert resp.status_code == 200
        assert store.pending_transfer is not None

    def test_secretary_gets_403_on_start(self, login_as, wired):
        _cfg, _vmc, _inv, store = wired
        codes = store.generate_emergency_codes()
        secretary = login_as(Role.secretary)
        resp = secretary.post(
            "/users/transfer", data={"pin": "2468", "emergency_code": codes[0]}
        )
        assert resp.status_code == 403
        assert store.pending_transfer is None
        assert store.unused_emergency_code_count() == 20

    def test_secretary_gets_403_on_cancel(self, client, login_as, wired):
        _cfg, _vmc, _inv, store = wired
        codes = store.generate_emergency_codes()
        client.post("/users/transfer", data={"pin": "1379", "emergency_code": codes[0]})
        secretary = login_as(Role.secretary)
        resp = secretary.post("/users/transfer/cancel", data={"pin": "2468"})
        assert resp.status_code == 403
        assert store.pending_transfer is not None


class TestOwnershipTransferCompletion:
    """§3.3 steps 2-4: the incoming owner completes the wizard with the
    transfer code, then reviews the retained users."""

    @pytest.fixture(autouse=True)
    def _anon_clients(self):
        """Every client `_anon()` hands out is tracked here and closed on
        teardown, whatever the test does — mirroring how the module-level
        `login_as` fixture closes every client it makes. A bare TestClient
        closed only by a trailing `.close()` as the test's last statement
        leaks when the test raises before reaching it, and a leaked client
        left the suite order-dependent."""
        self._clients: list[TestClient] = []
        yield
        for c in self._clients:
            c.close()

    def _anon(self) -> TestClient:
        c = TestClient(app, follow_redirects=False)
        c.headers["HX-Request"] = "true"
        self._clients.append(c)
        return c

    def _start_transfer(self, client, store) -> str:
        codes = store.generate_emergency_codes()
        resp = client.post(
            "/users/transfer", data={"pin": "1379", "emergency_code": codes[0]}
        )
        return _codes_in(resp.text)[0]

    def _submit_transfer_form(self, anon, code, *, name="Bea", pin="9042"):
        return anon.post(
            "/setup",
            data={
                "setup_code": code,
                "name": name,
                "email": f"{name.lower()}@example.com",
                "pin": pin,
                "pin_confirm": pin,
            },
        )

    def _complete_transfer(self, client, store) -> TestClient:
        """Complete a transfer end to end, returning a client signed in as
        the new owner (positioned right after the redirect to /setup/review)."""
        code = self._start_transfer(client, store)
        anon = self._anon()
        anon.get("/setup")
        self._submit_transfer_form(anon, code)
        return anon

    def test_setup_is_reachable_while_a_transfer_is_pending(self, client, wired):
        _cfg, _vmc, _inv, store = wired
        self._start_transfer(client, store)
        anon = self._anon()
        resp = anon.get("/setup")
        assert resp.status_code == 200
        assert "transfer code" in resp.text.lower()

    def test_other_routes_are_not_redirected_while_a_transfer_is_pending(
        self, client, wired
    ):
        _cfg, _vmc, _inv, store = wired
        self._start_transfer(client, store)
        resp = client.get("/status")
        assert resp.status_code == 200
        resp = client.get("/users")
        assert resp.status_code == 200

    def test_wrong_transfer_code_changes_nothing(self, client, wired):
        _cfg, _vmc, _inv, store = wired
        self._start_transfer(client, store)
        pending_before = dict(store.pending_transfer)
        anon = self._anon()
        anon.get("/setup")
        resp = self._submit_transfer_form(anon, "00000000")
        assert resp.status_code == 200
        assert store.pending_transfer == pending_before
        assert store.owner().name == "Ada"
        assert len(store.users) == 1

    def test_transfer_code_completes_the_swap(self, client, wired):
        _cfg, _vmc, _inv, store = wired
        old_owner = store.owner()
        code = self._start_transfer(client, store)
        anon = self._anon()
        anon.get("/setup")
        resp = self._submit_transfer_form(anon, code)
        assert resp.headers["hx-redirect"] == "/setup/review"

        new_owner = store.owner()
        assert new_owner is not None
        assert new_owner.name == "Bea"
        assert store.get_user(old_owner.id) is None
        assert store.pending_transfer is None
        assert store.unused_emergency_code_count() == 0

    def test_old_owners_session_is_dead_after_the_swap(self, client, wired):
        _cfg, _vmc, _inv, store = wired
        code = self._start_transfer(client, store)
        anon = self._anon()
        anon.get("/setup")
        self._submit_transfer_form(anon, code)

        resp = client.get("/status")
        assert resp.status_code == 401
        assert resp.headers["hx-redirect"] == "/login"

    def test_transfer_code_still_enrolls_new_owner_on_a_second_browser(
        self, client, wired
    ):
        """A lost step-2 response cannot strand the incoming owner: the
        transfer code still enrolls them, exactly as the setup code does
        for step 1 (spec §3.3 step 4)."""
        _cfg, _vmc, _inv, store = wired
        code = self._start_transfer(client, store)
        first = self._anon()
        first.get("/setup")
        self._submit_transfer_form(first, code)
        new_owner = store.owner()

        second = self._anon()
        login_resp = second.post(
            "/login", data={"user_id": new_owner.id, "pin": "9042"}
        )
        assert login_resp.status_code == 200
        assert second.cookies.get("vmc_enroll")

        enroll_resp = second.post("/login/enroll", data={"code": code})
        assert enroll_resp.headers["hx-redirect"] == "/"
        assert second.cookies.get("vmc_session")

    def test_review_walks_retained_users_and_removing_the_last_one_redirects_on(
        self, client, wired
    ):
        _cfg, _vmc, _inv, store = wired
        lee = store.create_user("Lee", "lee@example.com", Role.loader, "7890")
        tim = store.create_user("Tim", "tim@example.com", Role.tech, "3456")
        new_owner_client = self._complete_transfer(client, store)

        resp = new_owner_client.get("/setup/review")
        assert resp.status_code == 200
        assert "Lee" in resp.text

        resp2 = new_owner_client.post(f"/setup/review/{lee.id}/keep")
        assert resp2.status_code == 200
        assert "Tim" in resp2.text
        assert store.get_user(lee.id) is not None

        resp3 = new_owner_client.post(f"/setup/review/{tim.id}/remove")
        assert resp3.headers["hx-redirect"] == "/setup/codes"
        assert store.get_user(tim.id) is None

    def test_keep_response_has_exactly_one_bar_and_main(self, client, wired):
        """Task 14 fix round 1, finding 1: POST /setup/review/{id}/keep
        re-renders the full setup_review_user.html page for the next
        candidate, so that response must still carry exactly one shell
        `<header id="bar">` and exactly one `<main>` — never zero (the OOB
        header dropped) and never two (a nested `<main>`).

        The header/main counts alone can't fail on a reversion (see
        TestEnrollment's twin of this test for why) — this also pins the
        Keep/Remove buttons' own hx attributes directly.
        """
        _cfg, _vmc, _inv, store = wired
        lee = store.create_user("Lee", "lee@example.com", Role.loader, "7890")
        store.create_user("Tim", "tim@example.com", Role.tech, "3456")
        new_owner_client = self._complete_transfer(client, store)

        resp = new_owner_client.post(f"/setup/review/{lee.id}/keep")
        assert resp.status_code == 200
        assert resp.text.count('<header id="bar"') == 1
        assert len(re.findall(r"<main[ >]", resp.text)) == 1
        assert 'hx-target="body"' not in resp.text
        assert 'hx-select="main"' in resp.text
        assert store.get_user(lee.id) is not None

    def test_review_with_nobody_left_redirects_straight_to_codes(self, client, wired):
        _cfg, _vmc, _inv, store = wired
        new_owner_client = self._complete_transfer(client, store)
        resp = new_owner_client.get("/setup/review")
        assert resp.status_code == 303
        assert resp.headers["location"] == "/setup/codes"

    def test_keeping_a_user_leaves_them_in_place(self, client, wired):
        _cfg, _vmc, _inv, store = wired
        tim = store.create_user("Tim", "tim@example.com", Role.tech, "3456")
        new_owner_client = self._complete_transfer(client, store)
        resp = new_owner_client.post(f"/setup/review/{tim.id}/keep")
        assert resp.headers["hx-redirect"] == "/setup/codes"
        assert store.get_user(tim.id) is not None

    def test_removing_a_user_ends_their_sessions_first(self, client, wired):
        _cfg, _vmc, _inv, store = wired
        tim = store.create_user("Tim", "tim@example.com", Role.tech, "3456")
        new_owner_client = self._complete_transfer(client, store)
        # Tim signs back in after the swap (which already cleared every
        # session), so there is a live session to check removal against.
        device, _token = store.create_device("Tim's tablet", shared=False)
        store.trust_device(device.id, tim.id)
        session_id = store.create_session(tim.id, device.id)

        resp = new_owner_client.post(f"/setup/review/{tim.id}/remove")
        assert resp.headers["hx-redirect"] == "/setup/codes"
        assert store.resolve_session(session_id) is None
        assert store.get_user(tim.id) is None


class TestEmergencyCodeRegeneration:
    """§3.2: the owner can replace the whole pool at any time from the
    Users area."""

    def test_regenerate_replaces_pool_and_shows_twenty_new_codes(self, client, wired):
        _cfg, _vmc, _inv, store = wired
        old_codes = store.generate_emergency_codes()
        resp = client.post("/users/codes/regenerate", data={"pin": "1379"})
        assert resp.status_code == 200
        found = _codes_in(resp.text)
        assert len(found) == 20
        assert set(found).isdisjoint(old_codes)
        assert store.unused_emergency_code_count() == 20
        # The old codes no longer work.
        for code in old_codes:
            assert store.consume_emergency_code(code, store.owner().id, "test") is False

    def test_regenerate_with_wrong_pin_changes_nothing(self, client, wired):
        _cfg, _vmc, _inv, store = wired
        old_codes = store.generate_emergency_codes()
        resp = client.post("/users/codes/regenerate", data={"pin": "0000"})
        assert resp.status_code == 200
        found = _codes_in(resp.text)
        assert found == []
        # Every original code still works.
        for code in old_codes:
            assert store.consume_emergency_code(code, store.owner().id, "test") is True

    def test_secretary_gets_403_on_regenerate(self, login_as, wired):
        _cfg, _vmc, _inv, store = wired
        old_codes = store.generate_emergency_codes()
        secretary = login_as(Role.secretary)
        resp = secretary.post("/users/codes/regenerate", data={"pin": "2468"})
        assert resp.status_code == 403
        for code in old_codes:
            assert store.consume_emergency_code(code, store.owner().id, "test") is True

    def test_regenerate_response_is_marked_no_store(self, client, wired):
        """The regenerated pool is plaintext, shown once — Copilot review,
        ice_edge/web/routes.py:647, extended to every response that renders
        plaintext codes, not just GET /setup/codes."""
        _cfg, _vmc, _inv, store = wired
        store.generate_emergency_codes()
        resp = client.post("/users/codes/regenerate", data={"pin": "1379"})
        assert resp.headers["cache-control"] == "no-store"
