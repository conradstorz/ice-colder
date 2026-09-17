# First-Run Startup & Product Lifecycle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A machine with no `config.json` boots straight into normal operation with blank defaults; the owner builds the product catalog live through the dashboard (add/edit/copy/delete) with no JSON editing and no restart.

**Architecture:** Minimal in-place changes per the approved spec (`docs/superpowers/specs/2026-09-12-first-run-startup-design.md`): `load_config()` self-provisions instead of exiting, `ConfigModel` products default to empty, `config_store`/`InventoryManager` gain delete/remove, the dashboard gains a delete route + empty state, and `_process_payment` gains a pre-deduction product-existence guard.

**Tech Stack:** Python 3.12, FastAPI + HTMX/Jinja2, Pydantic v2, transitions, pytest (asyncio_mode=auto), uv.

## Global Constraints

- Use `uv run pytest` / `uv run python` for everything; never bare `pip`/`python`.
- NEVER chain shell commands with `&&` — run each command as a separate tool call.
- After code changes in a task: `ruff check --fix .` then `ruff format .` (separate calls) before committing.
- Commit after every task; messages end with:
  `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`
- Do NOT touch `config.json` (repo root), `colder-docker/`, or `data/`.
- Baseline before Task 1: full suite is 336 passed, 9 skipped.
- `tests/conftest.py` has an autouse fixture monkeypatching `services.config_store.CONFIG_PATH` to `tmp_path / "config.json"` — new tests rely on it (plus `monkeypatch.chdir(tmp_path)` where cwd-relative paths matter).
- Tests document intended behavior. If a production code path breaks in a way a test change would merely hide (esp. simulators on empty product lists), STOP and report — do not paper over it.

---

### Task 1: Blank defaults — empty product list

**Files:**
- Modify: `config/config_model.py:121-123` (`PhysicalDetails.products`)
- Modify: `tests/test_config_model.py:23-27` (`test_products_convenience_property`)
- Modify: `tests/test_web_routes.py:65-68` (`test_edit_form`)

**Interfaces:**
- Produces: `ConfigModel().products == []`. Every later task assumes a fresh `ConfigModel()` has no products.

- [ ] **Step 1: Update the two known default-product tests to the new behavior (failing first)**

In `tests/test_config_model.py`, replace `test_products_convenience_property`:

```python
def test_products_convenience_property():
    cfg = ConfigModel()
    assert cfg.products is cfg.physical.products
    assert cfg.products == []
```

In `tests/test_web_routes.py`, replace `test_edit_form`:

```python
    def test_edit_form(self, client):
        """Edit form for a product created via the dashboard."""
        client.post(
            "/inventory/add",
            data={"sku": "EDIT-1", "name": "Editable", "price": "1.50"},
        )
        resp = client.get("/inventory/edit/EDIT-1")
        assert resp.status_code == 200
        assert "Editable" in resp.text
```

- [ ] **Step 2: Run them to verify they fail against current code**

Run: `uv run pytest tests/test_config_model.py::test_products_convenience_property tests/test_web_routes.py -v`
Expected: `test_products_convenience_property` FAILS (list has SAMPLE-SKU entry). `test_edit_form` PASSES already (it no longer references the default) — that's fine; the model change is what the first test gates.

- [ ] **Step 3: Change the model default**

In `config/config_model.py`, change:

```python
    products: List[Product] = Field(
        default_factory=lambda: [Product()], description="List of products available"
    )
```

to:

```python
    products: List[Product] = Field(
        default_factory=list, description="List of products available"
    )
```

- [ ] **Step 4: Run the full suite and repair default-product assumptions**

Run: `uv run pytest -q`
Expected: `test_products_convenience_property` now passes. If any OTHER test fails, inspect it: a test that merely assumed the SAMPLE-SKU default product exists gets an explicit product added to its own config setup (e.g., `cfg.physical.products = [Product(sku="T-1", name="Test", price=1.0)]`) — assertions must not be weakened. If a failure shows PRODUCTION code (e.g., `simulators/vending_machine.py`, `simulators/mdb_gateway.py`) crashing on an empty product list, STOP and report BLOCKED with the traceback.

- [ ] **Step 5: Lint + commit**

Run `ruff check --fix .` then `ruff format .` (separate calls).

```bash
git add config/config_model.py tests/test_config_model.py tests/test_web_routes.py
git commit -m "feat: ConfigModel defaults to an empty product catalog

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

(Include any additional test files repaired in Step 4 in the `git add`.)

---

### Task 2: First-run boot — create defaults and keep running

**Files:**
- Modify: `main.py:77-121` (`_generate_skeleton`, `load_config`) and imports
- Create: `tests/test_first_run.py`

**Interfaces:**
- Consumes: `save_config(config)` from `services/config_store.py` (atomic, call-time `CONFIG_PATH`).
- Produces: `load_config() -> ConfigModel` returns a persisted blank-defaults model on first run instead of exiting; still `sys.exit(1)` on unreadable/invalid existing config.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_first_run.py`:

```python
"""First-run startup: config.json is auto-created and the app continues."""

import json

import pytest

import main as main_mod


def test_first_run_creates_config_and_continues(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = main_mod.load_config()
    assert cfg.products == []
    saved = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert saved["physical"]["products"] == []


def test_first_run_config_round_trips(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    main_mod.load_config()          # first run writes the file
    cfg = main_mod.load_config()    # second run loads it normally
    assert cfg.products == []


def test_unreadable_config_still_exits(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.json").write_text("{not valid json", encoding="utf-8")
    with pytest.raises(SystemExit):
        main_mod.load_config()


def test_invalid_config_still_exits(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.json").write_text(
        '{"mqtt": {"broker_port": "not-a-port"}}', encoding="utf-8"
    )
    with pytest.raises(SystemExit):
        main_mod.load_config()
```

- [ ] **Step 2: Run tests to verify the first two fail**

Run: `uv run pytest tests/test_first_run.py -v`
Expected: first two FAIL with `SystemExit` (current code exits 0 after writing the skeleton); the two exit tests already pass.

- [ ] **Step 3: Implement**

In `main.py`, add to the imports (with the other `services.` imports at top):

```python
from services.config_store import save_config
```

Replace `_generate_skeleton` (lines 77-86) entirely with:

```python
def _create_default_config() -> ConfigModel:
    """First run: build blank defaults, persist them, and continue running."""
    defaults = ConfigModel()
    save_config(defaults)
    logger.info("First run: created 'config.json' with blank defaults")
    return defaults
```

In `load_config`, replace:

```python
    if not os.path.exists("config.json"):
        logger.warning("'config.json' not found, creating skeleton")
        _generate_skeleton()
```

with:

```python
    if not os.path.exists("config.json"):
        logger.warning("'config.json' not found — first run: creating defaults")
        return _create_default_config()
```

Leave the rest of `load_config` (read/validate/exit paths) untouched.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_first_run.py -v` — 4 passed.
Run: `uv run pytest -q` — full suite green.

- [ ] **Step 5: Lint + commit**

```bash
git add main.py tests/test_first_run.py
git commit -m "feat: first run creates blank config.json and boots instead of exiting

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: delete_product + InventoryManager.remove_sku

**Files:**
- Modify: `services/config_store.py` (append `delete_product`)
- Modify: `services/inventory_manager.py` (append `remove_sku`)
- Modify: `tests/test_config_store.py`, `tests/test_inventory_manager.py` (append tests)

**Interfaces:**
- Produces: `delete_product(config: ConfigModel, sku: str) -> bool` (removes + saves; False and no save on unknown SKU). `InventoryManager.remove_sku(sku: str)` (drops count + tracking, persists; unknown SKU is a no-op). Task 4's routes call both.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_config_store.py`:

```python
def test_delete_product_removes_and_saves(tmp_path):
    from config.config_model import Product
    from services.config_store import delete_product

    cfg = ConfigModel()
    cfg.physical.products = [Product(sku="A-1", name="Thing A", price=1.0)]
    assert delete_product(cfg, "A-1") is True
    assert cfg.products == []
    assert (tmp_path / "config.json").exists()


def test_delete_product_unknown_sku_returns_false_without_saving(tmp_path):
    from services.config_store import delete_product

    cfg = ConfigModel()
    assert delete_product(cfg, "NOPE") is False
    assert not (tmp_path / "config.json").exists()
```

Append to `tests/test_inventory_manager.py` (match its import style; it already imports `InventoryManager`):

```python
class TestRemoveSku:
    def test_remove_sku_deletes_and_persists(self, tmp_path):
        path = tmp_path / "inv.json"
        inv = InventoryManager([], path=path)
        inv.add_sku("X-1", 5, tracked=True)
        inv.remove_sku("X-1")
        assert inv.get_count("X-1") == 0
        assert inv.is_tracked("X-1") is False
        reloaded = InventoryManager([], path=path)
        assert "X-1" not in reloaded.get_all()

    def test_remove_sku_unknown_is_harmless(self, tmp_path):
        inv = InventoryManager([], path=tmp_path / "inv.json")
        inv.remove_sku("NOPE")  # must not raise
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_config_store.py tests/test_inventory_manager.py -v`
Expected: FAIL — `ImportError: cannot import name 'delete_product'` / `AttributeError: remove_sku`.

- [ ] **Step 3: Implement**

Append to `services/config_store.py`:

```python
def delete_product(config: ConfigModel, sku: str) -> bool:
    for i, p in enumerate(config.products):
        if p.sku == sku:
            del config.products[i]
            save_config(config)
            logger.info(f"Deleted product SKU={sku} | name='{p.name}'")
            return True

    logger.warning(f"Cannot delete product: SKU not found: {sku}")
    return False
```

Append to `services/inventory_manager.py` (inside the class, after `add_sku`):

```python
    def remove_sku(self, sku: str):
        """Remove a SKU from counts and tracking (e.g., product deleted)."""
        removed = self._counts.pop(sku, None) is not None
        self._track.pop(sku, None)
        if removed:
            self._save()
            logger.info(f"Inventory: removed SKU {sku}")
```

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/test_config_store.py tests/test_inventory_manager.py -v` — all pass.

- [ ] **Step 5: Lint + commit**

```bash
git add services/config_store.py services/inventory_manager.py tests/test_config_store.py tests/test_inventory_manager.py
git commit -m "feat: delete_product in config store; InventoryManager.remove_sku

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: Dashboard — delete route, inventory wiring, empty state

**Files:**
- Modify: `web_interface/routes.py` (setter, add-route registration, delete route)
- Modify: `web_interface/templates/partials/inventory_table.html` (delete button, empty state)
- Modify: `main.py` (wire `routes.set_inventory_manager(inventory)`)
- Modify: `tests/test_web_routes.py` (fixture + new tests)

**Interfaces:**
- Consumes: `delete_product`, `InventoryManager.add_sku/remove_sku/get_all` from Task 3.
- Produces: `routes.set_inventory_manager(inv)`; `POST /inventory/delete/{sku}` (auth-gated, returns the re-rendered inventory table, HTTP 200 even for unknown SKU); `POST /inventory/add` registers the SKU with the inventory manager.

- [ ] **Step 1: Write the failing tests**

In `tests/test_web_routes.py`, add the import near the other imports:

```python
from services.inventory_manager import InventoryManager
```

Replace the `client` fixture with:

```python
@pytest.fixture
def client(tmp_path):
    """Create a TestClient with a real ConfigModel, VMC, and InventoryManager."""
    cfg = ConfigModel()
    vmc = VMC(config=cfg)
    inv = InventoryManager([], path=tmp_path / "inventory.json")
    routes.set_config_object(cfg)
    routes.set_vmc_instance(vmc)
    routes.set_inventory_manager(inv)

    with TestClient(app) as c:
        c.auth = ("admin", "changeme")
        yield c

        for t in vmc._pending_tasks:
            t.cancel()
```

Append a new test class:

```python
class TestDeleteAndEmptyState:
    def test_empty_state_shown_when_no_products(self, client):
        resp = client.get("/inventory")
        assert resp.status_code == 200
        assert "No products configured" in resp.text

    def test_delete_product_removes_row(self, client):
        client.post(
            "/inventory/add",
            data={"sku": "DEL-1", "name": "Doomed", "price": "1.00"},
        )
        resp = client.post("/inventory/delete/DEL-1")
        assert resp.status_code == 200
        assert "Doomed" not in resp.text
        assert "No products configured" in resp.text

    def test_delete_unknown_sku_is_harmless(self, client):
        resp = client.post("/inventory/delete/NOPE")
        assert resp.status_code == 200

    def test_delete_requires_auth(self, client):
        resp = client.post("/inventory/delete/X", auth=None)
        assert resp.status_code == 401

    def test_add_registers_inventory_sku(self, client):
        from web_interface import routes as r

        client.post(
            "/inventory/add",
            data={"sku": "INV-1", "name": "Tracked Thing", "price": "1.00"},
        )
        assert "INV-1" in r.inventory_manager.get_all()

    def test_delete_removes_inventory_sku(self, client):
        from web_interface import routes as r

        client.post(
            "/inventory/add",
            data={"sku": "INV-2", "name": "Gone Soon", "price": "1.00"},
        )
        client.post("/inventory/delete/INV-2")
        assert "INV-2" not in r.inventory_manager.get_all()
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_web_routes.py -v`
Expected: new tests FAIL (`set_inventory_manager` missing, 404/405 on delete route, no empty-state text).

- [ ] **Step 3: Implement routes.py**

Update the import from config_store:

```python
from services.config_store import add_product, delete_product, update_product
```

Add below `set_event_recorder` (same pattern):

```python
inventory_manager = None


def set_inventory_manager(inv):
    global inventory_manager
    inventory_manager = inv
```

In `add_new_product`, after `success = add_product(config, sku, name, price)` add:

```python
        if success and inventory_manager:
            inventory_manager.add_sku(sku, 0, tracked=False)
```

Add the delete route (place it after `update_inventory_item`):

```python
    @router.post("/inventory/delete/{sku}", response_class=HTMLResponse)
    async def delete_inventory_item(request: Request, sku: str):
        success = delete_product(config, sku)
        if success and inventory_manager:
            inventory_manager.remove_sku(sku)
        return templates.TemplateResponse(
            "partials/inventory_table.html",
            {"request": request, "products": config.products},
        )
```

- [ ] **Step 4: Implement the template**

In `web_interface/templates/partials/inventory_table.html`, inside the actions `<div class="flex gap-2">` after the Copy button, add:

```html
            <button
              hx-post="/inventory/delete/{{ product.sku }}"
              hx-target="#content-body"
              hx-swap="innerHTML"
              hx-confirm="Delete {{ product.name }}?"
              class="border border-red-200 hover:bg-red-50 text-red-600 px-2.5 py-1 rounded text-xs font-medium transition-colors">
              Delete
            </button>
```

And convert the row loop to `for`/`else` for the empty state — after the `{% endfor %}`'s preceding `</tr>`, structure it as:

```html
      {% for product in products %}
      <tr class="hover:bg-gray-50">
        ...existing row unchanged...
      </tr>
      {% else %}
      <tr>
        <td colspan="5" class="py-8 text-center text-gray-400 text-sm">
          No products configured — add your first product.
        </td>
      </tr>
      {% endfor %}
```

- [ ] **Step 5: Wire main.py**

In `main.py`, directly after `routes.set_vmc_instance(vmc)` add:

```python
    routes.set_inventory_manager(inventory)
```

- [ ] **Step 6: Run to verify pass, then full suite**

Run: `uv run pytest tests/test_web_routes.py -v` — all pass.
Run: `uv run pytest -q` — full suite green.

- [ ] **Step 7: Lint + commit**

```bash
git add web_interface/routes.py web_interface/templates/partials/inventory_table.html main.py tests/test_web_routes.py
git commit -m "feat: dashboard product delete + empty state; wire inventory manager into routes

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: Mid-session delete guard in _process_payment

**Files:**
- Modify: `controller/vmc.py` (`_process_payment`, after the state check)
- Modify: `tests/test_vmc_flows.py` (append test)

**Interfaces:**
- Consumes: existing `error_occurred()` trigger; `on_error` refunds full escrow.
- Produces: a sale whose product vanished before charging lands in `error` with the customer fully refunded — no deduction ever happens.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_vmc_flows.py`:

```python
async def test_product_deleted_mid_session_refunds_and_errors():
    vmc = make_vmc(price=2.50)
    vmc.attach_to_loop(asyncio.get_running_loop())
    messages: list[str] = []
    vmc.set_message_callback(messages.append)
    vmc.machine.set_state("interacting_with_user")
    vmc.selected_product = vmc.products[0]
    vmc.credit_escrow = 5.00
    vmc.products.clear()  # product deleted via the dashboard mid-session

    vmc._process_payment()

    assert vmc.state == "error"
    assert vmc.credit_escrow == 0.0  # full escrow refunded by on_error
    assert any("refunded" in m.lower() for m in messages)
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_vmc_flows.py::test_product_deleted_mid_session_refunds_and_errors -v`
Expected: FAIL — current code charges $2.50 for the vanished product and moves to `dispensing`.

- [ ] **Step 3: Implement the guard**

In `controller/vmc.py` `_process_payment`, directly after the existing early return:

```python
        if self.state != "interacting_with_user":
            logger.debug(
                "State is not interacting_with_user; aborting payment process."
            )
            return
```

insert:

```python
        if self.selected_product is None or self.selected_product not in self.products:
            logger.error(
                "Selected product no longer exists in the catalog; cancelling sale."
            )
            txn_log.info("SALE CANCELLED: selected product removed from catalog")
            self.error_occurred()
            return
```

(Guard runs BEFORE any price deduction, so `on_error`'s full-escrow refund makes the customer whole — see spec §5 for why the guard is here and not in `on_dispense_product`.)

- [ ] **Step 4: Run to verify pass, then full suite**

Run: `uv run pytest tests/test_vmc_flows.py -v` — all pass (existing 6 flows unaffected: they all keep their product in the list).
Run: `uv run pytest -q` — full suite green.

- [ ] **Step 5: Lint + commit (include the spec amendment if uncommitted)**

Run `ruff check --fix .` then `ruff format .`.

```bash
git add controller/vmc.py tests/test_vmc_flows.py docs/superpowers/specs/2026-09-12-first-run-startup-design.md
git commit -m "fix: cancel sale with full refund when selected product was deleted mid-session

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Out of scope (per spec)

- Setup wizard, dashboard machine-identity/password editing, payment-disable signaling on empty catalog, skeleton section stripping, config migration.
