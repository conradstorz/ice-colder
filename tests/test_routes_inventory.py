"""Tests for the Inventory restock level (web_interface/routes/inventory.py):
GET /inventory and POST /inventory/{sku}/adjust.

Owns the coverage GET /inventory lost from tests/test_web_routes.py when
task 8 moved it here (see that file's TestInventoryEndpoints and
TestDeleteAndEmptyState) — see .superpowers/sdd/part2/task-8-brief.md,
executor resolution 2, for the itemised arithmetic.
"""

import pytest

from services.access import Role
from services.config_store import add_product
from web_interface import context


def _add_tracked(wired, sku, *, name="Tracked Item", slot=1, count=5):
    """Add a product to config and register it as a tracked SKU at *count*."""
    cfg, _vmc, inv, _store = wired
    add_product(cfg, sku, name, 1.00, slot=slot, kind="other")
    inv.add_sku(sku, count, tracked=True)


def _add_untracked(wired, sku, *, name="Untracked Item", slot=2):
    """Add a product to config and register it as an untracked SKU."""
    cfg, _vmc, inv, _store = wired
    add_product(cfg, sku, name, 1.00, slot=slot, kind="other")
    inv.add_sku(sku, 0, tracked=False)


class TestInventoryListGating:
    """All four roles hold edit_placement (brief resolution 10) — there is
    no 403 case for the level itself."""

    @pytest.mark.parametrize(
        "role", [Role.owner, Role.secretary, Role.tech, Role.loader]
    )
    def test_inventory_list_is_200_for_every_role(self, login_as, wired, role):
        worker = login_as(role)
        resp = worker.get("/inventory")
        assert resp.status_code == 200


class TestInventoryRows:
    def test_tracked_product_shows_all_four_adjust_buttons_and_count(
        self, client, wired
    ):
        _add_tracked(wired, "TRK-1", name="Tracked Soda", count=7)
        resp = client.get("/inventory")
        assert resp.status_code == 200
        assert resp.text.count('hx-post="/inventory/TRK-1/adjust"') == 4
        assert ">7<" in resp.text

    def test_untracked_product_shows_no_adjust_buttons(self, client, wired):
        _add_untracked(wired, "UNT-1", name="Untracked Chips")
        resp = client.get("/inventory")
        assert resp.status_code == 200
        assert 'hx-post="/inventory/UNT-1/adjust"' not in resp.text

    def test_untracked_product_is_still_listed(self, client, wired):
        """Untracked products must be visible — so a loader can see what
        is not being counted — even though they carry no adjust
        affordance (brief resolution 8)."""
        _add_untracked(wired, "UNT-2", name="Visible But Untracked")
        resp = client.get("/inventory")
        assert resp.status_code == 200
        assert "Visible But Untracked" in resp.text
        assert 'hx-post="/inventory/UNT-2/adjust"' not in resp.text

    def test_slot_shown_for_both_tracked_and_untracked(self, client, wired):
        _add_tracked(wired, "TRK-2", name="Slotted Tracked", slot=4, count=1)
        _add_untracked(wired, "UNT-3", name="Slotted Untracked", slot=9)
        resp = client.get("/inventory")
        assert resp.status_code == 200
        assert 'value="4"' in resp.text
        assert 'value="9"' in resp.text

    def test_empty_state_shown_when_no_products(self, client):
        resp = client.get("/inventory")
        assert resp.status_code == 200
        assert "No products configured" in resp.text

    def test_inventory_view_tolerates_missing_inventory_manager(self, login_as, wired):
        """No InventoryManager wired -> treat nothing as tracked and
        render the page rather than failing (brief resolution 4)."""
        cfg, _vmc, _inv, _store = wired
        add_product(cfg, "NOMGR-1", "No Manager Item", 1.00, slot=1, kind="other")
        worker = login_as(Role.owner)
        context.set_inventory_manager(None)
        try:
            resp = worker.get("/inventory")
            assert resp.status_code == 200
            assert "No Manager Item" in resp.text
            assert 'hx-post="/inventory/NOMGR-1/adjust"' not in resp.text
        finally:
            _cfg, _vmc, inv, _store = wired
            context.set_inventory_manager(inv)


class TestInventoryAdjust:
    def test_plus_ten_raises_count(self, client, wired):
        _cfg, _vmc, inv, _store = wired
        _add_tracked(wired, "ADJ-1", count=5)
        resp = client.post("/inventory/ADJ-1/adjust", data={"delta": "10"})
        assert resp.status_code == 200
        assert inv.get_count("ADJ-1") == 15
        assert ">15<" in resp.text

    def test_minus_ten_on_count_of_three_clamps_at_zero(self, client, wired):
        _cfg, _vmc, inv, _store = wired
        _add_tracked(wired, "ADJ-2", count=3)
        resp = client.post("/inventory/ADJ-2/adjust", data={"delta": "-10"})
        assert resp.status_code == 200
        assert inv.get_count("ADJ-2") == 0
        assert ">0<" in resp.text

    def test_bad_delta_is_refused_with_400_and_leaves_count_unchanged(
        self, client, wired
    ):
        _cfg, _vmc, inv, _store = wired
        _add_tracked(wired, "ADJ-3", count=5)
        resp = client.post("/inventory/ADJ-3/adjust", data={"delta": "7"})
        assert resp.status_code == 400
        assert inv.get_count("ADJ-3") == 5

    def test_non_integer_delta_is_refused_with_400_and_leaves_count_unchanged(
        self, client, wired
    ):
        _cfg, _vmc, inv, _store = wired
        _add_tracked(wired, "ADJ-4", count=5)
        resp = client.post("/inventory/ADJ-4/adjust", data={"delta": "abc"})
        assert resp.status_code == 400
        assert inv.get_count("ADJ-4") == 5

    def test_adjust_without_hx_request_is_403(self, client, wired):
        _add_tracked(wired, "ADJ-5", count=5)
        resp = client.post(
            "/inventory/ADJ-5/adjust",
            headers={"HX-Request": ""},
            data={"delta": "1"},
        )
        assert resp.status_code == 403

    def test_adjust_response_is_single_row_not_full_list(self, client, wired):
        """A successful adjust must return only the re-rendered row for
        that SKU — returning the whole list would scroll-jump a tablet
        under a loader's repeated taps (brief resolution 7)."""
        _add_tracked(wired, "ADJ-6", name="Adjusted One", count=5)
        _add_tracked(wired, "ADJ-7", name="Untouched Other", count=2)
        resp = client.post("/inventory/ADJ-6/adjust", data={"delta": "1"})
        assert resp.status_code == 200
        assert "Adjusted One" in resp.text
        assert "Untouched Other" not in resp.text

    def test_adjust_unknown_sku_is_shell_404(self, client, wired):
        resp = client.post("/inventory/NOPE/adjust", data={"delta": "1"})
        assert resp.status_code == 404
