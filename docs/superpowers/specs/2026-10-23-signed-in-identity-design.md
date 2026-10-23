# Signed-in identity in the dashboard shell

**Date:** 2026-10-23
**Status:** approved design, not yet planned

## Goal

Every page of the operator dashboard, whether served by the machine
(`ice_edge.web`) or the cloud portal (`ice_cloud.portal`), shows who is signed
in and under what role, and offers one place (`/me`, the "You" level) that
spells out what that role may do here. The experience is platform-agnostic:
the same chip, the same page, the same permission wording on both sides. Where
the person is (local or remote) matters only to what they may do, which is
already decided by `perms`; this spec changes nothing about permissions or
remote write access. Remote write access is a separate, later spec.

## Non-goals

- No change to `ROLE_PERMISSIONS`, `REMOTE_VIEWER_PERMISSIONS`, or any route
  gate.
- No profile editing (name, email, PIN) on the "You" page.
- No change to the login, setup or enrol pages, which stay chip-free.
- No change to the customer `/screen`.

## 1. Context contract: `current_user`

Both platforms already put a `current_user` object into every template
context. `base.html` starts reading two attributes of it:

| attribute | type | edge value | cloud value |
|---|---|---|---|
| `name` | `str` | `User.name` | Google account display name (`Account.name`) |
| `role_label` | `str` or `None` | `Role.value` (`owner`, `secretary`, `tech`, `loader`) | `owner` / `tech` / `admin` for the selected machine (the rule `require_machine_access` applies, so an admin who is also the owner reads `owner`); `None` on fleet-wide pages (`/fleet`, `/admin/*`) and when no machine is selected |

Implementation:

- `ice_edge.web.auth.TemplateUser` gains a `role_label` property returning
  `self.role.value`. No new fields.
- `ice_cloud.viewer.RemoteViewer` gains `role_label: str | None = None` and
  `email: str | None = None`; `portal._context` fills them from the account
  and the selected machine's role. The portal already resolves the selected
  machine per request; it gains the role lookup (`repo.access_for` plus the
  admin fallback) through one helper in `ice_cloud.auth` that
  `require_machine_access` also uses, so the chip and the access check can
  never disagree.
- `current_user` stays `None` for an unauthenticated request, which is what
  hides the chip on `/login`, `/setup`, `/enroll`.

## 2. Permission wording: `PERMISSION_LABELS`

`ice_contracts.access` gains `PERMISSION_LABELS: dict[Permission, str]`, one
short plain-words phrase per member, for example `view_status` = "See machine
status and health", `collect_cash` = "Record a cash collection",
`manage_ownership` = "Transfer ownership and manage emergency codes". The
"You" page on both platforms renders the viewer's `perms` through this table,
in `Permission` definition order, so the edge and the cloud describe a
permission with the same words. A test pins that every `Permission` member has
a label.

## 3. The bar chip

In `base.html`'s default `bar_variant`, between the health pill and Lock:

```
{% if current_user %}
<a href="/me" class="touch-target px-2 shrink-0 flex items-center gap-1"
   aria-label="Signed in as {{ current_user.name }}">
  {% if current_user.role_label %}
  <span class="inline-flex items-center rounded-full bg-slate-600 text-white text-xs font-semibold uppercase tracking-wide px-2 py-1">{{ current_user.role_label }}</span>
  {% endif %}
  <span class="max-md:hidden text-sm text-slate-200 truncate max-w-[10rem]">{{ current_user.name }}</span>
</a>
{% endif %}
```

- Role chip: same shape as the health pill (`rounded-full`, uppercase,
  `text-xs`), neutral `bg-slate-600` so it never reads as a health state.
- Name hidden below `md` so a phone bar keeps Home / Back / crumbs / pill /
  chip / Lock without overflow; the role chip stays at every width.
- With `role_label` `None`, only the name renders (and only at `md` and up);
  on a phone the link still exists with its `aria-label`, so the tap target
  is kept.
- Lock stays on the bar (one-tap lock on a shared tablet).
- The bar is OOB-swapped on every response, so a boosted navigation always
  shows the current identity; no polling.

The login/setup/enrol variant bars are untouched (they override
`bar_variant`).

## 4. The "You" level: `/me`

- `ice_ui.levels.LEVEL_ME = Level(title="You", url="/me", parent=LEVEL_HOME)`.
- One template `ice_ui/templates/me.html`, extending `base.html`, rendered by
  both platforms with the ordinary context (`current_user`, `perms`, `level`)
  plus two extras: `logout_label` (`"Lock"` on the edge, `"Sign out"` on the
  cloud) and `role_scope` (`None` on the edge; on the cloud the selected
  machine's display name, or `None` when none is selected). Content, top to
  bottom:
  1. Name (hero size) and email when `current_user.email` is set.
  2. Role line. Edge (`role_scope` `None`, `role_label` set): "Role: owner".
     Cloud with a machine selected: "tech on <role_scope>". Cloud with no
     machine selected (`role_label` `None`): "No machine selected" with a
     link to `/fleet`. The machine display name is the stored report's
     `machine_id` when a report exists, else the short fingerprint.
  3. "What you can do here": a `<ul>` of `PERMISSION_LABELS[p]` for each `p`
     in `perms`, in `Permission` definition order; the remote viewer's single
     `view_status` entry renders the same way. Empty `perms` renders
     "Nothing on this page yet."
  4. "Last signed in": `current_user.last_login_at` when the attribute exists
     and is set (edge only; the template uses `is defined`).
  5. A button `hx-post="/logout"` labelled `logout_label`. Both `/logout`
     routes already exist and already require `HX-Request`.
- Edge route: `GET /me` in a new `ice_edge/web/routes/me.py` (module-level
  `router`, gated on `require(Permission.view_status)`, wired by
  `routes/__init__.py`).
- Cloud route: `GET /me` in `ice_cloud.portal`, `require_signed_in`, placed
  before the `/{path:path}` catch-all, rendering the installed UI's `me.html`
  through the same `_resolve`/`_context` path as `/health`, so a trusted
  machine bundle renders its own `me.html` and the remote banner appears when
  a machine is selected.
- Nothing on the page mutates state except the existing logout.

## 5. Tests

Edge (`tests/edge/web/test_me.py` plus small additions where noted):

- For each of the four roles: `/` renders the chip with the role label and
  the name, inside `#bar`, linking to `/me`.
- `/login` and `/setup` (setup mode) render no chip.
- `/me` is 200 for every role, lists exactly `ROLE_PERMISSIONS[role]`'s
  labels and no others, shows the name, and carries a `hx-post="/logout"`
  button labelled "Lock".
- `/me` redirects to `/login` when signed out (existing gate behaviour).
- `LEVEL_ME` is in the level tree with parent Home; the existing bar-markup
  assertions (`tests/edge/web/test_routes_tests_level.py`) still pass.
- `tests/ui/test_static_css.py` passes after `app.css` is rebuilt.

Cloud (`tests/cloud/test_me.py`):

- With a machine selected, the chip shows `owner`, `tech` or `admin`
  according to `access_for`, and an admin who is also the owner shows
  `owner`.
- On `/fleet` the chip shows the name alone (no role span).
- `/me` is 200 and renders the shared template (never `not_remote.html`),
  with the role line naming the selected machine, the `view_status` label,
  and a "Sign out" button; with no machine selected it says so and links to
  `/fleet`.
- `tests/cloud/test_ui_parity.py` is unchanged: the bar is outside `<main>`.

Contracts (`tests/contracts/test_access_labels.py`): every `Permission`
member has a non-empty `PERMISSION_LABELS` entry and the labels are distinct.

## 6. Out of scope, recorded

Remote users acting with their real role (edits, commands) from the cloud,
with only on-site-dangerous actions (tests, dispense) gated to a local
session, is the longer-term direction this spec is designed not to block:
the chip and `/me` already show the real role on both sides, so widening
`REMOTE_VIEWER_PERMISSIONS` later changes what `/me` lists without touching
this feature. That widening needs its own spec (command path from cloud to
machine, audit, and the local-only gate).
