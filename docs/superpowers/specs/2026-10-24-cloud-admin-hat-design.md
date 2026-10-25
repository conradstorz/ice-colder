# Cloud admin hat — design

**Date:** 2026-10-24. **Scope:** `ice_cloud` only. **Supersedes:** the "admin" machine role of
`2026-10-19-cloud-identity-tenancy-design.md` §3/§5.3 (the `machine_role` admin fallback) and the
`/fleet` "Fleet dashboard" link of `2026-10-21-cloud-fleet-admin-dashboard-design.md` §3.

## 1. Problem

One Google account can be both the platform admin (`ICE_CLOUD_ADMINS`) and a machine owner. Today
the two roles blend: `/fleet` lists the account's own machines as "owner" and every other machine
as "admin" in one list; selecting any of them opens that machine's dashboard with the chip reading
whichever role applied; `/admin` is a separate tree reached by a stray link at the bottom of
`/fleet`. The owner of the system (who is this account) found the mixing confusing.

**Decision (owner, 2026-10-24):** one identity, two hats, shown one at a time. The hat is
determined by the URL and nothing else: everything under `/admin` is the **Admin** hat, everything
else is **My machines**. The Admin hat has no access to any machine's dashboard pages; to see a
dashboard the account must be that machine's owner or tech, like anyone else.

## 2. Access model

- `Role = Literal["owner", "tech"]` (`ice_cloud.auth`). `machine_role` is deleted.
  `require_machine_access` resolves the role with `repo.access_for` alone, so a platform admin with
  no membership gets **404** on `POST /fleet/select/{short}`, `GET /m/{short}`,
  `/machines/{short}/people` and `/machines/{short}/alerts`, exactly like any non-member.
- Portal machine pages (`/`, `/status`, `/pill`, `/kpi`, `/health`, `/health/faults`, `/me`'s
  role line) use the same resolution through `_selection`; a selection cookie naming a machine the
  account is not a member of is treated as no selection (303 `/fleet`, 204 for a fragment), as it
  already is for any non-member.
- `is_admin` and `require_admin` are unchanged. Everything under `/admin` is still 404 for a
  non-admin. The admin's powers are unchanged: fleet tables, unbind, suspend/unsuspend, test
  email, fleet-wide alert history. Admin can still never set an owner directly.
- Unclaimed machines' reports stay visible only through the admin tables (status, fault count,
  alerts), which is all the admin saw of them in practice.

## 3. `/fleet`

Lists the account's owned and tech-granted machines only. The `is_admin` context flag and the
"Fleet dashboard" paragraph are removed from `fleet.html`; the bar carries the switch (§4). An admin
with no memberships sees the existing empty state ("You have no machines yet." plus Claim).

## 4. The bar switch

One new optional template variable, **`hat_switch`**, a mapping with `label` and `url`. `base.html`
renders it, when present and `current_user` is set, as a plain link in the bar immediately before
the signed-in chip, reusing the Back link's classes
(`touch-target px-3 rounded-md text-sm font-medium text-white hover:bg-slate-700 shrink-0`) so the
committed `app.css` needs no rebuild. The edge never sets `hat_switch`, so edge pages render
byte-identically; `tests/cloud/test_ui_parity.py` compares `<main>`, which the bar is outside of.

| Pages | Chip `role_label` | `hat_switch` |
|---|---|---|
| `/admin*` (`admin_context`) | `admin` | `My machines` → `/fleet` |
| portal machine pages, `/fleet`, `/me`, `/claim` (`_context` and the page-specific contexts), when `is_admin(settings, account)` | the selected machine's `owner`/`tech`, or none when no machine is selected | `Admin` → `/admin` |
| the same pages for a non-admin | as today | absent |

The word "admin" never appears in the chip outside `/admin*`. The login page renders no chip and no
switch (unchanged).

## 5. `/me`

Under the My machines hat (its URL is not under `/admin`). For an admin it gains one line under
the existing role-on-machine / "No machine selected" line: **Platform admin** with a link
"Admin dashboard" to `/admin`. Non-admins see no change.

## 6. Admin pages that pointed at machine pages

`admin_machines.html` today links a row's label to `/m/{short}` and its 24 h alert count to
`/machines/{short}/alerts`, and `partials/admin_overview.html`'s "Needs attention" rows link the
label to `/m/{short}`; all three would now 404 for an admin who is not a member. The labels become
plain text (the short fingerprint stays beside them) and the alert count links to `/admin/alerts`,
which already shows the fleet-wide history. `/admin/owners` rows keep linking to
`/admin/machines?owner=<id>`. No other admin template links outside `/admin`.

`ice_cloud/alerts/pages.py`'s `/machines/{short}/alerts` treats `role in ("owner", "admin")` as
privileged; with `admin` gone from `Role` that becomes `role == "owner"`, no behaviour change for
owners or techs.

## 7. Code changes

| File | Change |
|---|---|
| `ice_cloud/auth.py` | `Role` narrows to `owner`/`tech`; `machine_role` removed; `require_machine_access` uses `repo.access_for`; module docstring updated |
| `ice_cloud/portal.py` | `_selection` uses `repo.access_for`; `_context` takes `hat_switch` (computed by one helper `admin_switch(settings, account) -> dict | None`) and passes it to every page; `/fleet` lists memberships only and drops `is_admin`; `/me` passes `is_platform_admin` |
| `ice_cloud/viewer.py` | `ADMIN_SWITCH` / `MY_MACHINES_SWITCH` constants (the two `hat_switch` values) |
| `ice_cloud/admin/common.py` | `admin_context` sets `role_label="admin"` and `hat_switch={"label": "My machines", "url": "/fleet"}` |
| `ice_cloud/templates/fleet.html` | remove the `is_admin` block |
| `ice_cloud/templates/admin_machines.html` | label as text; alert count → `/admin/alerts` |
| `ice_cloud/templates/partials/admin_overview.html` | attention-row label as text |
| `ice_cloud/alerts/pages.py` | `privileged = role == "owner"` |
| `ice_cloud/claim.py` | `_page` passes `hat_switch` like `_context` does |
| `ice_ui/templates/base.html` | render `hat_switch` link before the chip |
| `ice_ui/templates/me.html` | the Platform admin line when `is_platform_admin` |
| `docs/superpowers/specs/2026-10-19-cloud-identity-tenancy-design.md`, `2026-10-21-cloud-fleet-admin-dashboard-design.md` | one-line "superseded by" notes at the affected sections |
| `CLAUDE.md` (Cloud → People / Portal / Admin dashboard) | roles are owner/tech only; admin has no machine-page access; the bar switch |

No contract bump, no schema migration, no new env, no new tables, no new routes.

## 8. Tests

- `tests/cloud/test_auth.py`: the admin-fallback cases flip to 404 (select, `/m/{short}`, people,
  alerts, and a selection cookie for a non-member machine); an admin who is also owner still gets
  `owner`.
- `tests/cloud/test_me.py` (or a new `test_admin_hat.py`): `/fleet` for an admin lists only
  memberships and has no "Fleet dashboard" text; every `/admin*` page carries the `ADMIN` chip and
  the My machines link; `/fleet`, `/me` and a selected machine's `/` for an admin carry the Admin
  link and a chip that never reads `admin`; a non-admin's pages contain no switch; `/me` shows
  "Platform admin" for an admin and not for an owner.
- `tests/cloud/test_admin_dashboard.py`: the machines table and the overview's attention rows have
  no `/m/` or `/machines/` hrefs; the alert count links to `/admin/alerts`.
- `tests/cloud/test_portal.py`, `test_alert_pages.py`, `test_people.py`: the admin cases become
  non-member 404s (`test_admin_sees_every_machine` inverts; the admin alerts-page test becomes a 404).
- `tests/cloud/test_ui_parity.py` unchanged. `tests/ui/test_static_css.py` guards the reused
  classes.
- An edge render of `base.html` without `hat_switch` is unchanged (existing edge tests cover it).

## 9. Rollout

Cloud-only deploy, no edge change. One behaviour change for the runbook
(`docs/deploying-the-cloud-server.md`): an admin who wants a machine's dashboard must be made its
owner (claim code) or tech (owner invitation) like anyone else; the admin tables show status,
faults and alerts without that.

## 10. Non-goals

- No admin management UI (`ICE_CLOUD_ADMINS` stays the mechanism; identity spec D5).
- No "view as admin" banner or impersonation; the owner chose no dashboard access at all.
- No stored mode; the hat is the URL.
- No change to what the Admin hat can do.
