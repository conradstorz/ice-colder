# Admin-hat deferred minors

Branch `cloud/admin-hat-minors`. Four small independent tasks deferred from the
cloud admin-hat feature's final review (spec
`docs/superpowers/specs/2026-10-24-cloud-admin-hat-design.md`).

## Global constraints

- Run tools as `uv run pytest tests/cloud` and `uv run ruff check --fix .` /
  `uv run ruff format .` — never the global ruff (older, reformats unrelated files).
- Cloud templates are in the Tailwind content scan and `app.css` is committed;
  **use only CSS classes that already appear in templates** so no Tailwind
  rebuild is needed. `data-*` attributes and `id` attributes are free.
- `tests/ui/test_static_css.py` fails if a template uses a class the committed
  `packages/ui/src/ice_ui/static/app.css` lacks.
- No behavior change may leak to the edge dashboard: the edge never sets
  `hat_switch`, and `tests/cloud/test_ui_parity.py` must keep passing.
- Admin pages never link to machine pages (`/m/...`, `/machines/...`) — an
  admin is not a machine role.

## Task 1 — `admin_switch` helper

The spec (§ file table, `portal.py` row) named one helper
`admin_switch(settings, account) -> dict | None`; the implementation instead
duplicated `ADMIN_SWITCH if is_admin(request.app.state.settings, account) else None`
in `packages/cloud/src/ice_cloud/portal.py` `_context` (line ~140) and
`packages/cloud/src/ice_cloud/claim.py` `_page` (line ~65).

- Add `admin_switch(settings, account) -> dict | None` to
  `packages/cloud/src/ice_cloud/viewer.py`, next to the `ADMIN_SWITCH` /
  `MY_MACHINES_SWITCH` constants it returns from. It may import `is_admin`
  from `ice_cloud.auth` (auth does not import viewer; verify no cycle).
- Replace both duplicated expressions with calls to it.
- Tests: no new test needed; `tests/cloud/test_admin_hat.py` and
  `tests/cloud/test_claim_page.py` (if present) must pass unchanged.

## Task 2 — stable hat-switch marker

`tests/cloud/test_admin_hat.py:27` distinguishes the hat-switch link from a
breadcrumb by the class tail `'shrink-0">Admin</a>'` — brittle against any
styling change.

- In `packages/ui/src/ice_ui/templates/base.html`, give the hat-switch anchor
  (the `{% if current_user and hat_switch %}` link, line ~79) a stable
  `id="hat-switch"`, placed so a test can extract the element unambiguously.
- Re-key every assertion in `tests/cloud/test_admin_hat.py` that currently
  matches `>Admin</a>`, `>My machines</a>`, or the `shrink-0">Admin</a>` tail
  onto the id — e.g. a helper that extracts the hat-switch anchor from the bar
  and returns its href/label, with `None` when absent. The assertions keep
  their current meanings exactly (which pages show which switch, or none).
- `id` is not a CSS class: no Tailwind rebuild. The edge never sets
  `hat_switch`, so edge pages render no such element.

## Task 3 — stable machine-label marker in admin templates

`tests/cloud/test_admin.py:69` and `tests/cloud/test_admin_dashboard.py:250`
(`_alert_labels`) key on the styling classes `font-mono text-gray-900`.

- Add a `data-machine-label` attribute to the machine-label `<span>`s in
  `packages/cloud/src/ice_cloud/templates/admin_machines.html` (line ~34),
  `admin_alerts.html` (line ~17), and `partials/admin_overview.html` (line
  ~30, the `a.row.label` span — **not** the `edge_build` span at line ~73).
  Keep the existing classes for styling.
- Re-key both tests on the attribute, not the classes. Check for any other
  test matching `font-mono text-gray-900` against **admin** pages and re-key
  those too; edge-template tests (health_subsystem, tests_log, …) are out of
  scope.

## Task 4 — per-machine filter on /admin/alerts

`admin_machines.html` line ~53 links every row's 24 h alert count to the
fleet-wide `/admin/alerts`; the final review wanted the row to land on that
machine's events.

- `Repository.recent_alert_events` (protocol
  `packages/cloud/src/ice_cloud/store/repository.py:314`, impl
  `store/sqlite.py:924`) gains a keyword `fingerprint: str | None = None`;
  when given, only that machine's events are returned (same ordering/limit).
- `GET /admin/alerts` (`packages/cloud/src/ice_cloud/admin/alerts.py`) accepts
  an optional query parameter `machine=<short fingerprint>`. Resolve the short
  form to a machine the same way existing cloud routes resolve
  `/machines/{short}/...` (find and reuse that resolver). An unknown or
  malformed value renders the unfiltered page (no 500, no 404).
- When filtered, the template shows which machine is filtered (label + short)
  and an "All machines" link back to `/admin/alerts`; the footer line reflects
  the filter. Reuse only existing CSS classes.
- `admin_machines.html`'s per-row alert-count link carries
  `?machine={{ m.short }}` (confirm the machines view model exposes the short
  form; `fleet_view.py` builds it).
- Admin templates still link only within `/admin`.
- Tests (in `tests/cloud/test_admin_dashboard.py` or a new file): filtered
  page shows only the named machine's events while another machine's exist;
  unknown `machine` value falls back to fleet-wide; the machines table row
  links to the filtered URL; route stays admin-only (existing guard pattern).
