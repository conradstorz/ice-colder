# First-Run Startup & Product Lifecycle — Design

**Date:** 2026-09-12
**Status:** Approved approach: minimal in-place change (Approach A)

## Goal

A machine with no `config.json` boots straight into normal operation with blank
defaults, creates everything it needs on its own, and the owner builds the
product catalog live through the dashboard — add, edit, copy, and delete —
with no hand-editing of JSON and no restart.

## Decisions (from brainstorming)

1. **First boot: boot and run.** Generate `config.json` and keep running.
   No setup wizard, no exit-and-edit.
2. **Zero products: normal idle.** No special customer-facing lockout;
   button presses log "invalid product index", payments behave as today.
3. **Blank defaults: empty products, placeholder rest.** No more `SAMPLE-SKU`
   seed product. People/location/email keep obvious placeholders
   (`Your Name`, `user@example.com`) because `EmailStr` cannot be blank.
4. **Admin auth: unchanged.** Default `changeme` password with the existing
   loud startup warning.
5. **Product CRUD: add delete.** Complete the dashboard loop
   (add / edit / copy / **delete**).

## Non-Goals

- Setup wizard or dashboard-driven machine identity/password editing.
- Payment-disable signaling to the ESP32 while catalog is empty.
- Stripping optional config sections (paypal, sms, snapchat) from the skeleton.
- Migrating or versioning existing config files.

## Design

### 1. First-boot flow (`main.py`)

`load_config()` behavior when `config.json` is missing changes from
"write skeleton, `sys.exit(0)`, tell user to edit" to:

- Log `"'config.json' not found — first run: creating defaults"`.
- Build `ConfigModel()` (now with empty products, see §2).
- Write it via `services.config_store.save_config` (atomic tmp+rename —
  reuse the hardened writer; `_generate_skeleton`'s ad-hoc `open()` write
  is removed along with its `sys.exit(0)`).
- Return the model and continue startup normally.

Unchanged: an existing-but-invalid `config.json` still logs validation
errors and exits — a broken real config is never silently replaced.

Everything else the app needs already self-provisions and stays as-is:
`LOGS/` (setup_logging), `data/` + `events.db` (EventRecorder),
`inventory.json` (InventoryManager).

### 2. Blank defaults (`config/config_model.py`)

`PhysicalDetails.products` default changes:

- from `default_factory=lambda: [Product()]`
- to `default_factory=list`

All other defaults unchanged. Consumers already tolerate an empty list:
`VMC.select_product` bounds-checks the index; the dashboard table renders
zero rows (plus the new empty state, §4).

### 3. Product lifecycle (`services/config_store.py`, `services/inventory_manager.py`)

New `delete_product(config, sku) -> bool` in `config_store.py`: removes the
product from `config.products`, saves via `save_config`, returns False with
a warning log when the SKU doesn't exist. Mirrors `add_product`'s style.

New `InventoryManager.remove_sku(sku)`: drops the SKU from `_counts` and
`_track`, persists. (No such method exists today.)

Wiring gap fixed while we're here: products added via the dashboard are
currently never registered with the InventoryManager until restart (they
sell anyway because untracked SKUs default to available, but counts/tracking
are stale). The add route will call `inventory.add_sku(sku,
product.inventory_count, product.track_inventory)` after a successful
`add_product`; the delete route calls `remove_sku` after a successful
`delete_product`.

### 4. Dashboard (`web_interface/routes.py`, templates)

- `routes.py` gains `set_inventory_manager(inv)` (same module-global setter
  pattern as `set_vmc_instance`); `main.py` calls it during wiring.
- New route `POST /inventory/delete/{sku}` (inside the auth-gated router,
  like everything else): calls `delete_product` + `remove_sku`, re-renders
  `partials/inventory_table.html`. Unknown SKU → table unchanged, warning
  logged, still HTTP 200 (HTMX-friendly).
- `partials/inventory_table.html`: each row gets a Delete button with
  `hx-post="/inventory/delete/{sku}"` and `hx-confirm` ("Delete <name>?").
  When `products` is empty the table body shows one empty-state row:
  "No products configured — add your first product."

### 5. Mid-session delete guard (`controller/vmc.py`)

`on_dispense_product` computes the slot with `self.products.index(
self.selected_product)`, which raises `ValueError` if the product was
deleted between selection and dispense (today that exception is swallowed
by `@logger.catch` and no dispense command is ever sent — money taken,
nothing dispensed, machine stuck in `dispensing` until the 60s fallback).

Guard (refined at planning): the check lives in `_process_payment`, after
the state check and BEFORE the price is deducted — if
`self.selected_product` is gone from `self.products`, log the anomaly and
call `error_occurred()`; `on_error` refunds the full (undeducted) escrow,
so the customer is made whole with no re-credit step. Placing the guard in
`on_dispense_product` was rejected: triggering `error_occurred()` inside a
`before`-callback nests transitions and would leave the machine in
`dispensing` after the error transition completes. Because the loop is
single-threaded and `_process_payment` → dispense runs synchronously, a
deletion cannot interleave after this check.

### 6. Testing

- `load_config` first run: missing file → returns model, file written
  (valid JSON, empty products, real-value serialization), process does NOT
  exit; invalid existing file still exits.
- `ConfigModel()` default: `products == []`.
- `delete_product`: removes existing SKU and saves; unknown SKU returns
  False without saving.
- `InventoryManager.remove_sku`: removes count+tracking, persists;
  unknown SKU harmless.
- Delete route: requires auth (401 unauthenticated); deletes and re-renders
  table without the row; unknown SKU returns 200 with table unchanged.
- Add route now registers the SKU with the inventory manager.
- Empty-state row renders when catalog is empty.
- Mid-session delete: select → pay → delete product → dispense attempt
  lands in `error` with full escrow refunded (customer made whole).

## Files touched

| File | Change |
|---|---|
| `main.py` | first-run continue path; remove `_generate_skeleton` exit; wire `set_inventory_manager` |
| `config/config_model.py` | empty products default |
| `services/config_store.py` | `delete_product` |
| `services/inventory_manager.py` | `remove_sku` |
| `web_interface/routes.py` | delete route, inventory-manager setter, add-route inventory registration |
| `web_interface/templates/partials/inventory_table.html` | delete button, empty state |
| `controller/vmc.py` | dispense-time product-existence guard with refund |
| tests | per §6 |
