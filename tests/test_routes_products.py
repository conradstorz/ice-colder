"""Tests for the Products levels (web_interface/routes/products.py):
/products, /products/new, /products/{sku}, /products/{sku}/catalog,
/products/{sku}/placement, /products/{sku}/copy, /products/{sku}/delete.

Replaces the old /inventory/* catalog and placement forms covered by
tests/test_web_routes.py, which keeps its own coverage of those routes
until Task 15 removes them (see .superpowers/sdd/part2/task-7-brief.md,
executor resolution 1) — this file owns only the new /products/* routes.
"""

import pytest

from services.access import Role
from web_interface import context


def _add(client, sku, name="Item", price="1.00", **extra):
    """POST /products/new with sensible defaults, returning the response."""
    data = {"sku": sku, "name": name, "price": price}
    data.update(extra)
    return client.post("/products/new", data=data)


class TestProductsListGating:
    """GET /products: gate is edit_catalog OR edit_placement (brief
    interface), so every one of the four roles reaches it; the Add button
    is edit_catalog-only."""

    @pytest.mark.parametrize(
        "role", [Role.owner, Role.secretary, Role.tech, Role.loader]
    )
    def test_products_list_is_200_for_every_role(self, login_as, wired, role):
        worker = login_as(role)
        resp = worker.get("/products")
        assert resp.status_code == 200

    @pytest.mark.parametrize(
        "role,expect_add_button",
        [
            (Role.owner, True),
            (Role.secretary, True),
            (Role.tech, False),
            (Role.loader, False),
        ],
    )
    def test_add_button_only_for_edit_catalog_roles(
        self, login_as, wired, role, expect_add_button
    ):
        worker = login_as(role)
        resp = worker.get("/products")
        assert resp.status_code == 200
        assert ('href="/products/new"' in resp.text) == expect_add_button

    def test_empty_state_shown_when_no_products(self, client):
        resp = client.get("/products")
        assert resp.status_code == 200
        assert "No products configured" in resp.text


class TestProductDetailReadOnlyRule:
    """The point of this task (brief resolution 4): a loader/tech holds
    edit_placement but not edit_catalog. GET /products/{sku} must show
    name, price and kind as visible read-only text — not hidden, not an
    <input> — while the Catalog sub-tile's URL is entirely absent from the
    page (partials/tile.html only renders a tile when tile.enabled)."""

    @pytest.mark.parametrize(
        "role", [Role.owner, Role.secretary, Role.tech, Role.loader]
    )
    def test_product_detail_is_200_for_every_role(self, login_as, wired, role):
        owner = login_as(Role.owner)
        _add(owner, f"DET-{role.value}", name="Detail Item", price="3.25")
        worker = login_as(role)
        resp = worker.get(f"/products/DET-{role.value}")
        assert resp.status_code == 200

    def test_loader_sees_price_as_text_not_input(self, login_as, wired):
        owner = login_as(Role.owner)
        _add(owner, "RO-1", name="Read Only Item", price="3.25", kind="water")
        loader = login_as(Role.loader)
        resp = loader.get("/products/RO-1")
        assert resp.status_code == 200
        # Visible, not hidden:
        assert "Read Only Item" in resp.text
        assert "3.25" in resp.text
        assert "water" in resp.text
        # Not inside an <input> — the whole read-only summary page renders
        # no <input> element at all, unlike the Catalog/Placement forms.
        assert "<input" not in resp.text
        assert 'name="price"' not in resp.text

    def test_catalog_tile_url_absent_for_loader_placement_tile_present(
        self, login_as, wired
    ):
        owner = login_as(Role.owner)
        _add(owner, "TILE-1", name="Tile Item", price="1.00")
        loader = login_as(Role.loader)
        resp = loader.get("/products/TILE-1")
        assert resp.status_code == 200
        assert "/products/TILE-1/catalog" not in resp.text
        assert "/products/TILE-1/placement" in resp.text

    def test_both_tiles_present_for_owner(self, login_as, wired):
        owner = login_as(Role.owner)
        _add(owner, "TILE-2", name="Tile Item", price="1.00")
        resp = owner.get("/products/TILE-2")
        assert resp.status_code == 200
        assert "/products/TILE-2/catalog" in resp.text
        assert "/products/TILE-2/placement" in resp.text

    def test_copy_and_delete_only_for_edit_catalog(self, login_as, wired):
        owner = login_as(Role.owner)
        _add(owner, "CD-1", name="CD Item", price="1.00")
        loader = login_as(Role.loader)
        resp = loader.get("/products/CD-1")
        assert "/products/CD-1/copy" not in resp.text
        assert "/products/CD-1/delete" not in resp.text

        resp = owner.get("/products/CD-1")
        assert "/products/CD-1/copy" in resp.text
        assert "/products/CD-1/delete" in resp.text


class TestCatalogGating:
    @pytest.mark.parametrize(
        "role,expected_status",
        [
            (Role.owner, 200),
            (Role.secretary, 200),
            (Role.tech, 403),
            (Role.loader, 403),
        ],
    )
    def test_catalog_get_gated_on_edit_catalog(
        self, login_as, wired, role, expected_status
    ):
        owner = login_as(Role.owner)
        _add(owner, f"GATE-{role.value}", name="Item", price="1.00")
        worker = login_as(role)
        resp = worker.get(f"/products/GATE-{role.value}/catalog")
        assert resp.status_code == expected_status

    @pytest.mark.parametrize("role", [Role.tech, Role.loader])
    def test_catalog_post_403_for_placement_only_roles(self, login_as, wired, role):
        owner = login_as(Role.owner)
        _add(owner, f"POSTGATE-{role.value}", name="Item", price="1.00")
        worker = login_as(role)
        resp = worker.post(
            f"/products/POSTGATE-{role.value}/catalog",
            data={"name": "Hacked", "price": "0.01", "kind": "other"},
        )
        assert resp.status_code == 403
        product = next(
            p for p in context.config.products if p.sku == f"POSTGATE-{role.value}"
        )
        assert product.name == "Item"


class TestPlacementGating:
    @pytest.mark.parametrize(
        "role", [Role.owner, Role.secretary, Role.tech, Role.loader]
    )
    def test_placement_get_is_200_for_every_role(self, login_as, wired, role):
        owner = login_as(Role.owner)
        _add(owner, f"PLGET-{role.value}", name="Item", price="1.00")
        worker = login_as(role)
        resp = worker.get(f"/products/PLGET-{role.value}/placement")
        assert resp.status_code == 200

    @pytest.mark.parametrize("role", [Role.loader, Role.tech])
    def test_placement_post_allowed_for_placement_roles(self, login_as, wired, role):
        _cfg, _vmc, inv, _store = wired
        owner = login_as(Role.owner)
        _add(owner, f"PLPOST-{role.value}", name="Item", price="5.00", slot="1")
        worker = login_as(role)
        resp = worker.post(
            f"/products/PLPOST-{role.value}/placement",
            data={"slot": "2", "inventory_count": "3", "track_inventory": "on"},
        )
        assert resp.status_code == 200
        product = next(
            p for p in context.config.products if p.sku == f"PLPOST-{role.value}"
        )
        assert product.slot == 2
        assert product.price == 5.00
        assert inv.get_count(f"PLPOST-{role.value}") == 3


class TestCatalogWrites:
    def test_catalog_post_changes_name_price_kind(self, client, wired):
        cfg, _vmc, _inv, _store = wired
        _add(client, "CAT-1", name="Old Name", price="1.00")
        resp = client.post(
            "/products/CAT-1/catalog",
            data={"name": "New Name", "price": "3.50", "kind": "water"},
        )
        assert resp.status_code == 200
        assert resp.headers["hx-redirect"] == "/products/CAT-1"
        updated = next(p for p in cfg.products if p.sku == "CAT-1")
        assert updated.name == "New Name"
        assert updated.price == 3.50
        assert updated.kind == "water"

    def test_catalog_breadcrumb_chain_is_four_entries(self, client, wired):
        """Home / Products / <product name> / Catalog (brief resolution 8),
        the product's name as the crumb title."""
        _add(client, "BC-1", name="Bread Crumb Item", price="1.00")
        resp = client.get("/products/BC-1/catalog")
        assert resp.status_code == 200
        assert 'href="/products"' in resp.text
        assert 'href="/products/BC-1"' in resp.text
        assert "Bread Crumb Item" in resp.text
        assert ">Catalog<" in resp.text

    def test_catalog_breadcrumb_falls_back_to_sku_when_name_empty(self, client, wired):
        _add(client, "BC-2", name="", price="1.00")
        resp = client.get("/products/BC-2/catalog")
        assert resp.status_code == 200
        assert ">BC-2<" in resp.text


class TestPlacementWrites:
    def test_placement_post_changes_slot_count_and_tracking(self, client, wired):
        cfg, _vmc, inv, _store = wired
        _add(client, "PLC-1", name="Placed Item", price="2.00", slot="1")
        resp = client.post(
            "/products/PLC-1/placement",
            data={"slot": "8", "inventory_count": "15", "track_inventory": "on"},
        )
        assert resp.status_code == 200
        assert resp.headers["hx-redirect"] == "/products/PLC-1"
        updated = next(p for p in cfg.products if p.sku == "PLC-1")
        assert updated.slot == 8
        assert inv.get_count("PLC-1") == 15
        assert inv.is_tracked("PLC-1") is True

    def test_placement_post_unchecked_tracking_clears_flag(self, client, wired):
        _cfg, _vmc, inv, _store = wired
        _add(client, "PLC-3", name="Placed Item", price="2.00", slot="2")
        client.post(
            "/products/PLC-3/placement",
            data={"slot": "2", "inventory_count": "5", "track_inventory": "on"},
        )
        assert inv.is_tracked("PLC-3") is True
        resp = client.post(
            "/products/PLC-3/placement",
            data={"slot": "2", "inventory_count": "5"},
        )
        assert resp.status_code == 200
        assert inv.is_tracked("PLC-3") is False

    def test_placement_post_cannot_change_name_or_price(self, client, wired):
        """Part 1's a2b8829: a placement POST must not smuggle a catalog
        change through, regardless of what the form body contains — the
        handler declares no name/price Form params at all, so
        update_product is always called with the product's own stored
        values."""
        cfg, _vmc, _inv, _store = wired
        _add(client, "PLC-4", name="Original", price="9.99", slot="3")
        resp = client.post(
            "/products/PLC-4/placement",
            data={
                "slot": "5",
                "inventory_count": "2",
                "name": "Hacked Name",
                "price": "0.01",
            },
        )
        assert resp.status_code == 200
        updated = next(p for p in cfg.products if p.sku == "PLC-4")
        assert updated.slot == 5
        assert updated.name == "Original"
        assert updated.price == 9.99

    def test_placement_post_negative_count_is_rejected(self, client, wired):
        """Part 1's 231fe36: a negative inventory_count is rejected and
        leaves the stored count unchanged."""
        cfg, _vmc, inv, _store = wired
        _add(client, "PLC-6", name="Placed Item", price="2.00", slot="1")
        client.post(
            "/products/PLC-6/placement",
            data={"slot": "1", "inventory_count": "9", "track_inventory": "on"},
        )
        assert inv.get_count("PLC-6") == 9

        resp = client.post(
            "/products/PLC-6/placement",
            data={"slot": "6", "inventory_count": "-3", "track_inventory": "on"},
        )
        assert resp.status_code == 200
        assert inv.get_count("PLC-6") == 9
        updated = next(p for p in cfg.products if p.sku == "PLC-6")
        assert updated.slot == 1

    def test_placement_post_with_slot_already_in_use_changes_nothing(
        self, client, wired
    ):
        """Part 1's a2b8829 (Copilot review, web_interface/routes.py:1202):
        a rejected slot change must leave every field of this form — slot,
        count, and tracking — exactly as it was, not just the slot."""
        cfg, _vmc, inv, _store = wired
        _add(client, "PLC-7", name="Item A", price="2.00", slot="1")
        _add(client, "PLC-8", name="Item B", price="3.00", slot="2")
        client.post(
            "/products/PLC-8/placement",
            data={"slot": "2", "inventory_count": "9", "track_inventory": "on"},
        )
        assert inv.get_count("PLC-8") == 9
        assert inv.is_tracked("PLC-8") is True

        resp = client.post(
            "/products/PLC-8/placement",
            data={"slot": "1", "inventory_count": "50"},  # slot 1 is PLC-7's
        )
        assert resp.status_code == 200
        updated = next(p for p in cfg.products if p.sku == "PLC-8")
        assert updated.slot == 2
        assert inv.get_count("PLC-8") == 9
        assert inv.is_tracked("PLC-8") is True


class TestProductsListInventoryCount:
    def test_list_shows_inventory_manager_count_not_stale_product_field(
        self, client, wired
    ):
        """Part 1's d58da37: the list must render the InventoryManager
        count, never the stale Product.inventory_count seed field."""
        cfg, _vmc, inv, _store = wired
        _add(client, "CNT-1", name="Counter", price="1.00", inventory_count="5")
        # A restock-style direct adjustment, bypassing the placement form —
        # exactly the case that used to render stale.
        inv.set_count("CNT-1", 41)

        resp = client.get("/products")
        assert resp.status_code == 200
        assert "41 in stock" in resp.text
        product = next(p for p in cfg.products if p.sku == "CNT-1")
        assert product.inventory_count != 41


class TestCreateCopyDelete:
    def test_create_works_for_owner_and_registers_inventory_sku(self, client, wired):
        cfg, _vmc, inv, _store = wired
        resp = client.post(
            "/products/new",
            data={
                "sku": "NEW-1",
                "name": "New Item",
                "price": "4.00",
                "kind": "ice",
                "slot": "3",
                "inventory_count": "7",
                "track_inventory": "on",
            },
        )
        assert resp.status_code == 200
        assert resp.headers["hx-redirect"] == "/products/NEW-1"
        product = next(p for p in cfg.products if p.sku == "NEW-1")
        assert product.name == "New Item"
        assert product.kind == "ice"
        assert product.slot == 3
        assert inv.get_count("NEW-1") == 7
        assert inv.is_tracked("NEW-1") is True

    def test_create_negative_count_rejected(self, client, wired):
        cfg, _vmc, _inv, _store = wired
        resp = client.post(
            "/products/new",
            data={
                "sku": "NEG-NEW",
                "name": "X",
                "price": "1.00",
                "inventory_count": "-1",
            },
        )
        assert resp.status_code == 200
        assert not any(p.sku == "NEG-NEW" for p in cfg.products)

    def test_copy_prefills_from_source_and_reads_live_count(self, client, wired):
        _cfg, _vmc, inv, _store = wired
        _add(client, "SRC-1", name="Source", price="5.00", kind="water")
        inv.set_count("SRC-1", 12)
        resp = client.get("/products/SRC-1/copy")
        assert resp.status_code == 200
        assert "Source Copy" in resp.text
        assert 'value="12"' in resp.text
        # Copy is a fresh SKU, not the source's — the visible SKU input is
        # a freshly generated value, not the source's own SKU (the source
        # SKU still appears, but only in the hidden source_sku field this
        # form's own error-path re-render depends on).
        assert 'id="sku" name="sku" value="SRC-1"' not in resp.text

    def test_copy_breadcrumb_chain_is_four_entries(self, client, wired):
        """Home / Products / <product name> / Copy, nested under the
        product being copied (not under Products) — the shell's Back
        button on /products/{sku}/copy must return to /products/{sku},
        not to /products.

        The form's own Cancel link already points at /products/{sku}
        (unrelated to this defect and unchanged by the fix), and the
        name field's prefill ("<name> Copy") already contains the bare
        product name as a substring — so both are checked precisely
        enough not to pass by accident on the pre-fix rendering.
        """
        import re

        _add(client, "SRC-BC", name="Source Bread Crumb", price="1.00")
        resp = client.get("/products/SRC-BC/copy")
        assert resp.status_code == 200
        # The breadcrumb nav's third crumb is the product's own name,
        # linking to /products/SRC-BC — the fourth crumb the defect
        # dropped by nesting Copy under Products instead of the product.
        assert ">Source Bread Crumb</a>" in resp.text
        assert ">Copy<" in resp.text
        # The shell's Back button (in the header, distinct from the
        # form's Cancel link further down the page) must resolve to the
        # product's own level, not the Products list.
        assert re.search(r'<a href="/products/SRC-BC"[\s\S]*?>Back</a>', resp.text)

    def test_copy_then_create_makes_independent_product(self, client, wired):
        cfg, _vmc, inv, _store = wired
        _add(client, "SRC-2", name="Source Two", price="6.00", kind="ice", slot="4")
        resp = client.get("/products/SRC-2/copy")
        assert resp.status_code == 200
        import re

        m = re.search(r'id="sku" name="sku" value="([^"]+)"', resp.text)
        assert m is not None
        new_sku = m.group(1)
        assert new_sku != "SRC-2"

        create_resp = client.post(
            "/products/new",
            data={
                "sku": new_sku,
                "name": "Source Two Copy",
                "price": "6.00",
                "kind": "ice",
                "mode": "copy",
                "source_sku": "SRC-2",
            },
        )
        assert create_resp.status_code == 200
        assert create_resp.headers["hx-redirect"] == f"/products/{new_sku}"
        assert any(p.sku == new_sku for p in cfg.products)
        assert any(p.sku == "SRC-2" for p in cfg.products)

    def test_delete_works_for_owner_and_removes_inventory_sku(self, client, wired):
        cfg, _vmc, inv, _store = wired
        _add(client, "DEL-1", name="Doomed", price="1.00")
        assert "DEL-1" in inv.get_all()

        resp = client.post("/products/DEL-1/delete")
        assert resp.status_code == 200
        assert resp.headers["hx-redirect"] == "/products"
        assert not any(p.sku == "DEL-1" for p in cfg.products)
        assert "DEL-1" not in inv.get_all()

    def test_create_copy_delete_403_for_loader(self, login_as, wired):
        owner = login_as(Role.owner)
        _add(owner, "SRC-3", name="Source Three", price="1.00")
        loader = login_as(Role.loader)

        resp = loader.post(
            "/products/new", data={"sku": "LOADER-NEW", "name": "X", "price": "1.00"}
        )
        assert resp.status_code == 403

        resp = loader.get("/products/SRC-3/copy")
        assert resp.status_code == 403

        resp = loader.post("/products/SRC-3/delete")
        assert resp.status_code == 403


class TestUnknownSku:
    @pytest.mark.parametrize(
        "path",
        [
            "/products/NOPE",
            "/products/NOPE/catalog",
            "/products/NOPE/placement",
            "/products/NOPE/copy",
        ],
    )
    def test_unknown_sku_get_routes_are_shell_404(self, client, wired, path):
        resp = client.get(path)
        assert resp.status_code == 404
        assert "Not found" in resp.text
        # The in-shell 404 page (error.html), not a bare JSON body.
        assert "Back" in resp.text

    @pytest.mark.parametrize(
        "path,data",
        [
            ("/products/NOPE/catalog", {"name": "x", "price": "1", "kind": "other"}),
            (
                "/products/NOPE/placement",
                {"slot": "0", "inventory_count": "0"},
            ),
            ("/products/NOPE/delete", {}),
        ],
    )
    def test_unknown_sku_post_routes_are_shell_404(self, client, wired, path, data):
        resp = client.post(path, data=data)
        assert resp.status_code == 404
        assert "Not found" in resp.text


class TestCsrfGuard:
    @pytest.mark.parametrize(
        "path",
        [
            "/products/new",
            "/products/HTMX-1/catalog",
            "/products/HTMX-1/placement",
            "/products/HTMX-1/delete",
        ],
    )
    def test_post_without_hx_request_is_403(self, client, wired, path):
        _add(client, "HTMX-1", name="Item", price="1.00")
        resp = client.post(
            path,
            headers={"HX-Request": ""},
            data={
                "sku": "HTMX-1",
                "name": "n",
                "price": "1",
                "slot": "0",
                "kind": "other",
                "inventory_count": "0",
            },
        )
        assert resp.status_code == 403
        assert "HTMX" in resp.text


class TestLockBadge:
    def test_locked_badge_shown_on_list_and_detail(self, client, wired):
        from contracts.vending_machine import FaultCode

        _cfg, vmc, _inv, _store = wired
        _add(client, "ICE-1", name="Ice", price="2.5")
        vmc._raise_fault(FaultCode.ICE_301, sku="ICE-1")

        list_resp = client.get("/products")
        assert "locked" in list_resp.text.lower()
        assert "ICE-301" in list_resp.text

        detail_resp = client.get("/products/ICE-1")
        assert "locked" in detail_resp.text.lower()
        assert "ICE-301" in detail_resp.text
