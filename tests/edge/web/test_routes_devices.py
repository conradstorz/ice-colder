"""Device levels: /devices, /devices/{id}/shared and /devices/{id}/forget.

Moved from tests/edge/web/test_web_routes.py (Task 6.2); the user-side
guards for the same routes stay in test_routes_users.py.
"""

import pytest

from ice_edge.access.access import Role
from tests.conftest import make_client
from ice_edge.web import auth as web_auth


class TestDeviceManagement:
    """§4.1: the device list is the second factor's revocation surface —
    forget must actually end the device's sessions, not just drop the row."""

    def test_owner_sees_a_device_row_naming_its_trusted_user(self, client):
        resp = client.get("/devices")
        assert resp.status_code == 200
        assert "Ada" in resp.text

    @pytest.mark.parametrize("role", [Role.tech, Role.loader])
    def test_tech_and_loader_get_403(self, login_as, role):
        worker = login_as(role)
        resp = worker.get("/devices")
        assert resp.status_code == 403

    def test_forget_removes_the_device_and_revokes_its_client(self, login_as, wired):
        _cfg, _vmc, _inv, store = wired
        owner = login_as(Role.owner)
        loader_client, loader = make_client(store, Role.loader, name="Loafer")
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
            # The forgotten device's own client is refused on its very next
            # request, not merely absent from the list.
            refused = loader_client.get("/status")
            assert refused.status_code == 401
            assert refused.headers["hx-redirect"] == "/login"

    def test_shared_toggle_flips_the_flag(self, login_as, wired):
        _cfg, _vmc, _inv, store = wired
        owner = login_as(Role.owner)
        device_id = next(iter(store.devices))
        assert store.devices[device_id].shared is False

        resp = owner.post(f"/devices/{device_id}/shared")
        assert resp.status_code == 200
        assert store.devices[device_id].shared is True

        resp = owner.post(f"/devices/{device_id}/shared")
        assert resp.status_code == 200
        assert store.devices[device_id].shared is False

    def test_shared_toggle_on_unknown_device_is_404(self, client):
        resp = client.post("/devices/no-such-device/shared")
        assert resp.status_code == 404

    def test_forget_unknown_device_is_404(self, client):
        resp = client.post("/devices/no-such-device/forget")
        assert resp.status_code == 404

    def test_secretary_forbidden_from_sharing_the_owners_device(self, login_as, wired):
        _cfg, _vmc, _inv, store = wired
        login_as(Role.owner)
        secretary = login_as(Role.secretary)
        device_id = next(
            d.id
            for d in store.devices.values()
            if store.owner().id in d.trusted_user_ids
        )
        before = store.devices[device_id].shared

        resp = secretary.post(f"/devices/{device_id}/shared")

        assert resp.status_code == 403
        assert store.devices[device_id].shared == before

    def test_secretary_may_still_manage_a_device_the_owner_is_not_on(
        self, login_as, wired
    ):
        """The guard must not over-restrict: managing ordinary staff
        devices is the secretary's actual job."""
        _cfg, _vmc, _inv, store = wired
        secretary = login_as(Role.secretary)
        loader_client, loader = make_client(store, Role.loader, name="Loafer4")
        with loader_client:
            device_id = next(
                d.id for d in store.devices.values() if loader.id in d.trusted_user_ids
            )

            resp = secretary.post(f"/devices/{device_id}/shared")
            assert resp.status_code == 200
            assert store.devices[device_id].shared is True

            resp = secretary.post(f"/devices/{device_id}/forget")
            assert resp.status_code == 200
            assert device_id not in store.devices

    def test_owner_may_still_forget_and_share_their_own_device(self, login_as, wired):
        _cfg, _vmc, _inv, store = wired
        owner = login_as(Role.owner)
        # A second device trusted on the owner, distinct from the one
        # `owner` is currently authenticated through.
        device, _token = store.create_device("Ada's spare", shared=False)
        store.trust_device(device.id, store.owner().id)

        resp = owner.post(f"/devices/{device.id}/shared")
        assert resp.status_code == 200
        assert store.devices[device.id].shared is True

        resp = owner.post(f"/devices/{device.id}/forget")
        assert resp.status_code == 200
        assert device.id not in store.devices

    def test_list_never_prints_a_raw_user_id_for_a_deleted_user(self, login_as, wired):
        _cfg, _vmc, _inv, store = wired
        owner = login_as(Role.owner)
        loader = store.create_user("Ghost", "ghost@example.com", Role.loader, "5297")
        device, _token = store.create_device("Ghost's device", shared=False)
        store.trust_device(device.id, loader.id)
        # Directly resurrect a dangling trusted id, bypassing delete_user's
        # own cleanup, to exercise the template's defensive path.
        ghost_id = loader.id
        del store.users[loader.id]
        store.devices[device.id].trusted_user_ids.append(ghost_id)

        resp = owner.get("/devices")

        assert resp.status_code == 200
        assert ghost_id not in resp.text
