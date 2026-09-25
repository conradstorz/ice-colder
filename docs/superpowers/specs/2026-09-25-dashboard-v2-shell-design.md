# Dashboard v2 Shell — Design

**Date:** 2026-09-25
**Status:** Approved
**Series:** Dashboard v2, part 2 of 4 (roles → **v2 shell** → sales reports → system tests)
**Depends on:** `2026-09-25-roles-and-access-design.md` (permissions, `_ctx`, keypad partial, login pages)

## Context

The current dashboard is a single desktop page with a tabbed content panel.
v2 targets a shared 7-inch touchscreen inside the machine first, personal
phones second, desktops last. Home shows operational status and a grid of
tiles; each tile opens a sub-menu that owns the whole screen except a
navigation bar with Home, Back, and Lock. Levels nest as deep as an area
needs.

Decisions made in brainstorming:

- Home is a status strip on top with the tile grid below (layout A).
- Inside a tile the level gets the full screen except a top bar carrying
  Home, Back, breadcrumb, a live health pill, and Lock.
- Eight home tiles: Health, Products, Inventory, Reports, Controls, Tests,
  Users, Settings. A tile is hidden when the role holds none of its
  permissions. Reports and Tests are placeholders until parts 3 and 4.
- Loaders see catalog fields read-only, not hidden.
- Every level has a real URL; Back means parent in the tree, not browser
  history.
- Tailwind and HTMX are vendored into `static/` so the machine tablet works
  with no internet.
- Must work in 7-inch landscape (1024×600), 7-inch portrait (600×1024), and
  phone width. Desktop gets the tablet layout centered.
- Only the Home strip and the health pill poll. Lists and forms load once and
  refresh on action.

## Approach

One `base.html` renders the bar; every level extends it and declares its
`level` (title, parent URL, crumbs). `hx-boost` on the body makes tile taps
and Back swap `<main>` and push the URL, so navigation is server-rendered
pages with no flash. Existing partials become level bodies, restyled.

Rejected: keeping one page and wrapping partials in chrome over HTMX (no
URLs, no reload-to-place); a client-side router (JavaScript beyond HTMX).

## 1. Shell

### 1.1 `base.html`

- `<meta name="viewport" content="width=device-width, initial-scale=1">`.
- `static/app.css` (compiled Tailwind, §3) and `static/htmx.min.js`
  (pinned 1.9.x, same major as today). No CDN references anywhere.
- `<body hx-boost="true" hx-target="main" hx-swap="innerHTML show:top">`.
  Boosted requests carry `HX-Request`, so the existing CSRF guard on POSTs
  keeps working. Full-page requests (reload, bookmark) render the same
  template with the bar.
- **Bar** (56 px, dark): left to right
  - `Home` button (`hx-get="/"`), hidden on Home.
  - `Back` button to `level.parent_url`, hidden on Home.
  - Breadcrumb: `level.crumbs`, a list of `(title, url)`; the last entry is
    the current level and is not a link. On phones only the last two crumbs
    show.
  - **Health pill**: `<span id="pill" hx-get="/pill" hx-trigger="load, every 5s" hx-swap="outerHTML">`.
    Green `OK` when `is_healthy`; red with the highest-priority active fault
    code otherwise (the same ordering `/status` uses); grey `…` before first
    load. Tapping it navigates to `/health/faults`. The pill is outside
    `<main>` so it survives every swap.
  - `Lock` button: `hx-post="/logout"` then full navigation to `/login`.
    Present on every level including Home.
- `<main>` holds the level body.

### 1.2 Level context

`web_interface/levels.py` defines:

```python
@dataclass(frozen=True)
class Level:
    title: str
    url: str
    parent: "Level | None"
    @property
    def crumbs(self) -> list[tuple[str, str]]: ...
    @property
    def parent_url(self) -> str: ...
```

and a static tree of the levels in §2. Routes pass `level=` through the
`_ctx(request, level, **extra)` helper from part 1, which also injects
`perms` and `current_user`. A level whose URL contains a parameter (a SKU, a
subsystem name) is built with `Level.child(parent, title, url)` at request
time.

### 1.3 Home (`/`)

- **Status strip** (top ~35 % of the screen in landscape):
  - Hero polling `/status` every 1 s: the existing healthy / issues card,
    with machine state, escrow, and payment enabled, and the active fault
    list in the issue state. Unchanged data, restyled for touch.
  - Four KPI cards polling `/kpi` every 60 s: Money in, Vends, Errors,
    Uptime, 24-hour window. The 24 h / 7 d / 30 d activity table moves to
    Reports.
- **Tile grid**: the eight tiles from §2, each a 48 px-minimum button with
  an icon (inline SVG), a title, and one line of live context (Health:
  fault count; Products: product count; Inventory: products below their
  low-stock line, or "tracking off"; Users: user count; the others static).
  Tile context comes from the route, not from polling. A tile whose
  permissions the role lacks entirely is not rendered; Reports and Tests
  render with a "coming soon" style when the role has their permission.

Tile visibility rule: a tile is shown when `perms` intersects the tile's
permission set (§2 table). The grid re-flows: 4×2 landscape, 2×4 portrait,
1 column on phones.

## 2. URL tree and gates

Permissions are those defined in part 1. "Gate" is the minimum permission to
open the level; actions inside may require more and are hidden otherwise.

| Level | URL | Gate | Body |
|---|---|---|---|
| Home | `/` | any session | §1.3 |
| Health | `/health` | view_status | Four sub-tiles: Subsystems, Faults, Availability, Logs (Logs shown only with view_logs); summary counts on each |
| · Subsystems | `/health/subsystems` | view_status | One card per `EXPECTED_SUBSYSTEMS` entry: alive/stale, uptime, firmware, contract version; tap opens the subsystem |
| · · Subsystem | `/health/subsystems/{name}` | view_status | Identity fields (brand, model, hardware id, ip, firmware, contract), heartbeat age, temperature ranges where the subsystem reports them |
| · Faults | `/health/faults` | view_status | Active faults with age and gate class (safety / fulfillment / alert); Clear button per fault with clear_faults, two-tap confirm |
| · Availability | `/health/availability` | view_status | The permissive table as stacked cards: payment enabled, per-kind availability, blocking reasons |
| · Logs | `/health/logs` | view_logs | Last 50 lines, monospace, with a Refresh button (no polling) |
| Products | `/products` | edit_catalog or edit_placement | Row per product: name, price, slot, count, lock badge; Add button with edit_catalog |
| · New | `/products/new` | edit_catalog | Add form (catalog and placement fields together, since the creator owns both) |
| · Product | `/products/{sku}` | edit_catalog or edit_placement | Read-only summary with two sub-tiles: Catalog, Placement; Copy and Delete buttons with edit_catalog |
| · · Catalog | `/products/{sku}/catalog` | edit_catalog | Name, price, kind, SKU form. Loaders and techs reach `/products/{sku}` and see these values read-only; this level returns 403 for them |
| · · Placement | `/products/{sku}/placement` | edit_placement | Slot/button, inventory count, tracking toggle |
| · · Copy | `/products/{sku}/copy` | edit_catalog | Prefilled add form |
| Inventory | `/inventory` | edit_placement | Restock view: one row per tracked product with the count, `−10 −1 +1 +10` buttons posting `/inventory/{sku}/adjust`, and a slot field. Untracked products listed at the bottom without buttons |
| Reports | `/reports` | view_reports | Activity table with 24 h / 7 d / 30 d selector (`?period=`), moved from Home. Part 3 extends this level |
| Controls | `/controls` | machine_controls | Three large buttons; first tap turns the button into "Confirm restart?" with Cancel, second tap posts. Result message inline |
| Tests | `/tests` | run_tests | Placeholder text until part 4 |
| Users | `/users` | manage_users | People list; Add; sub-tiles Devices, Emergency codes, Ownership (owner only) |
| · Person | `/users/{id}` | manage_users | Edit name/email/role, Disable/Enable, Reset PIN, Delete; owner row read-only for secretaries |
| · New | `/users/new` | manage_users | Part 1 form |
| · Devices | `/devices` | manage_users | Part 1 list: label, shared toggle, trusted users, last seen, Forget |
| · Emergency codes | `/users/codes` | manage_ownership | Unused count, Regenerate (two-tap), shows new codes once |
| · Ownership | `/users/ownership` | manage_ownership | Machine report (email), Transfer ownership form (PIN + code) |
| Settings | `/settings` | edit_contacts or edit_secrets | Sub-tiles: Machine, Contacts, Payments, Comms, MQTT, Web |
| · Machine | `/settings/machine` | edit_contacts | Name, location, machine id (read-only), notes; edit form |
| · Contacts | `/settings/contacts` | edit_contacts | Owner and other people: name, email, phone, address, preferred channel; edit form |
| · Payments | `/settings/payments` | edit_secrets | Stripe, PayPal, MDB settings with secrets masked; edit form |
| · Comms | `/settings/comms` | edit_secrets | Email, SMS gateways masked; edit form; "Send test email" button |
| · MQTT | `/settings/mqtt` | edit_secrets | Broker host, port, username, TLS; password masked; note that env overrides win |
| · Web | `/settings/web` | edit_secrets | Host, port (read-only, restart to apply), trusted proxies |

Settings edit forms write through `services/config_store.save_config`
after mutating the live `ConfigModel`, the same path products use today.
Machine id, host, and port are shown but not editable. The MQTT page shows
the effective value when an env override is active and disables the field.

Removed routes: `GET /` old dashboard body, `/config/*`, `/inventory/new`,
`/inventory/copy/{sku}`, `/inventory/edit/{sku}`, `/inventory/update/{sku}`,
`/inventory/delete/{sku}`, `/inventory/add`, `/activity`, `/logs`, `/health`
as a fragment, `/action/{command}`. Their replacements are the levels above;
POST endpoints keep the same names under the new prefixes
(`/products/{sku}/delete`, `/controls/restart`, `/health/faults/{key}/clear`).
`/status`, `/kpi`, and the new `/pill` are the only fragment endpoints.
`/screen` and `/screen/body` are unchanged.

## 3. Layout and styling

- **Tailwind, compiled.** `web_interface/tailwind.config.js` scans
  `web_interface/templates/**/*.html`; `web_interface/static/app.css` is the
  committed output. The build command is documented in CLAUDE.md and runs
  with the standalone Tailwind CLI binary (no Node project). A test asserts
  every class used in templates exists in `app.css` so a forgotten rebuild
  fails CI rather than shipping an unstyled tile.
- **Breakpoints.** `lg` (≥ 900 px): landscape tablet and desktop, 4×2 tile
  grid, strip and grid side by side vertically as in layout A. `md`
  (600–899 px): portrait tablet, 2×4 grid, hero and KPIs stack. Below 600 px:
  phones, one column, tiles are full-width rows, breadcrumb shows two crumbs.
  Desktop centers a 1024 px-wide layout.
- **Touch.** Every button and row is at least 48 px tall with 8 px gaps.
  Font sizes: body 16 px, tile title 20 px, hero 24 px. No hover-only
  affordances.
- **Lists not tables.** The health subsystem, availability, product, and
  user tables become card rows that wrap; a table is used only inside the
  Reports activity view and gets `overflow-x: auto`.
- **Numeric entry.** Price, count, slot, and PIN fields use the keypad
  partial from part 1 (`inputmode="numeric"` as the fallback); text fields
  use the device keyboard.
- **Feedback.** Actions reply with a swapped row or an inline message under
  the button; no toasts, no modals.
- **Light theme only**, tokens from the 2026-04-17 spec (slate-50 page, white
  cards, green/amber/red status), with the bar in slate-800.

## 4. Migration

- `dashboard.html` is deleted along with its tab row, content panel, activity
  panel, and controls bar.
- Existing partials are moved and restyled: `status_fragment` (hero, kept as
  a fragment), `kpi_fragment` (kept), `health_fragment` (split into the four
  Health levels), `inventory_table` (becomes the Products list and the
  Inventory restock view), `inventory_add_form` (Products › New and Copy),
  `activity_fragment` (Reports), `logs_fragment` (Health › Logs),
  `machine_info` and `contacts` (Settings pages). Templates for
  `/config/payments` and `/config/comms`, which never existed, are written as
  Settings pages.
- Part 1's login, enrollment, setup, and Users templates adopt `base.html`;
  login and setup pages use a variant bar with no Home/Back/Lock.
- `routes.py` is split by area into `web_interface/routes/` (`home.py`,
  `health.py`, `products.py`, `inventory.py`, `reports.py`, `controls.py`,
  `users.py`, `settings.py`, `auth.py`), each exposing an `APIRouter` that
  `server.py` includes. Shared setters and `_ctx` move to
  `web_interface/context.py`. The current 486-line single file would double
  otherwise.

## 5. Error handling

- A level whose backing object is gone (deleted SKU, unknown subsystem)
  renders a 404 page inside the shell with a Back button, never a bare JSON
  404.
- 403 renders "You don't have access to this" inside the shell.
- Fragment endpoints (`/status`, `/kpi`, `/pill`) tolerate missing services
  exactly as today (neutral state when the VMC or monitor is unwired).
- `save_config` failures on Settings return the form with the error text and
  leave the in-memory model as the user submitted it, matching current
  product-save behavior.

## 6. Testing

- `tests/test_levels.py`: crumbs and parent URLs for the static tree and for
  parameterized children.
- `tests/test_web_routes.py`: every level in §2 renders 200 for a role that
  holds its gate and 403 for one that does not; the bar contains Home and
  Back except on Home; the breadcrumb text matches the level; tiles on Home
  are filtered per role (loader sees exactly Health, Products, Inventory);
  Products › Catalog is 403 for loader while `/products/{sku}` shows price
  read-only; Inventory adjust changes counts through `InventoryManager`;
  Controls require two POSTs (confirm then act); Settings forms round-trip
  through `save_config` with a temp config path; `/pill` reports OK and a
  fault code.
- `tests/test_static_css.py`: every class token in templates is present in
  `app.css`.
- Existing status, KPI, fault, and screen tests are kept, retargeted to the
  new URLs.

## 7. Files

| File | Change |
|---|---|
| `web_interface/templates/base.html` | New shell |
| `web_interface/levels.py`, `web_interface/context.py` | New |
| `web_interface/routes/*.py` | Split from `routes.py` (removed) |
| `web_interface/templates/{home,health,health_subsystems,health_subsystem,health_faults,health_availability,health_logs,products,product,product_catalog,product_placement,product_form,inventory,reports,controls,tests,users,user,user_form,devices,users_codes,users_ownership,settings,settings_*}.html` | New level templates |
| `web_interface/templates/partials/{status_fragment,kpi_fragment,pill,tile,keypad,confirm_button}.html` | Kept or new fragments |
| `web_interface/templates/dashboard.html` and old partials listed in §4 | Removed |
| `web_interface/static/app.css`, `static/htmx.min.js` | Vendored |
| `web_interface/tailwind.config.js`, `web_interface/tailwind.input.css` | Build inputs |
| `CLAUDE.md`, `README.md` | Document the level tree, the CSS build, and the removed routes |
| `tests/test_levels.py`, `tests/test_static_css.py`, `tests/test_web_routes.py` | New and rewritten |

## 8. Out of scope

- Sales reports content beyond the moved activity table (part 3).
- Any test actions (part 4).
- Dark theme, animations, offline caching of pages (service worker).
- Changes to `/screen`.
- Editing machine id, web host, or port from the dashboard.
