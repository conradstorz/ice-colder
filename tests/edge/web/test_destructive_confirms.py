"""Two-tap confirms for destructive actions (touch-review todo section 2).

Each confirm GET follows confirm_button.html's contract: absent or anything
but the literal "false" renders the Confirm/Cancel pair, "false" renders the
plain first-tap button. The destructive POST is never rendered on the first
tap, and the confirm GET carries the same permission gate (and guards) as the
POST it fronts.
"""

import pytest

from ice_edge.access.access import Role
from ice_edge.web import context


def _assert_pair(html: str, post_url: str, confirm_url: str) -> None:
    assert f'hx-post="{post_url}"' in html
    assert "Cancel" in html
    assert 'hx-vals=\'{"confirming": "false"}\'' in html
    assert f'hx-get="{confirm_url}"' in html
    # Cancel sits left of Confirm.
    assert html.index("Cancel") < html.index(f'hx-post="{post_url}"')


def _assert_plain(html: str, post_url: str, confirm_url: str) -> None:
    assert f'hx-post="{post_url}"' not in html
    assert f'hx-get="{confirm_url}"' in html
    assert "Cancel" not in html


class TestProductDeleteConfirm:
    def _make(self, client, sku="CF-1", name="Doomed Cola"):
        resp = client.post(
            "/products/new", data={"sku": sku, "name": name, "price": "1.00"}
        )
        assert resp.status_code == 200

    def test_detail_page_has_no_direct_delete_post(self, client, wired):
        self._make(client)
        html = client.get("/products/CF-1").text
        assert 'hx-post="/products/CF-1/delete"' not in html
        assert 'hx-get="/products/CF-1/delete/confirm"' in html

    def test_confirming_default_and_true_render_pair_with_name(self, client, wired):
        self._make(client)
        for url in (
            "/products/CF-1/delete/confirm",
            "/products/CF-1/delete/confirm?confirming=true",
        ):
            html = client.get(url).text
            _assert_pair(html, "/products/CF-1/delete", "/products/CF-1/delete/confirm")
            assert "Doomed Cola" in html
            assert "cannot be undone" in html

    def test_confirming_false_renders_plain_button(self, client, wired):
        self._make(client)
        html = client.get("/products/CF-1/delete/confirm?confirming=false").text
        _assert_plain(html, "/products/CF-1/delete", "/products/CF-1/delete/confirm")

    def test_unknown_sku_is_404(self, client, wired):
        assert client.get("/products/NOPE/delete/confirm").status_code == 404

    @pytest.mark.parametrize("role", [Role.secretary, Role.tech, Role.loader])
    def test_confirm_is_403_without_edit_catalog(self, login_as, wired, role):
        owner = login_as(Role.owner)
        self._make(owner)
        worker = login_as(role)
        expected = 200 if role is Role.secretary else 403
        # secretary holds edit_catalog; tech and loader do not.
        resp = worker.get("/products/CF-1/delete/confirm")
        assert resp.status_code == expected, role

    def test_post_still_needs_htmx_header(self, client, wired):
        cfg, _vmc, _inv, _store = wired
        self._make(client)
        resp = client.post("/products/CF-1/delete", headers={"HX-Request": ""})
        assert resp.status_code == 403
        assert any(p.sku == "CF-1" for p in cfg.products)


class TestUserDeleteConfirm:
    def _target(self, store):
        return store.create_user("Target Tina", "t@example.com", Role.loader, "5297")

    def test_detail_page_has_no_direct_delete_post(self, login_as, wired):
        _cfg, _vmc, _inv, store = wired
        owner = login_as(Role.owner)
        target = self._target(store)
        html = owner.get(f"/users/{target.id}").text
        assert f'hx-post="/users/{target.id}/delete"' not in html
        assert f'hx-get="/users/{target.id}/delete/confirm"' in html

    def test_confirming_values(self, login_as, wired):
        _cfg, _vmc, _inv, store = wired
        owner = login_as(Role.owner)
        target = self._target(store)
        post = f"/users/{target.id}/delete"
        confirm = f"{post}/confirm"
        html = owner.get(confirm).text
        _assert_pair(html, post, confirm)
        assert "Target Tina" in html and "lose access" in html
        _assert_pair(owner.get(f"{confirm}?confirming=true").text, post, confirm)
        _assert_plain(owner.get(f"{confirm}?confirming=false").text, post, confirm)

    def test_unknown_user_is_404(self, login_as, wired):
        owner = login_as(Role.owner)
        assert owner.get("/users/nope/delete/confirm").status_code == 404

    @pytest.mark.parametrize("role", [Role.tech, Role.loader])
    def test_confirm_is_403_without_manage_users(self, login_as, wired, role):
        _cfg, _vmc, _inv, store = wired
        target = self._target(store)
        worker = login_as(role)
        assert worker.get(f"/users/{target.id}/delete/confirm").status_code == 403

    def test_owner_cannot_be_confirmed_by_secretary(self, login_as, wired):
        _cfg, _vmc, _inv, store = wired
        secretary = login_as(Role.secretary)
        owner = store.owner()
        resp = secretary.get(f"/users/{owner.id}/delete/confirm")
        assert resp.status_code == 403

    def test_owner_cannot_confirm_deleting_self(self, login_as, wired):
        _cfg, _vmc, _inv, store = wired
        owner_client = login_as(Role.owner)
        owner = store.owner()
        resp = owner_client.get(f"/users/{owner.id}/delete/confirm")
        assert resp.status_code == 403


class TestRunAllConfirm:
    def test_confirming_values(self, client):
        html = client.get("/tests/run-all/confirm").text
        _assert_pair(html, "/tests/run-all", "/tests/run-all/confirm")
        assert "out of service" in html
        # The result lands in #run-all-result, not over the button.
        assert 'hx-target="#run-all-result"' in html
        assert 'hx-swap="innerHTML"' in html
        _assert_pair(
            client.get("/tests/run-all/confirm?confirming=true").text,
            "/tests/run-all",
            "/tests/run-all/confirm",
        )
        _assert_plain(
            client.get("/tests/run-all/confirm?confirming=false").text,
            "/tests/run-all",
            "/tests/run-all/confirm",
        )

    @pytest.mark.parametrize("role", [Role.secretary, Role.loader])
    def test_confirm_is_403_without_run_tests(self, login_as, role):
        assert login_as(role).get("/tests/run-all/confirm").status_code == 403

    def test_post_response_restores_first_tap_button_out_of_band(self, client, wired):
        resp = client.post("/tests/run-all")
        assert resp.status_code == 200
        assert 'hx-swap-oob="outerHTML"' in resp.text
        assert 'id="run-all-confirm"' in resp.text
        assert 'hx-get="/tests/run-all/confirm"' in resp.text


class TestTakeoverConfirm:
    def _hold_by_other(self):
        granted, reason = context.machine_instance.lease.begin_maintenance(
            "someone-else", "different-session"
        )
        assert granted, reason

    def test_confirming_values(self, client, wired):
        self._hold_by_other()
        html = client.get("/tests/takeover/confirm").text
        _assert_pair(html, "/tests/takeover", "/tests/takeover/confirm")
        assert "someone-else" in html and "becomes yours" in html
        _assert_pair(
            client.get("/tests/takeover/confirm?confirming=true").text,
            "/tests/takeover",
            "/tests/takeover/confirm",
        )
        _assert_plain(
            client.get("/tests/takeover/confirm?confirming=false").text,
            "/tests/takeover",
            "/tests/takeover/confirm",
        )

    @pytest.mark.parametrize("role", [Role.secretary, Role.loader])
    def test_confirm_is_403_without_run_tests(self, login_as, wired, role):
        assert login_as(role).get("/tests/takeover/confirm").status_code == 403

    def test_confirm_does_not_move_the_lease(self, client, wired):
        self._hold_by_other()
        client.get("/tests/takeover/confirm")
        assert (
            context.machine_instance.maintenance_hold.holder_session_id
            == "different-session"
        )

    def test_post_without_htmx_header_is_403(self, client, wired):
        self._hold_by_other()
        resp = client.post("/tests/takeover", headers={"HX-Request": ""})
        assert resp.status_code == 403
        assert (
            context.machine_instance.maintenance_hold.holder_session_id
            == "different-session"
        )


class TestControlsToneAndOrder:
    def test_restart_and_shutdown_first_tap_is_danger_toned(self, client):
        for cmd in ("restart", "shutdown"):
            html = client.get(f"/controls/confirm/{cmd}?confirming=false").text
            assert "text-red-700" in html, cmd
            assert "bg-slate-800" not in html, cmd

    def test_reset_first_tap_keeps_default_tone(self, client):
        html = client.get("/controls/confirm/reset?confirming=false").text
        assert "bg-slate-800" in html

    def test_confirm_pair_is_cancel_then_confirm_with_wide_gap(self, client):
        html = client.get("/controls/confirm/restart").text
        assert html.index("Cancel") < html.index("Restart Machine")
        assert "gap-4" in html


class TestVerdictGap:
    def test_pass_fail_gap_widened(self):
        from pathlib import Path

        import ice_ui

        root = Path(ice_ui.__file__).parent / "templates" / "partials"
        for name in ("test_result_card.html", "test_sale_result.html"):
            text = (root / name).read_text(encoding="utf-8")
            assert "flex gap-6" in text and "flex gap-2" not in text, name


class TestHxConfirmActions:
    def test_device_forget_asks_first(self, login_as, wired):
        _cfg, _vmc, _inv, store = wired
        owner = login_as(Role.owner)
        html = owner.get("/devices").text
        assert "/forget" in html
        assert 'hx-confirm="Forget ' in html

    def test_setup_codes_done_asks_first(self):
        from pathlib import Path

        import ice_ui

        text = (
            Path(ice_ui.__file__).parent / "templates" / "setup_codes.html"
        ).read_text(encoding="utf-8")
        assert "can never be shown again" in text
        assert 'hx-post="/setup/codes/done"' in text
        done = text[text.index('hx-post="/setup/codes/done"') :]
        assert "hx-confirm=" in done.split(">")[0]
