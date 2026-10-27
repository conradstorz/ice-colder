# Titlebar context badge (edge vs. cloud) — design

**Date:** 2026-10-27
**Status:** approved

## Problem

The dark top bar looks identical whether the operator is logged into the
machine directly (edge dashboard, PIN session) or viewing it through the
cloud portal (Google session, read-only). The only remote indicator today is
`partials/remote_banner.html`, an amber strip fixed to the **bottom** of the
viewport — easy to miss, and it carries no way back to the fleet.

## Decision summary (from brainstorming)

- The bottom banner goes away entirely.
- The top bar gets one **badge** (a chip next to the health pill) that names
  the current context and is clickable:
  - **Edge, direct login:** the machine's own name; clicking it links to the
    cloud portal's deep link for this machine. The browser's *existing* cloud
    session (Google OIDC, 30-day idle) picks it up — no identity federation.
    Edge login stays PIN-only; nothing about edge auth changes.
  - **Cloud, machine selected (remote view):** the machine's name; clicking
    it returns to `/fleet`.
  - **Cloud, no machine selected (fleet-level pages):** the word "Cloud";
    clicking it goes to `/fleet`, which is now ordered most-recently-viewed
    first.
- Remote sessions remain hard-limited to `REMOTE_VIEWER_PERMISSIONS`
  (already true today); a mutating session still requires a PIN login served
  by the machine itself. No new work there.
- The banner's two warnings relocate: **stale** turns the badge amber and
  appends "— offline" (full "last report from X (N ago)" detail in the
  badge's `title`); **UI version mismatch** becomes a one-line strip under
  the bar, cloud-only.

## 1. Shared badge in the bar (`base.html`)

One template variable drives it:

```python
badge = {
    "label": str,          # machine name, or "Cloud"
    "url": str | None,     # None -> plain span, not a link
    "tone": str,           # "local" | "remote" | "offline" | "cloud"
    "title": str | None,   # tooltip; offline detail lives here
    "external": bool,      # True only for the edge -> cloud link
}
```

`base.html` renders it inside `#bar` (the `hx-swap-oob="true"` header)
between the health pill and the hat-switch/signed-in chip, so every boosted
navigation refreshes it with the rest of the bar. When `badge` is absent
(login/setup/enroll pages) nothing renders. Tone → classes:

| tone | look |
|---|---|
| `local` | slate chip (matches the role chip) |
| `remote` | sky chip |
| `offline` | amber chip |
| `cloud` | sky chip |

A badge with a `url` renders as `<a>`; `external: true` adds
`hx-boost="false"` (required for any external origin, per the existing
portal rule — a boosted XHR to another origin dies on CORS).

### Edge side

`ice_edge.web.context.template_context` builds the badge for every level
page:

- `label`: `config.physical.common_name`, falling back to `machine_id`.
- `url`: `{config.cloud.url}/m/{context.machine_identity.short_fingerprint}`
  when `config.cloud.enabled` and `context.machine_identity` is set;
  otherwise `None` (plain, non-clickable name chip). `external: true`.
- `tone`: `"local"` always.

The cloud's `/m/{short}` deep link deliberately lands on `/fleet` with the
machine pre-selected (existing behaviour, kept: a cross-site click never
renders machine content directly), so the edge badge takes the operator to
the fleet list with their machine selected, one tap from its remote view.

### Cloud side

`ice_cloud.portal._context` builds the badge:

- Machine selected (the remote-view pages: `/`, `/status`, `/pill`, `/kpi`,
  `/health`, `/health/faults`, `/me` with a selection): `label` =
  `report.common_name` or `report.machine_id`; `url` = `/fleet`;
  `tone` = `"offline"` when the report is stale (label gains "— offline",
  `title` = "last report from {local time} ({age} ago)"), else `"remote"`.
  On the no-report page a machine IS selected, so it gets the machine badge,
  not "Cloud": with no report to read, `label` falls back to `machine.label`.
- No machine selected (`/fleet`, `/me` without a selection, `/admin*`,
  `not_remote` pages): `label` = `"Cloud"`, `url` = `/fleet`,
  `tone` = `"cloud"`.
- The login page passes no badge (it renders no chip either).

## 2. Banner removal and the mismatch strip

- Delete `partials/remote_banner.html` and the `{% if remote %}` include in
  `base.html`; drop the `remote` key from `portal._context` (and `_remote`'s
  banner-shaped dict).
- UI version mismatch: `_context` passes `ui_notice` ("UI version mismatch:
  machine {digest[:8]}, portal {digest[:8]}") when `ui.mismatch`;
  `base.html` renders it as a one-line amber strip absolutely positioned
  just below the bar, inside `#bar` so it stays OOB-updated. The edge never
  sets `ui_notice`.

## 3. `/fleet` recency ordering

- New cloud table `machine_views(account_id, fingerprint, viewed_at)`,
  primary key `(account_id, fingerprint)`, appended to `MIGRATIONS` (never
  edited in place).
- `Repository` gains `record_machine_view(account_id, fingerprint, at)`
  (upsert) and `machine_views_for_account(account_id) -> dict[str, datetime]`.
- Upsert on both selection paths: `POST /fleet/select/{short}` and
  `GET /m/{short}`.
- `/fleet` sorts rows most-recently-viewed first, never-viewed after,
  stable by label within each group.
- Rows are deleted with the other per-person/per-machine state on unbind and
  owner change (same cleanup path as alert prefs/state); a removed
  membership's stale row is otherwise harmless (the machine no longer lists).

## 4. Tests and housekeeping

- Template/route tests: the three badge states (edge named chip with and
  without a cloud link, cloud machine view incl. stale→amber, fleet-level
  "Cloud"), banner markup gone, mismatch strip renders only when the cloud
  passes `ui_notice`, login/setup pages badge-free.
- Repository tests: upsert semantics, ordering query, cleanup on unbind and
  owner change.
- `/fleet` ordering test: select machine B, list shows B before A.
- Update any existing test asserting `remote_banner` / the `remote` context
  key.
- Parity (`tests/cloud/test_ui_parity.py`) is unaffected: the badge lives in
  the bar, which is outside `<main>`.
- Rebuild and commit `packages/ui/src/ice_ui/static/app.css` if the badge or
  strip introduces classes the committed file lacks.
- No contract bump: `common_name` already rides `MachineReport` since 0.4.0;
  nothing new crosses the wire.

## Out of scope

- Identity federation / SSO between edge PIN users and cloud Google accounts
  (explicitly rejected in favour of the browser's existing cloud session).
- A machine-list dropdown in the bar (rejected; `/fleet` is the list).
- Any change to remote permissions (`REMOTE_VIEWER_PERMISSIONS` unchanged).
