# Web dashboard (`ice_edge.web`)

Scoped `CLAUDE.md`, loaded when working under `packages/edge/src/ice_edge/web/`. Moved from the root `CLAUDE.md`; project-wide rules stay there.

### Web Dashboard (`ice_edge/web/`)

FastAPI app (`server.py`) with Jinja2 templates and HTMX-driven partials. `routes/` is a package with one module per area (`home.py`, `health`, `products`, `inventory.py`, `reports`, `controls.py`, `tests_level`, `users`, `auth`, `settings`); `routes/__init__.py` wires them all with `attach_routes`. `tests_level/`, `users/`, `auth/`, `health/`, `settings/`, `products/`, and `reports/` are packages whose `__init__.py` builds the area router from sub-routers (`tests_level`: `overview`, `log`, `sale`, `lease`, `commands`; `users`: `ownership`, `devices`, `accounts`; `auth`: `login`, `setup`; `health`: `overview`, `subsystems`, `faults`, `recovery`, `availability`, `logs`; `settings`: `secrets` (shared masking helpers, no router), `overview`, `machine`, `contacts`, `payments`, `comms`, `mqtt`, `web`, `reports`; `products`: `helpers` (shared helpers, no router), `catalog`, `placement`, `overview`; `reports`: `helpers` and `compute` (shared helpers, no router), `overview`, `levels`, `email`), included in that order (for `tests_level` and `users` the last sub-router holds the parametrised paths, which would otherwise shadow earlier static ones; `auth`'s two do not overlap; `health`, `settings` and `reports` keep their original file order, having nothing to shadow; `products` puts `overview` last because its `GET /products/{sku}` would capture `catalog`'s `GET /products/new`). Every area module exposes a module-level `router` (`auth/`'s is `public`), and its handlers are module-level functions (plain undecorated closures inside a handler or helper are still allowed; the guard flags a nested function only when it carries a decorator); `packages/ui/src/ice_ui/templating.py` owns the shared `Jinja2Templates` singleton, and `tests/meta/test_routes_shape.py` discovers sub-modules recursively, requires every sub-module to expose its own `router` (except a sub-module listed in its `HELPER_MODULES` allowlist, which must expose none and carry no route decorator), and fails on any nested handler or a reintroduced `build_router`. Templates live in `packages/ui/src/ice_ui/templates/` with HTMX partial fragments in `templates/partials/`. Static assets in `packages/ui/src/ice_ui/static/`.

**The v2 shell** (`base.html` + `packages/ui/src/ice_ui/levels.py`) replaces the tabbed dashboard. Every level is a real URL and every level has its own breadcrumb trail and Back button that goes to the parent, not through history. `base.html` defines three blocks: `title` (for `<title>`), `body` (the sole content of `<main>`, where your level goes), and `bar_variant` (the bar header, not to be overridden). The bar itself uses `hx-swap-oob="true"` to deliver an out-of-band swap on every response, so a boosted navigation updates it with no reload. The navigation tree is a hierarchy of 25 named levels plus three parameterized ones (a product SKU, a subsystem name, a user id). Each level is defined in `packages/ui/src/ice_ui/levels.py` as a frozen dataclass with `title`, `url`, `parent`, and properties `crumbs`, `parent_url`, plus a classmethod `Level.child(parent, title, url)` for parameterized levels. The bar shows a signed-in chip (role chip plus name, the name hidden below `md`) linking to `/me` whenever `current_user` is set, so the login, setup and enrol pages have none.

The tree, every URL with the permission that gates it (`Permission.<x>` from `packages/edge/src/ice_edge/access/access.py`; "A or B" is `_require_any(A, B)`, a local OR-semantics dependency defined in `products/helpers.py` and `settings/overview.py` — `web_auth.require()` itself is AND-only):

| URL | Gate |
|---|---|
| `/` | `view_status` (held by every role) |
| `/me` | `view_status` |
| `/health` | `view_status` |
| `/health/subsystems` | `view_status` |
| `/health/subsystems/{name}` | `view_status` |
| `/health/faults` | `view_status` |
| `/health/availability` | `view_status` |
| `/health/logs` | `view_logs` |
| `/products` | `edit_catalog` **or** `edit_placement` |
| `/products/new` | `edit_catalog` |
| `/products/{sku}` | `edit_catalog` **or** `edit_placement` |
| `/products/{sku}/catalog` | `edit_catalog` |
| `/products/{sku}/placement` | `edit_placement` |
| `/products/{sku}/copy` | `edit_catalog` |
| `/inventory` | `edit_placement` |
| `POST /inventory/collect` | `collect_cash` (**all four roles**) |
| `/reports` | `view_reports` |
| `/reports/period` | `view_reports` |
| `/reports/product` | `view_reports` |
| `/reports/product/{sku}` | `view_reports` |
| `/reports/method` | `view_reports` |
| `/reports/collections` | `view_reports` |
| `/controls` | `machine_controls` |
| `/tests` | `run_tests` |
| `/tests/log` | `run_tests` |
| `/tests/sale` | `run_tests` |
| `/tests/{subsystem}` | `run_tests` |
| `POST /tests/sale` | `run_tests` |
| `POST /tests/run-all` | `run_tests` |
| `/tests/standby/confirm` | `run_tests` |
| `POST /tests/standby` | `run_tests` |
| `POST /tests/end` | `run_tests` |
| `POST /tests/takeover` | `run_tests` |
| `POST /tests/runs/{run_id}/verdict` | `run_tests` |
| `POST /tests/{subsystem}/{command}` | `run_tests` (re-checks `TESTABLE_COMMANDS`, 403 if not allowlisted) |
| `/users` | `manage_users` |
| `/users/new` | `manage_users` |
| `/users/{id}` | `manage_users` |
| `/devices` | `manage_users` |
| `/users/codes` | `manage_ownership` |
| `/users/ownership` | `manage_ownership` |
| `/settings` | `edit_contacts` **or** `edit_secrets` |
| `/settings/machine` | `edit_contacts` |
| `/settings/contacts` | `edit_contacts` |
| `/settings/payments` | `edit_secrets` |
| `/settings/comms` | `edit_secrets` |
| `/settings/mqtt` | `edit_secrets` |
| `/settings/web` | `edit_secrets` |
| `/settings/reports` | `edit_contacts` |
| `POST /health/faults/PAY-104/record-sale` | `clear_faults` (two-tap confirm) |
| `POST /health/faults/PAY-104/discard` | `clear_faults` (two-tap confirm) |

`/devices` sits under Users in this **navigation** tree (`LEVEL_DEVICES`'s `parent` is `LEVEL_USERS` in `packages/ui/src/ice_ui/levels.py`) even though its URL is not under `/users/` — the tree is a navigation hierarchy, not a URL-prefix hierarchy. Every mutating route under these levels additionally requires `Depends(context.require_htmx)`.

`packages/edge/src/ice_edge/web/context.py` holds shared state (config, VMC, health monitor, access store, availability, inventory manager) and helpers: `template_context(request, level=None, **extra)` injects request, current_user and perms into every template, `require_htmx` guards all POST routes (the CSRF dependency), `tail(file_path, lines=50)` reads log tails, `health_snapshot()` is the single health predicate shared by `/status` and `/pill` (so the hero and pill never disagree), and `LOW_STOCK_THRESHOLD = 3` (a tracked product is "low" at or below this count; not configurable per-product yet).

The dashboard uses cookie-based session auth via `packages/edge/src/ice_edge/access/access.py`'s `AccessStore`,
persisted in `data/access.json` (mode 0600). Users have four roles (`owner`,
`secretary`, `tech`, `loader`) and a 4–8 digit PIN. First login on a new device
requires a second factor: a 6-digit OTP sent by email (when
`communication.email_gateway` is configured) or an 8-digit emergency code,
which works offline. On first boot, setup mode redirects every route to `/setup`
behind a code that exists only at the machine. The `Backoff` class enforces
exponential back-off per `(kind, subject, client)` tuple plus a per-user budget
for untrusted clients — no hard caps. Every route is gated by `require(Permission)`;
templates receive `perms` and `current_user` via `template_context` so the server
does not render controls it would refuse. POST routes require the `HX-Request`
header (HTMX's own requests set it), which blocks a plain cross-site form post
as a CSRF guard. Session, device and enrol cookies are `Secure` only when the hop was TLS (`is_https` honours `X-Forwarded-Proto` solely from a trusted proxy), so a plain-HTTP deployment relies on the machine's LAN being trusted. `/static` is mounted without auth (Starlette mounts take no dependency; `web/server.py` carries the NOTE); it serves only app assets, and nothing operator-specific may ever be placed there. The only fragment endpoints are `/status` (home hero, 1 s), `/kpi` (home KPIs, 60 s), `/pill` (health indicator, 5 s), and `/health/subsystems/{name}/live` (one board's live window, 2 s); lists and forms load once and refresh on action. All four are exempt from the session idle clock via `is_polling_path` (`packages/edge/src/ice_edge/web/auth.py`) — the fixed `POLLING_PATHS` set (`/status`, `/kpi`, `/pill`) plus a regex matching the per-board `/live` pattern.

The Home level shows a status strip (hero showing the next scheduled event or the health summary) plus a tile grid of eight tiles: Health, Products, Inventory, Reports, Controls, Tests, Users, Settings. A tile is rendered only when `perms` intersects that tile's permission set; the grid adapts to screen width (4×2 on desktop, 2×4 on tablets, 1 column on phones). Each tile links to its level, and the pill in the top-right taps to `/health/faults`.

The bar carries a context badge (`#context-badge`, `base.html`, truncated at `max-w-[10rem]` with the full label as its `title`): `template_context` injects `badge`, built by `context_badge()` (`ice_edge.web.context`). The label is `physical.common_name`, falling back to `machine_id`; it links to `{cloud.url}/m/{short_fingerprint}` with `hx-boost="false"` only when `cloud.enabled` and the uplink identity exists, else it renders as a plain chip. The login, setup and enrol pages show none because the badge lives in the `bar_variant` block. Caveat: the link reuses `config.cloud.url`, the uplink's API base, so it works only from a browser that can reach that URL — fine for the public droplet, a dead link for a LAN-only or docker-internal uplink URL.

Tailwind v3.4.17 and HTMX 1.9.10 are **vendored** in `static/app.css` and `static/htmx.min.js`, so the tablet operator's dashboard works with no internet — that claim covers the dashboard proper; the customer-facing `/screen` page (and `/screen/body`) is the one exception and keeps CDN references. Every page extending `base.html` gets global press (`:active`), busy (`.htmx-request`), focus-visible and checked styling from `@layer base` in `tailwind.input.css` with no per-element classes needed, plus a click sound played from `static/feedback.js` (`static/click.wav`, generated by `scripts/make_click_wav.py` — regenerate rather than hand-edit). The Tailwind config sets `future.hoverOnlyWhenSupported` so hover-only styles don't stick after a touch tap. Secrets on Settings pages are masked: a placeholder is rendered for a field that is already set, and posting the unchanged placeholder leaves the stored value alone. The MQTT page shows the effective value and disables the field when an env override is active, and never writes an env value into `config.json`. Machine id, web host and web port are displayed but not editable — the handlers accept and ignore those fields even if a direct POST supplies them.

`partials/confirm_button.html` (Task 5) is the server-rendered two-tap confirm used by Controls, the Health fault Clear and the Users code Regenerate, with **no JavaScript state**. Parameters: `label`, `confirm_label`, `post_url`, `target` (an `#id` selector), `confirm_url`, and optional `confirming` (default false). The first tap `hx-get`s `confirm_url`, which the including route renders in its confirming state; the second tap `hx-post`s `post_url`. Cancel re-`hx-get`s `confirm_url` with `confirming=false` — so **any route backing a confirm button must honour that query parameter**: absent or anything but the literal string `"false"` renders the Confirm/Cancel pair, `"false"` renders the plain first-tap button.

`POST /inventory/{sku}/adjust` (the Inventory restock level, `packages/edge/src/ice_edge/web/routes/inventory.py`) takes one form field, `delta`, accepting exactly `-10`, `-1`, `1`, `10` (a loader tapping the four adjust buttons); anything else — including a non-integer string — returns **400** with the stored count unchanged. A valid delta **clamps the count at zero** rather than erroring, so tapping `-10` on a count of 3 lands on 0 — deliberately different from the Products placement form (`POST /products/{sku}/placement`), which *rejects* a typed negative count outright and leaves the stored value unchanged.

The Health level is split across six sub-levels rather than one merged page. `/health` itself shows only a four-tile summary (Subsystems, Faults, Availability, Logs) plus the VMC's own build identity from `packages/common/src/ice_common/build_info.py` (image env vars set by CI, or `git` when run from a checkout). `/health/subsystems` and `/health/subsystems/{name}` show heartbeats (liveness, uptime) and each subsystem's retained `capabilities/<subsystem>` document (`SubsystemCapabilities`: firmware, contract version, brand/model, hardware_id, ip) — subsystems in `EXPECTED_SUBSYSTEMS` are listed even before they speak. `/health/subsystems/{name}` (subsystem-windows design) renders the identity card as before, then a `#live` wrapper (`health_subsystem.html`) that owns every htmx attribute — `hx-get`, `hx-trigger="load, every 2s"`, `hx-target="this"`, innerHTML swap — around `partials/subsystem_live.html`, which itself carries no `hx-` attribute; `GET /health/subsystems/{name}/live` re-renders just that fragment, shared with the page route through `_window_context` (`packages/edge/src/ice_edge/web/routes/health/subsystems.py`) so the first paint and every poll agree. The window renders only the channels the board declares in its retained `capabilities/<name>` document (`ChannelDescriptor.direction` input/output, `driven_by`) — Inputs and Outputs sections, each either a row per declared channel or "Nothing declared by this board." Digital inputs render as circles (`rounded-full`) and digital outputs as squares (`rounded-md`), tinted green (on), red (off), or gray (board not alive, or the channel never reported); analog (temperature/counter) rows are gated the same way and show value, unit, and (for temperature) an OK/Out-of-range word. Dwell time ("on for 12 s" / "off since VMC start") comes from `HealthMonitor.record_signal`'s in-memory transition tracking — "since VMC start" until the first observed change, reset on every VMC restart. A reading is attributed to a board by `HealthMonitor.declaring_subsystem` (matched by channel id, and kind when given) scanning stored capabilities; a reading no board declares is still consumed for control but never shown. An output or input whose `driven_by` names a command the VMC would currently refuse is marked inhibited (`ring-dashed` plus the word "inhibited") via `Availability.command_inhibited` for `dispense`/`water_valve`/`set_policy`. The view model is built by the pure helper `packages/ui/src/ice_ui/views/subsystem_window.py`'s `build_window`. Channel ids are unique within a board (the ice maker's binary output is `compressor_run`, derived from `power_on`/`power_off` events, precisely because `compressor` was already its discharge-temperature input), but `declaring_subsystem` still matches by `(id, kind)` rather than id alone so that two different boards declaring the same literal id under different kinds can never steal each other's readings. `/health/faults` lists active faults with a Clear action (gated on `clear_faults`, the two-tap confirm above); `/health/availability` shows the permissive truth table and payment-blocking reasons; `/health/logs` (gated on `view_logs`) shows the last **50** lines of `LOGS/vmc.log` (`context.tail`), not the pre-v2 fragment's 10.

The Tests level (`packages/edge/src/ice_edge/web/routes/tests_level/`) lets a tech prove a subsystem works without making a real sale, and the machine cannot sell while they do it. `/tests` shows one card per `EXPECTED_SUBSYSTEMS` entry plus the service-state card (always rendered), but is itself read-only — the GET routes (`/tests`, `/tests/log`, `/tests/sale`, `/tests/{subsystem}`) never call `machine.lease.begin_maintenance`; only a POST action acquires the lease. `POST /tests/{subsystem}/{command:path}` runs one command test after re-checking `command in testable_commands(subsystem, row["commands"])` — the intersection of `ice_contracts.board.common.TESTABLE_COMMANDS` (below) with what that subsystem's live capabilities doc advertises — against a **freshly re-read** `_subsystem_summary()`, never trusting which buttons the client was shown; an unlisted command (`set_policy`, `cash_sale`, `set_interval`) is refused with **403** before the lease is ever touched or the dispatcher ever called. `POST /tests/run-all` runs `ping` then `self_test` against every alive subsystem **in sequence** (one `await` at a time, never `asyncio.gather`, so two subsystems' commands can never interleave on the wire). `POST /tests/sale` runs `machine.test_sales.run_test_sale` for a form-submitted SKU; `POST /tests/end` and `POST /tests/takeover` release or transfer the caller's lease; `POST /tests/runs/{run_id}/verdict` records pass/fail plus a note on a `test_run` log row via `EventRecorder.update_metadata`. `TESTABLE_COMMANDS` (`packages/contracts/src/ice_contracts/board/common.py`) is a **server-side allowlist**, per subsystem, of exactly the standard commands (`ping`, `self_test`, `force_report`) plus that subsystem's actuator commands — it is deliberately narrower than what a subsystem's capabilities document may advertise, so a control command like `set_policy` or `cash_sale` can never be invoked from the Tests tile even if it shows up in capabilities. The allowlist re-check on POST is the actual security boundary here, not which buttons the template renders — a crafted request to an unlisted command still gets 403 from the server, never a silent 404 that would look like the command doesn't exist. When any test is refused because the machine is busy, `_acquire_lease_or_refusal` (`packages/edge/src/ice_edge/web/routes/tests_level/lease.py`) remaps the two opportunistic-lease refusals ("machine is mid-sale", "credit is still on the machine") to "machine is busy — take it out of service first", pointing the operator at the `POST /tests/standby` button; the "held by <id>" wording stays unchanged. The Home hero shows "Out of service — maintenance by <name>" (via `context.health_snapshot()`'s `maintenance` field, packages/edge/src/ice_edge/web/context.py:280) when `SVC-102` is active and a holder exists, without changing `is_healthy`.
