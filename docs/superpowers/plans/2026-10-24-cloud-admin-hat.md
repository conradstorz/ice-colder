# Cloud Admin Hat Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A platform admin who is also a machine owner sees one hat at a time: `/admin*` is the Admin hat, everything else is My machines, and the Admin hat has no access to any machine's dashboard pages.

**Architecture:** `admin` stops being a machine role (`Role = owner | tech`; the `machine_role` admin fallback is deleted, so `require_machine_access` and the selection cookie admit members only). The hat is derived from the URL and rendered by `base.html` through one optional template variable `hat_switch` (`{"label", "url"}`): admin pages set the chip to `admin` plus a "My machines" link, portal pages for an admin get an "Admin" link. `/me` gains a "Platform admin" line. Admin templates stop linking to machine pages an admin can no longer open.

**Tech Stack:** Python 3.12, FastAPI, Jinja2, pytest (`uv run pytest`), ruff (`uv run ruff`). Spec: `docs/superpowers/specs/2026-10-24-cloud-admin-hat-design.md`.

## Global Constraints

- Cloud-only change: nothing under `packages/edge`, `packages/contracts`, `packages/common` or `packages/sims` changes. No contract bump, no schema migration, no new env, no new tables, no new routes.
- `ice_cloud` may import `ice_ui`, `ice_common`, `ice_contracts` only (`uv run lint-imports` enforces it).
- The word "admin" never appears in the signed-in chip outside `/admin*`.
- New markup reuses Tailwind classes already in the committed `packages/ui/src/ice_ui/static/app.css`; do not rebuild it. `tests/ui/test_static_css.py` and `tests/cloud/test_cloud_static_css.py` guard this.
- `tests/cloud/test_ui_parity.py` must stay green: the bar is outside `<main>`, so no parity change is expected.
- Commands: never chain with `&&`; run each command as its own call. Use `uv run ...` for everything Python.
- Commit trailer on every commit, in place of any `Co-Authored-By` line:
  `Assisted-by: Claude Fable 5.1 (Claude Code)`

---

## File map

| File | Responsibility after this plan |
|---|---|
| `packages/cloud/src/ice_cloud/auth.py` | `Role = Literal["owner", "tech"]`; `require_machine_access` resolves with `repo.access_for` only; `machine_role` gone |
| `packages/cloud/src/ice_cloud/viewer.py` | `RemoteViewer` plus the two `hat_switch` constants `ADMIN_SWITCH`, `MY_MACHINES_SWITCH` |
| `packages/cloud/src/ice_cloud/portal.py` | `_selected` uses `repo.access_for`; `_context` adds `hat_switch`; `/fleet` lists memberships only; `/me` passes `is_platform_admin` |
| `packages/cloud/src/ice_cloud/claim.py` | `_page` adds `hat_switch` |
| `packages/cloud/src/ice_cloud/alerts/pages.py` | `privileged = role == "owner"` |
| `packages/cloud/src/ice_cloud/admin/common.py` | `admin_context` sets `role_label="admin"` and `hat_switch=MY_MACHINES_SWITCH` |
| `packages/cloud/src/ice_cloud/templates/fleet.html` | no admin block |
| `packages/cloud/src/ice_cloud/templates/admin_machines.html`, `partials/admin_overview.html` | labels as text; alert count links to `/admin/alerts` |
| `packages/ui/src/ice_ui/templates/base.html` | renders `hat_switch` before the chip |
| `packages/ui/src/ice_ui/templates/me.html` | "Platform admin" line when `is_platform_admin` |
| `tests/cloud/test_admin_hat.py` (new) | the hat: chip, switch link, `/me` line |
| `tests/cloud/test_auth.py`, `test_me.py`, `test_portal.py`, `test_alert_pages.py`, `test_people.py`, `test_admin_dashboard.py` | admin cases flipped |
| `CLAUDE.md`, two earlier specs, `docs/deploying-the-cloud-server.md` | docs |

---

### Task 1: `admin` is no longer a machine role

**Files:**
- Modify: `packages/cloud/src/ice_cloud/auth.py` (docstring lines 13-15, `Role` line 65, `machine_role` lines 217-225, `require_machine_access` lines 228-264)
- Modify: `packages/cloud/src/ice_cloud/portal.py` (imports lines 21-29, `_selected` lines 76-94, `fleet` lines 179-224)
- Modify: `packages/cloud/src/ice_cloud/alerts/pages.py:48`
- Modify: `packages/cloud/src/ice_cloud/templates/fleet.html` (lines 30-32)
- Test: `tests/cloud/test_auth.py`, `tests/cloud/test_me.py`, `tests/cloud/test_portal.py`, `tests/cloud/test_alert_pages.py`

**Interfaces:**
- Produces: `ice_cloud.auth.Role = Literal["owner", "tech"]`; `require_machine_access(*, owner_only=False)` unchanged in signature, yields `(account, machine, role)` with role `owner`/`tech` only; `machine_role` no longer exists (nothing else imports it: `grep -rn machine_role packages/` must return only its definition before you start, and nothing after).
- Produces: `portal._context` keeps its signature for now (Task 2 extends it).

- [ ] **Step 1: Flip the admin cases in the existing tests**

In `tests/cloud/test_auth.py`, `test_machine_access_roles` (line 357): change

```python
    assert get(f"/m/{short}/probe", "admin@example.com").json()["role"] == "admin"
```

to

```python
    assert get(f"/m/{short}/probe", "admin@example.com").status_code == 404
```

Delete `test_machine_role_member_admin_and_stranger` (lines 640-657) entirely and add in its place:

```python
def test_admin_is_not_a_machine_role(cloud, signer):
    """A platform admin with no membership is a stranger to the machine."""
    from tests.cloud.conftest import push
    from tests.cloud.reports import make_report

    owner = login(cloud, "owner@example.com")
    push(cloud, signer, make_report())
    repo = cloud.app.state.repo
    repo.claim_machine(signer.fingerprint, owner.id, cloud.app.state.now())
    admin = login(cloud, "admin@example.com")
    assert repo.access_for(admin.id, signer.fingerprint) is None
    assert not hasattr(auth, "machine_role")
```

In `tests/cloud/test_me.py`, replace `test_admin_non_member_and_admin_owner` (lines 53-59) with:

```python
def test_admin_non_member_cannot_select(cloud, signer):
    owner = login(cloud, "owner@example.com")
    push(cloud, signer, _named_report())
    claim(cloud, signer, owner)
    login(cloud, "admin@example.com")
    r = cloud.post(f"/fleet/select/{short(signer)}", headers={"HX-Request": "true"})
    assert r.status_code == 404
    assert "No machine selected" in cloud.get("/me").text
```

and add `short` to the `from tests.cloud.conftest import (...)` list at the top of the file.

In `tests/cloud/test_portal.py`, replace `test_admin_sees_every_machine` (lines 86-100) with:

```python
def test_admin_fleet_lists_memberships_only(cloud):
    a, b = Signer(), Signer()
    owner = login(cloud, "owner@example.com")
    push(cloud, a, make_report(machine_id="m-A"))
    push(cloud, b, make_report(machine_id="m-B"))
    claim(cloud, a, owner)
    login(cloud, "admin@example.com")
    page = cloud.get("/fleet")
    assert "m-A" not in page.text and "m-B" not in page.text
    assert ">admin<" not in page.text and "Fleet dashboard" not in page.text
    assert "You have no machines yet" in page.text
    assert (
        cloud.post(
            f"/fleet/select/{short(b)}", headers={"HX-Request": "true"}
        ).status_code
        == 404
    )
    assert cloud.get(f"/m/{short(b)}").status_code == 404


def test_admin_who_owns_is_listed_as_owner(cloud):
    a = Signer()
    admin = login(cloud, "admin@example.com")
    push(cloud, a, make_report(machine_id="m-A"))
    claim(cloud, a, admin)
    page = cloud.get("/fleet")
    assert "m-A" in page.text and ">owner<" in page.text
```

In `tests/cloud/test_alert_pages.py`, replace `test_admin_alerts_page_shows_errors_without_a_mute_control` (lines 215-221) with:

```python
def test_admin_non_member_alerts_page_is_404(cloud, signer):
    owner, sh = _setup(cloud, signer)
    tech = _tech(cloud, signer, owner)
    _seed_delivery_history(cloud, signer, owner, tech)
    login(cloud, "admin@example.com")
    assert cloud.get(f"/machines/{sh}/alerts").status_code == 404
```

- [ ] **Step 2: Run the four files to verify the new cases fail**

Run: `uv run pytest tests/cloud/test_auth.py tests/cloud/test_me.py tests/cloud/test_portal.py tests/cloud/test_alert_pages.py -q`
Expected: FAIL on `test_machine_access_roles` (200 not 404), `test_admin_is_not_a_machine_role` (`hasattr` true), `test_admin_non_member_cannot_select` (303), `test_admin_fleet_lists_memberships_only`, `test_admin_non_member_alerts_page_is_404` (200). Everything else passes.

- [ ] **Step 3: Narrow `Role` and delete the fallback in `auth.py`**

Line 65:

```python
Role = Literal["owner", "tech"]
```

Delete `machine_role` (lines 217-225, the function and its docstring). Replace the body of the dependency inside `require_machine_access` (lines 248-262) so both branches call `repo.access_for`:

```python
        role: Role | None = await asyncio.to_thread(
            repo.access_for, account.id, machine.fingerprint
        )
        if role is None or (owner_only and role != "owner"):
            raise HTTPException(status_code=404)
        return account, machine, role
```

Rewrite the factory docstring (lines 229-235):

```python
    """Build a dependency yielding ``(account, machine, role)`` for ``{short}``.

    Role is ``owner`` or ``tech``, from the account's membership alone; a
    platform admin with no membership is a stranger here (the Admin hat lives
    under ``/admin`` and opens no machine page). ``owner_only=True`` admits the
    machine's owner alone. Everything else, including an unknown machine, is 404.
    """
```

Module docstring lines 13-15 become:

```
* :func:`require_machine_access` - a factory; the dependency it returns takes the
  ``short`` path parameter and yields ``(account, machine, role)``. A non-member
  gets 404 so they cannot learn that a machine exists; being a platform admin
  grants nothing here.
```

- [ ] **Step 4: Portal: `_selected` and `/fleet` use memberships only**

`packages/cloud/src/ice_cloud/portal.py`: remove `is_admin` and `machine_role` from the `from ice_cloud.auth import (...)` block (lines 21-29). In `_selected` (line 88) replace

```python
    role = await asyncio.to_thread(
        machine_role, state.settings, state.repo, account, fingerprint
    )
```

with

```python
    role: Role | None = await asyncio.to_thread(
        state.repo.access_for, account.id, fingerprint
    )
```

In `fleet` delete lines 184-187 (the `if is_admin(...)` block) and the `is_admin=is_admin(state.settings, account),` keyword in the `_context(...)` call (line 222). The entries list stays `entries: list[tuple[Machine, Role]] = [(a.machine, a.role) for a in access]`.

- [ ] **Step 5: Alerts page and fleet template**

`packages/cloud/src/ice_cloud/alerts/pages.py:48`:

```python
    privileged = role == "owner"
```

and its docstring line 37 `"""Owners and admins see every delivery in full.` becomes `"""Owners see every delivery in full.`

`packages/cloud/src/ice_cloud/templates/fleet.html`: delete lines 30-32:

```
{% if is_admin %}
<p class="mt-3 text-sm"><a href="/admin" class="text-gray-600">Fleet dashboard</a></p>
{% endif %}
```

- [ ] **Step 6: Run the cloud suite**

Run: `uv run pytest tests/cloud -q`
Expected: all pass. If `test_admin_dashboard.py` or `test_admin.py` fail on `/admin` links, stop and read the failure: they must not depend on `/fleet`'s removed link (a `grep -n "Fleet dashboard" tests/` should be empty).

Run: `grep -rn "machine_role\|is_admin" packages/cloud/src/ice_cloud/portal.py`
Expected: no output.

Run: `uv run ruff check packages/cloud tests/cloud`
Run: `uv run ruff format packages/cloud tests/cloud`

- [ ] **Step 7: Commit**

```
git add packages/cloud/src/ice_cloud/auth.py packages/cloud/src/ice_cloud/portal.py packages/cloud/src/ice_cloud/alerts/pages.py packages/cloud/src/ice_cloud/templates/fleet.html tests/cloud/test_auth.py tests/cloud/test_me.py tests/cloud/test_portal.py tests/cloud/test_alert_pages.py
git commit -m "feat(cloud): admin is no longer a machine role; /fleet lists memberships only

Assisted-by: Claude Fable 5.1 (Claude Code)"
```

---

### Task 2: The bar switch (`hat_switch`)

**Files:**
- Modify: `packages/cloud/src/ice_cloud/viewer.py`
- Modify: `packages/cloud/src/ice_cloud/portal.py` (`_context`, lines 122-144)
- Modify: `packages/cloud/src/ice_cloud/claim.py` (`_page`, lines 45-68)
- Modify: `packages/cloud/src/ice_cloud/admin/common.py` (`admin_context`, lines 26-35)
- Modify: `packages/ui/src/ice_ui/templates/base.html:79`
- Create: `tests/cloud/test_admin_hat.py`

**Interfaces:**
- Produces: `ice_cloud.viewer.ADMIN_SWITCH = {"label": "Admin", "url": "/admin"}` and `ice_cloud.viewer.MY_MACHINES_SWITCH = {"label": "My machines", "url": "/fleet"}` (plain dicts, read by templates as `hat_switch.label` / `hat_switch.url`).
- Produces: template variable `hat_switch` (dict or absent/None) consumed by `base.html`.

- [ ] **Step 1: Write the failing tests**

Create `tests/cloud/test_admin_hat.py`:

```python
"""One identity, two hats: the hat is the URL (spec 2026-10-24-cloud-admin-hat)."""

from tests.cloud.conftest import Signer, claim, login, own_and_select, push
from tests.cloud.reports import make_report


def _bar(html: str) -> str:
    return html.split('id="bar"', 1)[1].split("</header>", 1)[0]


ADMIN_LINK = 'href="/admin"'
MY_MACHINES_LINK = 'href="/fleet"'


def test_admin_pages_wear_the_admin_hat(cloud):
    login(cloud, "admin@example.com")
    for path in ("/admin", "/admin/machines", "/admin/owners", "/admin/accounts", "/admin/alerts"):
        bar = _bar(cloud.get(path).text)
        assert ">admin</span>" in bar, path
        assert ">My machines</a>" in bar and MY_MACHINES_LINK in bar, path
        assert ">Admin</a>" not in bar, path


def test_portal_pages_offer_the_admin_hat_to_an_admin(cloud, signer):
    own_and_select(cloud, signer, make_report(), email="admin@example.com")
    for path in ("/", "/health", "/fleet", "/me", "/claim"):
        bar = _bar(cloud.get(path).text)
        assert ">Admin</a>" in bar and ADMIN_LINK in bar, path
        assert ">admin</span>" not in bar, path
    assert ">owner</span>" in _bar(cloud.get("/").text)


def test_portal_pages_show_no_switch_to_an_owner(cloud, signer):
    own_and_select(cloud, signer, make_report())
    for path in ("/", "/fleet", "/me", "/claim"):
        bar = _bar(cloud.get(path).text)
        assert ">Admin</a>" not in bar and ">My machines</a>" not in bar, path


def test_people_and_alerts_pages_offer_the_switch_to_an_admin_owner(cloud, signer):
    own_and_select(cloud, signer, make_report(), email="admin@example.com")
    from tests.cloud.conftest import short

    for path in (f"/machines/{short(signer)}/people", f"/machines/{short(signer)}/alerts"):
        bar = _bar(cloud.get(path).text)
        assert ">Admin</a>" in bar, path


def test_login_page_has_no_switch(cloud):
    text = cloud.get("/login").text
    assert ">Admin</a>" not in text and ">My machines</a>" not in text
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/cloud/test_admin_hat.py -q`
Expected: the first four FAIL (no `>Admin</a>`, no `>admin</span>` on admin pages); `test_login_page_has_no_switch` passes.

- [ ] **Step 3: The constants**

`packages/cloud/src/ice_cloud/viewer.py`, append after the `RemoteViewer` class:

```python
# The two values of the ``hat_switch`` template variable (``base.html`` renders
# it as one link in the bar). The hat is the URL: under ``/admin`` the switch
# points back to My machines, everywhere else it offers Admin to an admin.
ADMIN_SWITCH = {"label": "Admin", "url": "/admin"}
MY_MACHINES_SWITCH = {"label": "My machines", "url": "/fleet"}
```

Update the module docstring to: `"""The remote viewer: ``current_user`` for pages the cloud renders from ``ice_ui``, and the two hat-switch links."""`

- [ ] **Step 4: Render it in `base.html`**

`packages/ui/src/ice_ui/templates/base.html` line 79 currently begins `</a>{% if current_user %}<a href="/me" ...`. Insert the switch between `</a>` and `{% if current_user %}`:

```
</a>{% if current_user and hat_switch %}<a href="{{ hat_switch.url }}"
         class="touch-target px-3 rounded-md text-sm font-medium text-white hover:bg-slate-700 shrink-0">{{ hat_switch.label }}</a>{% endif %}{% if current_user %}<a href="/me" ...
```

(the rest of the line is unchanged). The class string is byte-identical to the Back link's, so `app.css` needs no rebuild.

- [ ] **Step 5: Set it on the cloud contexts**

`packages/cloud/src/ice_cloud/portal.py`: add `is_admin` back to the `from ice_cloud.auth import (...)` block and import the constant: `from ice_cloud.viewer import ADMIN_SWITCH, RemoteViewer` (merge with the existing `RemoteViewer` import). In `_context`, after `"level": level,` add:

```python
        "hat_switch": (
            ADMIN_SWITCH if is_admin(request.app.state.settings, account) else None
        ),
```

`packages/cloud/src/ice_cloud/claim.py`: add `from ice_cloud.auth import is_admin, require_htmx, require_signed_in` and `from ice_cloud.viewer import ADMIN_SWITCH, RemoteViewer`; in `_page`'s context dict add after `"level": LEVEL_CLAIM,`:

```python
            "hat_switch": (
                ADMIN_SWITCH
                if is_admin(request.app.state.settings, account)
                else None
            ),
```

`packages/cloud/src/ice_cloud/admin/common.py`: import `from ice_cloud.viewer import MY_MACHINES_SWITCH, RemoteViewer` and change `admin_context` to:

```python
def admin_context(
    request: Request, account: Account, level: Level | None, **extra
) -> dict:
    """Every ``/admin*`` page wears the Admin hat: the chip reads ``admin`` and
    the bar offers the way back to My machines."""
    return {
        "request": request,
        "current_user": RemoteViewer(display_name=account.name, role_label="admin"),
        "perms": REMOTE_VIEWER_PERMISSIONS,
        "level": level,
        "hat_switch": MY_MACHINES_SWITCH,
        **extra,
    }
```

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/cloud/test_admin_hat.py tests/cloud/test_me.py tests/cloud/test_ui_parity.py tests/ui/test_static_css.py tests/cloud/test_cloud_static_css.py -q`
Expected: all pass. (`test_fleet_bar_has_no_role` in `test_me.py` asserts `"rounded-full bg-slate-600" not in bar` for an owner; the switch link has no `rounded-full`, so it still passes.)

Run: `uv run pytest tests/edge/web -q -x`
Expected: all pass (the edge never sets `hat_switch`; the `{% if %}` renders nothing).

Run: `uv run ruff check packages/cloud tests/cloud`
Run: `uv run ruff format packages/cloud tests/cloud`

- [ ] **Step 7: Commit**

```
git add packages/cloud/src/ice_cloud/viewer.py packages/cloud/src/ice_cloud/portal.py packages/cloud/src/ice_cloud/claim.py packages/cloud/src/ice_cloud/admin/common.py packages/ui/src/ice_ui/templates/base.html tests/cloud/test_admin_hat.py
git commit -m "feat(cloud): hat switch in the bar; /admin pages wear the admin chip

Assisted-by: Claude Fable 5.1 (Claude Code)"
```

---

### Task 3: `/me` names the platform admin

**Files:**
- Modify: `packages/ui/src/ice_ui/templates/me.html` (after the role paragraph, lines 11-19)
- Modify: `packages/cloud/src/ice_cloud/portal.py` (`me`, lines 434-462)
- Test: `tests/cloud/test_admin_hat.py`

**Interfaces:**
- Consumes: `is_admin` already imported in `portal.py` (Task 2).
- Produces: template variable `is_platform_admin: bool` on `/me` (the edge never sets it; absent is falsy).

- [ ] **Step 1: Write the failing tests**

Append to `tests/cloud/test_admin_hat.py`:

```python
def test_me_names_the_platform_admin(cloud, signer):
    own_and_select(cloud, signer, make_report(), email="admin@example.com")
    text = cloud.get("/me").text
    assert "Platform admin" in text and 'href="/admin"' in text
    assert "<strong>owner</strong> on" in text


def test_me_without_selection_still_names_the_platform_admin(cloud):
    login(cloud, "admin@example.com")
    text = cloud.get("/me").text
    assert "Platform admin" in text and "No machine selected" in text


def test_me_says_nothing_about_admin_to_an_owner(cloud, signer):
    own_and_select(cloud, signer, make_report())
    assert "Platform admin" not in cloud.get("/me").text
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/cloud/test_admin_hat.py -q -k me_`
Expected: the first two FAIL ("Platform admin" missing); the third passes.

- [ ] **Step 3: Template**

`packages/ui/src/ice_ui/templates/me.html`: after the closing `</p>` of the role paragraph (line 19) and before the card's closing `</div>` (line 20), add:

```
    {% if is_platform_admin %}
    <p class="text-sm text-gray-500">Platform admin <a href="/admin" class="underline">Admin dashboard</a></p>
    {% endif %}
```

(`text-sm text-gray-500` and `underline` already appear in this template.)

- [ ] **Step 4: Route**

`packages/cloud/src/ice_cloud/portal.py`, in `me`, change the final `ctx.update(...)` to:

```python
    ctx.update(
        machine_label=label,
        permission_lines=lines,
        logout_label="Sign out",
        is_platform_admin=is_admin(request.app.state.settings, account),
    )
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/cloud/test_admin_hat.py tests/cloud/test_me.py tests/edge/web/test_me.py tests/ui/test_static_css.py -q`
Expected: all pass. (If `tests/edge/web/test_me.py` does not exist, run `uv run pytest tests/edge/web -q -k me` instead.)

Run: `uv run ruff format packages/cloud tests/cloud`

- [ ] **Step 6: Commit**

```
git add packages/ui/src/ice_ui/templates/me.html packages/cloud/src/ice_cloud/portal.py tests/cloud/test_admin_hat.py
git commit -m "feat(cloud): /me names the platform admin with a link to /admin

Assisted-by: Claude Fable 5.1 (Claude Code)"
```

---

### Task 4: Admin templates stop linking to machine pages

**Files:**
- Modify: `packages/cloud/src/ice_cloud/templates/admin_machines.html:34,53`
- Modify: `packages/cloud/src/ice_cloud/templates/partials/admin_overview.html:30`
- Test: `tests/cloud/test_admin_dashboard.py`

**Interfaces:** none.

- [ ] **Step 1: Write the failing test**

Append to `tests/cloud/test_admin_dashboard.py`:

```python
def test_admin_pages_never_link_to_machine_pages(cloud, clock):
    """The Admin hat opens no machine page, so no admin template may point at one."""
    quiet, busy = _two_machines(cloud, clock)
    owner = login(cloud, "owner@example.com")
    claim(cloud, busy, owner)
    _admin(cloud)
    for path in ("/admin", "/admin/machines", "/admin/owners", "/admin/alerts"):
        text = cloud.get(path).text
        assert 'href="/m/' not in text, path
        assert 'href="/machines/' not in text, path
    frag = cloud.get("/admin/overview", headers={"HX-Request": "true"}).text
    assert 'href="/m/' not in frag
    machines = cloud.get("/admin/machines").text
    assert 'href="/admin/alerts"' in machines
    assert short(busy) in machines and "m-busy" in machines
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/cloud/test_admin_dashboard.py -q -k never_link`
Expected: FAIL on `'href="/m/' not in text` for `/admin`.

- [ ] **Step 3: Edit the templates**

`packages/cloud/src/ice_cloud/templates/admin_machines.html` line 34:

```
          <span class="font-mono text-gray-900">{{ m.label }}</span>
```

line 53:

```
        <td class="p-3"><a href="/admin/alerts" class="text-gray-700">{{ m.alerts_24h }}</a></td>
```

`packages/cloud/src/ice_cloud/templates/partials/admin_overview.html` line 30:

```
    <span class="font-mono text-gray-900">{{ a.row.label }}</span>
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/cloud/test_admin_dashboard.py tests/cloud/test_admin.py tests/cloud/test_cloud_static_css.py -q`
Expected: all pass. If an existing test asserted the old `/m/` href, update that assertion to the new plain-text markup rather than restoring the link.

- [ ] **Step 5: Commit**

```
git add packages/cloud/src/ice_cloud/templates/admin_machines.html packages/cloud/src/ice_cloud/templates/partials/admin_overview.html tests/cloud/test_admin_dashboard.py
git commit -m "fix(cloud): admin tables no longer link to machine pages an admin cannot open

Assisted-by: Claude Fable 5.1 (Claude Code)"
```

---

### Task 5: Docs, full suite, import rules

**Files:**
- Modify: `CLAUDE.md` (Cloud section: the **People**, **Portal**, **Admin dashboard** bullets)
- Modify: `docs/superpowers/specs/2026-10-19-cloud-identity-tenancy-design.md` (§3 line 55, §6.3 lines 158, 166-169)
- Modify: `docs/superpowers/specs/2026-10-21-cloud-fleet-admin-dashboard-design.md` (§3, the "/fleet gains ... Fleet dashboard" paragraph, line 96)
- Modify: `docs/deploying-the-cloud-server.md` (near line 69, the `ICE_CLOUD_ADMINS` line)

**Interfaces:** none.

- [ ] **Step 1: CLAUDE.md**

In the **People** bullet, replace `platform admin = email in `ICE_CLOUD_ADMINS` (`/admin`: unbind, suspend). A non-member gets 404 on a machine, never 403.` with:

```
platform admin = email in `ICE_CLOUD_ADMINS` (`/admin`: unbind, suspend). **Admin is not a machine role** (spec `docs/superpowers/specs/2026-10-24-cloud-admin-hat-design.md`): `Role` is `owner | tech`, `require_machine_access` and the selection cookie resolve membership alone, so an admin with no membership gets 404 on `/fleet/select`, `/m/{short}`, `/machines/{short}/people` and `/machines/{short}/alerts` like any stranger. A non-member gets 404 on a machine, never 403.
```

In the **Portal** bullet, replace `shows the viewer's role on the selected machine (owner / tech / admin), or "No machine selected" with a link to `/fleet`; the login page renders no signed-in chip.` with:

```
shows the viewer's role on the selected machine (owner / tech), or "No machine selected" with a link to `/fleet`, plus a "Platform admin" line linking to `/admin` for an admin; the login page renders no signed-in chip. **One identity, two hats, the hat is the URL:** every `/admin*` page sets the chip to `admin` and `hat_switch = MY_MACHINES_SWITCH` (a "My machines" link to `/fleet`); every other cloud page sets `hat_switch = ADMIN_SWITCH` ("Admin" → `/admin`) for an admin and nothing for anyone else (`ice_cloud.viewer`). `base.html` renders `hat_switch` as one bar link before the chip; the edge never sets it. The word "admin" never appears in the chip outside `/admin*`, and `/fleet` lists memberships only.
```

In the **Admin dashboard** bullet, after `in the package `ice_cloud/admin/`.` add:

```
Admin templates link only within `/admin` (a machine label is plain text, the alert count goes to `/admin/alerts`), because the Admin hat opens no machine page.
```

- [ ] **Step 2: Superseded notes in the two earlier specs**

`docs/superpowers/specs/2026-10-19-cloud-identity-tenancy-design.md`: after line 55 (the **platform admin** bullet) add a new paragraph:

```
*(Superseded 2026-10-24 by `2026-10-24-cloud-admin-hat-design.md`: platform admin is no longer a role on any machine. `Role` is `owner | tech`; an admin with no membership is a stranger to a machine, and the rows below that admit "admin" to machine pages no longer do.)*
```

`docs/superpowers/specs/2026-10-21-cloud-fleet-admin-dashboard-design.md`: after the paragraph at line 96 beginning `` `/fleet` gains, for admins only, a one-line link "Fleet dashboard" `` add:

```
*(Superseded 2026-10-24 by `2026-10-24-cloud-admin-hat-design.md`: the link moved into the bar as the hat switch, and `/fleet` lists memberships only.)*
```

- [ ] **Step 3: Runbook**

`docs/deploying-the-cloud-server.md`, directly after the `ICE_CLOUD_ADMINS=...` line's surrounding paragraph (around line 69), add:

```
An admin account sees the fleet tables under `/admin` but no machine's dashboard: to open one it
must be that machine's owner (claim code) or tech (owner invitation), like anyone else. The bar
switches between the Admin hat and My machines.
```

- [ ] **Step 4: Full verification**

Run: `uv run pytest -q`
Expected: all pass (browser tests skip without `ICE_COLDER_BROWSER_TESTS`).

Run: `uv run lint-imports`
Expected: all contracts kept.

Run: `uv run python scripts/check_package_isolation.py`
Expected: exit 0.

Run: `uv run ruff check .`
Run: `uv run ruff format --check .`
Expected: clean.

Run: `grep -rn "\"admin\"" packages/cloud/src/ice_cloud/ --include=*.py`
Expected: only `role_label="admin"` in `admin/common.py` (and nothing else referring to a machine role).

- [ ] **Step 5: Commit**

```
git add CLAUDE.md docs/superpowers/specs/2026-10-19-cloud-identity-tenancy-design.md docs/superpowers/specs/2026-10-21-cloud-fleet-admin-dashboard-design.md docs/deploying-the-cloud-server.md
git commit -m "docs: record the cloud admin hat (admin is not a machine role; hat by URL)

Assisted-by: Claude Fable 5.1 (Claude Code)"
```
