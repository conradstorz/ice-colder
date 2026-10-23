# Signed-in Identity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every dashboard page (edge and cloud) shows who is signed in and as what role in the bar, and a `/me` ("You") level spells out the person, their role on the named machine, and what that role may do.

**Architecture:** Widen the existing `current_user` template contract with `name` / `role_label` (and `email`) on both platforms; add one shared `me.html` and `LEVEL_ME` in `ice_ui`; put permission wording in `ice_contracts.access.PERMISSION_LABELS`; carry the machine's common name to the cloud by adding `MachineReport.common_name` under contract 0.4.0. Spec: `docs/superpowers/specs/2026-10-23-signed-in-identity-design.md`.

**Tech Stack:** Python 3.12, FastAPI + Jinja2 + HTMX, Pydantic v2, pytest, Tailwind 3.4.17 standalone CLI, uv.

## Global Constraints

- Use `uv run` for every command (`uv run pytest`, `uv run ruff check --fix .`, `uv run ruff format .`). Never the global `ruff`.
- Package layering (import-linter): `ice_contracts` -> `ice_common` -> `ice_ui` -> (`ice_edge` | `ice_cloud` | `ice_sims`). `ice_ui` may import only `ice_contracts`. Check with `uv run lint-imports`.
- Tests never read private attributes (`tests/meta/test_no_private_access.py` ratchet). Reach the VMC/Machine only through public seams.
- Every route module exposes a module-level `router`; handlers are module-level functions (`tests/meta/test_routes_shape.py`).
- Templates: any new Tailwind class must exist in the committed `packages/ui/src/ice_ui/static/app.css`. Rebuild with `.tailwind/tailwindcss.exe -c packages/ui/tailwind.config.js -i packages/ui/tailwind.input.css -o packages/ui/src/ice_ui/static/app.css --minify` and commit the result. `tests/ui/test_static_css.py` checks it.
- Never write a literal `class="..."` inside a Jinja comment (`{# #}`), the CSS extractor cannot tell.
- `tests/ui/golden/base_no_remote.html` is a byte golden of `base.html` rendered with no `current_user` and no `remote`; the chip must add no bytes to that render (see Task 3).
- Commit after every task with the trailer line `Assisted-by: Claude Fable 5.1 (Claude Code)` and no `Co-Authored-By` line.
- Spec naming note: the spec calls the UI view-model `MachineIdentity`; the edge already has `ice_edge.uplink.identity.MachineIdentity` (the Ed25519 key). This plan names the view-model **`MachineLabel`** (`ice_ui.views.identity.MachineLabel`) to avoid the collision. Task 7 records that in the spec.

## File map

| File | Responsibility |
|---|---|
| `packages/contracts/src/ice_contracts/access.py` | add `PERMISSION_LABELS` |
| `packages/contracts/src/ice_contracts/cloud/report.py` | `common_name`, contract `0.4.0` |
| `docs/contracts/edge-cloud/CONTRACT.md`, `docs/contracts/edge-cloud/schemas/*.json` | 0.4.0 section; regenerated schemas |
| `packages/edge/src/ice_edge/uplink/report.py` | send `common_name` |
| `packages/ui/src/ice_ui/views/identity.py` (new) | `MachineLabel` view-model |
| `packages/ui/src/ice_ui/levels.py` | `LEVEL_ME` |
| `packages/ui/src/ice_ui/templates/base.html` | bar chip |
| `packages/ui/src/ice_ui/templates/me.html` (new) | the "You" page |
| `packages/ui/src/ice_ui/templates/partials/remote_banner.html` | common name before id |
| `packages/edge/src/ice_edge/web/auth.py` | `TemplateUser.role_label` |
| `packages/edge/src/ice_edge/web/routes/me.py` (new), `routes/__init__.py` | `GET /me` on the edge |
| `packages/cloud/src/ice_cloud/viewer.py`, `auth.py`, `portal.py` | `RemoteViewer` fields, role helper, `Selection`, `GET /me`, banner |
| `CLAUDE.md`, spec | docs |

---

### Task 1: `PERMISSION_LABELS` in the contracts package

**Files:**
- Modify: `packages/contracts/src/ice_contracts/access.py`
- Test: `tests/contracts/test_access_labels.py` (new)

**Interfaces:**
- Produces: `PERMISSION_LABELS: dict[Permission, str]`, one short plain-words phrase per `Permission` member, in `Permission` definition order. Suggested wording:
  - `view_status`: "See machine status and health"
  - `clear_faults`: "Clear faults"
  - `view_logs`: "Read the machine log"
  - `machine_controls`: "Restart, reset or shut down the machine"
  - `run_tests`: "Run subsystem tests and simulated sales"
  - `edit_catalog`: "Add and edit products"
  - `edit_placement`: "Set product slots, stock and prices"
  - `view_reports`: "View sales reports"
  - `edit_contacts`: "Edit machine details and contacts"
  - `edit_secrets`: "Edit payment, email and MQTT settings"
  - `manage_users`: "Manage users and devices"
  - `manage_ownership`: "Transfer ownership and manage emergency codes"
  - `collect_cash`: "Record a cash collection"

- [ ] **Step 1: Write the failing tests** in `tests/contracts/test_access_labels.py`: every `Permission` member has a non-empty label; labels are distinct; the dict's key order equals `list(Permission)`.
- [ ] **Step 2: Run** `uv run pytest tests/contracts/test_access_labels.py -v`; expect ImportError on `PERMISSION_LABELS`.
- [ ] **Step 3: Add** `PERMISSION_LABELS` to `access.py` with a one-line docstring comment: "Plain-words wording for the You page; the edge and the cloud share it."
- [ ] **Step 4: Run** the test file; expect 3 passed. Run `uv run ruff check --fix .` then `uv run ruff format .`.
- [ ] **Step 5: Commit** `feat(contracts): plain-words labels for every permission`.

---

### Task 2: `MachineReport.common_name` under contract 0.4.0

**Files:**
- Modify: `packages/contracts/src/ice_contracts/cloud/report.py` (`CLOUD_CONTRACT_VERSION`, `MachineReport.contract` literal, new field)
- Modify: `docs/contracts/edge-cloud/CONTRACT.md` (header, Models line, new `## 0.4.0` section at the top of the sections)
- Regenerate: `docs/contracts/edge-cloud/schemas/*.schema.json` via `uv run python -m ice_contracts.generate`
- Modify: `packages/edge/src/ice_edge/uplink/report.py` (`build_machine_report` sets `common_name`)
- Test: `tests/contracts/test_cloud_contract.py` (update the three version tests), `tests/edge/uplink/test_report.py` (new test), `tests/cloud/test_ingest.py` (new test)

**Interfaces:**
- Produces: `MachineReport.common_name: str | None = None` (max length `MAX_TEXT`, 200); `MachineReport.contract: Literal["0.2.0", "0.3.0", "0.4.0"]`; `CLOUD_CONTRACT_VERSION == "0.4.0"`.
- Edge: `build_machine_report` sets `common_name=clip(config.physical.common_name)` using the existing `clip` from `ice_edge.views.health` (same clipping the product names get).

- [ ] **Step 1: Update contract tests** in `tests/contracts/test_cloud_contract.py`: rename `test_contract_version_is_0_3_0` to `..._0_4_0` asserting `"0.4.0"`; `test_report_accepts_0_2_0_and_0_3_0` becomes `..._through_0_4_0` accepting all three and rejecting `"0.5.0"`; add `test_report_common_name_is_optional_and_capped` (absent -> `None`; 200 chars ok; 201 chars -> `ValidationError`); `test_contract_doc_has_0_4_0_section`. Also update the module docstring's version.
- [ ] **Step 2: Write the edge test** in `tests/edge/uplink/test_report.py`, following `test_report_validates_and_carries_fault_and_digest`'s shape: with `config.physical.common_name = "Lobby Ice"` the built report's `common_name == "Lobby Ice"`; with a 300-char name it is clipped to 200 and the report still validates.
- [ ] **Step 3: Write the cloud ingest test** in `tests/cloud/test_ingest.py` using the file's existing `push`/`make_report` helpers: a report with `contract="0.4.0", common_name="Lobby Ice"` is accepted and `store.latest(fingerprint).common_name == "Lobby Ice"`; a `"0.3.0"` report without the field is still accepted with `common_name is None`.
- [ ] **Step 4: Run** `uv run pytest tests/contracts/test_cloud_contract.py tests/edge/uplink/test_report.py tests/cloud/test_ingest.py -v`; expect the new/updated tests to fail.
- [ ] **Step 5: Implement**: bump the version and literal, add `common_name: str | None = Field(default=None, max_length=MAX_TEXT)`, set it in `build_machine_report`, run `uv run python -m ice_contracts.generate`, and write the CONTRACT.md 0.4.0 section: "`MachineReport` gains `common_name: str | None = null`, the operator-entered machine name from `physical.common_name` (clipped to 200 characters by the edge), shown on the cloud's You page and remote banner. `contract` accepts `0.2.0`, `0.3.0` and `0.4.0`. Upgrade the cloud before the edges: a 0.3.0 cloud rejects a 0.4.0 report with 422." Update the file's header line and the "`CLOUD_CONTRACT_VERSION` is" sentence.
- [ ] **Step 6: Run** the same three files plus `tests/contracts/test_contract_schemas.py` (schema drift); expect all passed. Run ruff.
- [ ] **Step 7: Commit** `feat(contracts): MachineReport.common_name, edge-cloud contract 0.4.0`.

---

### Task 3: Bar chip, `LEVEL_ME`, and the `MachineLabel` view-model

**Files:**
- Create: `packages/ui/src/ice_ui/views/identity.py`
- Modify: `packages/ui/src/ice_ui/levels.py` (add `LEVEL_ME`)
- Modify: `packages/ui/src/ice_ui/templates/base.html` (chip between the pill and Lock)
- Modify: `packages/ui/src/ice_ui/templates/partials/remote_banner.html` (common name first)
- Regenerate: `tests/ui/golden/base_no_remote.html` only if Step 6 proves the no-`current_user` render changed (it must not; see Step 5)
- Test: `tests/ui/test_identity.py` (new), `tests/ui/test_bar_identity.py` (new), `tests/ui/test_levels.py` (count 31 -> 32), `tests/ui/test_remote_banner.py` (one new test)

**Interfaces:**
- Produces `ice_ui.views.identity.MachineLabel` (frozen dataclass): `common_name: str | None = None`, `machine_id: str | None = None`, `short_fingerprint: str | None = None`; property `display -> str` returning `f"{common_name} ({machine_id})"` when both are set, else the first set of `common_name`, `machine_id`, `short_fingerprint`, else `""`.
- Produces `LEVEL_ME = Level(title="You", url="/me", parent=LEVEL_HOME)`.
- The chip reads `current_user.name` and `current_user.role_label` (either may be absent only when `current_user` itself is `None`). Markup, placed after the `</a>` of the health pill and before the Lock button, on the existing lines with no added blank lines:

```
{% if current_user %}<a href="/me" class="touch-target px-2 shrink-0 flex items-center gap-1" aria-label="Signed in as {{ current_user.name }}">{% if current_user.role_label %}<span class="inline-flex items-center rounded-full bg-slate-600 text-white text-xs font-semibold uppercase tracking-wide px-2 py-1">{{ current_user.role_label }}</span>{% endif %}<span class="max-md:hidden text-sm text-slate-200 truncate max-w-[10rem]">{{ current_user.name }}</span></a>{% endif %}
```

  Keep the whole `{% if current_user %}...{% endif %}` on one line appended to the end of the pill's closing `</a>` line (no new line break before it), so the golden render without `current_user` is byte-identical. Add the explanatory Jinja comment *inside* the `{% if %}` block if you want one.
- Banner: `remote.common_name` (may be absent, use `remote.common_name is defined and remote.common_name`) renders as `<span class="font-semibold">{{ remote.common_name }}</span>` before the existing `machine_id` span.

- [ ] **Step 1: Tests for `MachineLabel.display`** in `tests/ui/test_identity.py`: both set -> `"Lobby Ice (ice-07)"`; name only; id only; fingerprint only; none -> `""`.
- [ ] **Step 2: Tests for the chip** in `tests/ui/test_bar_identity.py`, rendering `base.html` through `ice_ui.templating.templates` with `level=LEVEL_HEALTH_FAULTS` and `current_user=SimpleNamespace(name=..., role_label=...)` (the pattern in `tests/ui/test_remote_banner.py`): with role `owner` the bar contains `href="/me"`, the text `owner` inside the role span and the name; with `role_label=None` the role span is absent but the name link is present; with no `current_user` the string `/me` is absent. Assert the chip lies inside `<header id="bar"` (slice the html between `id="bar"` and `</header>`).
- [ ] **Step 3: Level test**: change `test_discovers_all_31_levels` to 32 and extend its docstring with "+ LEVEL_ME (/me, the You page)"; add `test_me_is_a_child_of_home` asserting title "You", url "/me", parent is `LEVEL_HOME`.
- [ ] **Step 4: Banner test**: in `tests/ui/test_remote_banner.py` add `test_common_name_renders_before_machine_id` (`_remote(common_name="Lobby Ice")` -> `"Lobby Ice"` appears before `"m-0042"`), and assert the existing `_remote()` without the attribute still renders (the template must tolerate the attribute being undefined).
- [ ] **Step 5: Run** `uv run pytest tests/ui -v`; expect the new tests to fail and `test_base_golden.py` to pass.
- [ ] **Step 6: Implement** `identity.py`, `LEVEL_ME`, the chip, and the banner change.
- [ ] **Step 7: Rebuild Tailwind** with the command in Global Constraints, then run `uv run pytest tests/ui tests/cloud/test_cloud_static_css.py -v`; expect all passed including `test_base_golden.py` (if the golden fails, the chip added bytes outside the `if`; fix the template, do not regenerate the golden).
- [ ] **Step 8: Commit** `feat(ui): signed-in chip in the bar, LEVEL_ME, MachineLabel, banner common name` including `app.css`.

---

### Task 4: `me.html`, the shared You page template

**Files:**
- Create: `packages/ui/src/ice_ui/templates/me.html`
- Test: `tests/ui/test_me_template.py` (new)

**Interfaces:**
- Consumes `LEVEL_ME`, `MachineLabel` (Task 3), `PERMISSION_LABELS` (Task 1).
- Template context: the ordinary `current_user` / `perms` / `level`, plus `logout_label: str`, `machine_label: MachineLabel | None`, and `permission_lines: list[str]` (the route computes `[PERMISSION_LABELS[p] for p in Permission if p in perms]`, so the template never imports anything). `current_user` has `name`, `role_label`, `email` (may be `None`), and optionally `last_login_at` (edge only; template guards with `is defined`).
- Page structure (extends `base.html`, `title` block "You · Vending Machine", `body` block):
  1. A white card (`bg-white rounded-xl border border-gray-200 shadow-sm p-5`) with `<h1 class="text-hero font-semibold">{{ current_user.name }}</h1>` and, when `current_user.email`, a `text-sm text-gray-500` line with the email.
  2. Role line in the same card, `text-base text-gray-700`:
     - `role_label` and `machine_label` set: "**{{ role_label }}** on {{ machine_label.display }}"
     - `role_label` set, `machine_label` None: "Role: **{{ role_label }}**"
     - `role_label` None: "No machine selected" followed by `<a href="/fleet" class="underline">Choose one</a>`
  3. Card "What you can do here": `<h2 class="text-lg font-semibold">` then `<ul class="list-disc pl-5 space-y-1">` of `permission_lines`, or `<p class="text-gray-500">Nothing on this page yet.</p>` when empty.
  4. When `current_user.last_login_at is defined and current_user.last_login_at`: a `text-sm text-gray-500` line "Last signed in {{ current_user.last_login_at }}".
  5. `<button hx-post="/logout" class="touch-target w-full rounded-md bg-slate-800 text-white font-medium px-4 py-3">{{ logout_label }}</button>`.

- [ ] **Step 1: Write render tests** in `tests/ui/test_me_template.py` using `templates.get_template("me.html").render(...)` with `SimpleNamespace` users: (a) edge shape (role `owner`, `machine_label=MachineLabel("Lobby Ice", "ice-07")`, `logout_label="Lock"`, three permission lines, `last_login_at="2026-10-23 09:00"`) shows name, "owner on Lobby Ice (ice-07)", each line, "Last signed in", a `hx-post="/logout"` button labelled Lock; (b) cloud shape (`role_label="tech"`, no `last_login_at` attribute, `logout_label="Sign out"`) shows "tech on" and "Sign out" and no "Last signed in"; (c) `role_label=None, machine_label=None` shows "No machine selected" and `href="/fleet"`; (d) empty `permission_lines` shows "Nothing on this page yet."; (e) the rendered page contains the chip from Task 3 (`href="/me"` in the bar) and the crumb "You".
- [ ] **Step 2: Run** `uv run pytest tests/ui/test_me_template.py -v`; expect TemplateNotFound.
- [ ] **Step 3: Write** `me.html`.
- [ ] **Step 4: Rebuild Tailwind**, run `uv run pytest tests/ui -v`; expect all passed.
- [ ] **Step 5: Commit** `feat(ui): me.html, the shared You page` including `app.css`.

---

### Task 5: Edge: `TemplateUser.role_label` and `GET /me`

**Files:**
- Modify: `packages/edge/src/ice_edge/web/auth.py` (`TemplateUser`, around line 177)
- Create: `packages/edge/src/ice_edge/web/routes/me.py`
- Modify: `packages/edge/src/ice_edge/web/routes/__init__.py` (import `me`, include its router alongside `controls`)
- Test: `tests/edge/web/test_me.py` (new), `tests/edge/web/test_web_auth.py` (one test for `role_label`)

**Interfaces:**
- Produces `TemplateUser.role_label` (property) `-> str` returning `self.role.value`.
- Route `GET /me`, dependency `web_auth.require(Permission.view_status)`, renders `me.html` with `context.template_context(request, level=LEVEL_ME, logout_label="Lock", machine_label=MachineLabel(common_name=cfg.physical.common_name, machine_id=cfg.machine_id), permission_lines=[PERMISSION_LABELS[p] for p in Permission if p in perms])` where `cfg = context.config` (read live like the other routes do; if `context.config` is `None`, pass `machine_label=None`) and `perms` comes from the same context dict (`ctx["perms"]`). Handler is a module-level async function `get_me`.

- [ ] **Step 1: Tests** in `tests/edge/web/test_me.py` using the `login_as` fixture (`tests/conftest.py:225`; non-owner roles accept `name=`):
  - parametrised over the four roles: `GET /` is 200 and the bar (slice between `id="bar"` and `</header>`) contains `href="/me"`, the role's `.value` and the user's name.
  - `GET /me` per role: 200, contains the name, "on" plus the config's `physical.common_name` and `machine_id`, exactly the labels of `ROLE_PERMISSIONS[role]` (every expected label present; every label for a permission the role lacks absent), `hx-post="/logout"`, and the word `Lock`.
  - anonymous client: `GET /me` is a redirect to `/login` (match whatever `/controls` does for an anonymous client in `tests/edge/web/test_routes_controls.py`).
  - `GET /login` (anonymous) contains no `href="/me"`.
  - setup mode: use `tests/edge/web/test_web_routes.py`'s existing setup-mode fixture pattern; `GET /setup` contains no `href="/me"`.
- [ ] **Step 2: `role_label` test** in `tests/edge/web/test_web_auth.py`: `web_auth.TemplateUser(..., role=Role.tech, ...).role_label == "tech"`.
- [ ] **Step 3: Run** `uv run pytest tests/edge/web/test_me.py tests/edge/web/test_web_auth.py -v`; expect failures (404 on `/me`, AttributeError on `role_label`).
- [ ] **Step 4: Implement** the property, `routes/me.py`, and the include in `routes/__init__.py`.
- [ ] **Step 5: Run** `uv run pytest tests/edge/web tests/meta -v -x`; expect all passed (the routes-shape and private-access ratchets included). Run ruff and `uv run lint-imports`.
- [ ] **Step 6: Commit** `feat(edge): /me level and role_label on the template user`.

---

### Task 6: Cloud: role on the viewer, `GET /me`, banner common name

**Files:**
- Modify: `packages/cloud/src/ice_cloud/viewer.py` (`RemoteViewer` gains `email: str | None = None`, `role_label: str | None = None`)
- Modify: `packages/cloud/src/ice_cloud/auth.py` (new helper `machine_role`)
- Modify: `packages/cloud/src/ice_cloud/portal.py` (`Selection`, `_selected`, `_context`, `_remote`, new `me` route before the `/{path:path}` catch-all)
- Test: `tests/cloud/test_me.py` (new), `tests/cloud/test_auth.py` (one test for `machine_role`)

**Interfaces:**
- `ice_cloud.auth.machine_role(settings, repo, account, fingerprint) -> Role | None`: `repo.access_for(account.id, fingerprint)`, else `"admin"` when `is_admin(settings, account)`, else `None`. `require_machine_access` is refactored to call it (behaviour unchanged, its own tests stay green).
- `portal.Selection` (frozen dataclass): `machine: Machine`, `report: MachineReport | None`, `role: Role`. `_selected` returns `Selection | None`; every caller (`portal.py` lines 230, 257, 290, 315, 339, 378, 410, 436) is updated from tuple unpacking to attribute access.
- `_context(..., role=None)` gains a keyword `role: Role | None`; it builds `RemoteViewer(display_name=account.name, email=account.email, role_label=role)`. Every machine-page route passes `role=selected.role`; fleet-wide callers (`alerts/pages.py`, `people.py`, `fleet`, `not_remote` without a selection) pass nothing, so `role_label` stays `None`.
- `_remote` adds `"common_name": report.common_name`.
- Route `GET /me` (`require_signed_in`): `selected = await _selected(...)`; when `None`, render `me.html` from the installed UI (`_resolve(request, None)`) with `role=None, machine_label=None`; else render through the selected report's UI with `machine_label=MachineLabel(common_name=report.common_name if report else None, machine_id=report.machine_id if report else None, short_fingerprint=short_fingerprint(machine.fingerprint))`, `role=selected.role`, `machine=`/`report=` for the banner. Always `logout_label="Sign out"` and `permission_lines=[PERMISSION_LABELS[p] for p in Permission if p in REMOTE_VIEWER_PERMISSIONS]`. Note the template context key `machine` collides with `_context`'s `machine: Machine | None` banner parameter: pass the banner one as the keyword `machine=` and put the label under `machine_label=`; then in `me.html` (Task 4) read `machine_label`, not `machine`. **Task 4's template and tests must use the name `machine_label`; the edge route in Task 5 passes `machine_label=` too.**

- [ ] **Step 1: Tests** in `tests/cloud/test_me.py` using `tests/cloud/conftest.py` helpers (`login`, `register`, `push`, `claim`, `select`, `own_and_select`, `short`) and `tests/cloud/reports.py`'s `make_report`:
  - owner selects a machine whose report has `common_name="Lobby Ice"`: `GET /` bar contains `href="/me"`, `owner`, and the account name; `GET /me` is 200, contains "owner on Lobby Ice (m-0042)", the `view_status` label, "Sign out", and no "Last signed in".
  - tech membership (use the repo's membership call the way `tests/cloud/test_people.py` does) shows `tech`; an admin (email in `settings.admins`, see `make_settings`) who is not a member shows `admin`; an admin who is also the owner shows `owner`.
  - `GET /fleet` signed in: bar has the name link and no role span (assert `rounded-full bg-slate-600` absent inside the bar slice).
  - signed in, nothing selected: `GET /me` is 200 (not a redirect, not `not_remote.html`), contains "No machine selected" and `href="/fleet"`.
  - selected machine with a `0.3.0` report (no `common_name`): `/me` says "owner on m-0042".
  - anonymous `GET /me` redirects to login like other pages (`test_portal.py::test_anonymous_machine_pages_redirect_to_login` shape).
  - the remote banner on `GET /health` shows "Lobby Ice" before "m-0042".
- [ ] **Step 2: `machine_role` test** in `tests/cloud/test_auth.py`: member -> `"tech"`, non-member admin -> `"admin"`, non-member non-admin -> `None`.
- [ ] **Step 3: Run** `uv run pytest tests/cloud/test_me.py tests/cloud/test_auth.py -v`; expect failures.
- [ ] **Step 4: Implement** the viewer fields, `machine_role`, `Selection`, the `_context` role, `_remote` common name, and the `/me` route.
- [ ] **Step 5: Run** `uv run pytest tests/cloud tests/ui -v -x`; expect all passed, including `test_ui_parity.py` unchanged. Run ruff and `uv run lint-imports`.
- [ ] **Step 6: Commit** `feat(cloud): /me with the viewer's machine role, common name on the banner`.

---

### Task 7: Docs

**Files:**
- Modify: `CLAUDE.md` (the Web Dashboard URL table: add `/me` gated on `view_status`; the "24 named levels" sentence becomes 25; a sentence in the Cloud "Portal" bullet: `/me` is served remotely and shows the viewer's machine role; the Cloud contract note: 0.4.0 adds `common_name`, cloud before edges)
- Modify: `docs/superpowers/specs/2026-10-23-signed-in-identity-design.md` (rename `MachineIdentity` to `MachineLabel` and the context key to `machine_label`, with a one-line note why)

- [ ] **Step 1: Make the edits.**
- [ ] **Step 2: Run** `uv run pytest tests/ui tests/edge/web tests/cloud tests/contracts tests/meta -q`; expect all passed.
- [ ] **Step 3: Commit** `docs: record the /me level, contract 0.4.0 and the MachineLabel naming`.

---

## Self-review

- Spec coverage: §1 (Tasks 3, 5, 6), §2 (Task 1), §3 (Task 3), §4 (Tasks 4, 5, 6), §4a (Tasks 2, 3, 6), §5 tests (each task), §6 n/a.
- Naming: the template context key for the machine label is **`machine_label`** everywhere (Tasks 4, 5, 6), because the cloud's `_context` already takes `machine=` for the banner. The view-model class is `MachineLabel` in `ice_ui.views.identity`.
- Ordering: Task 4 depends on Tasks 1 and 3; Task 5 on 1, 3, 4; Task 6 on 1, 2, 3, 4. Tasks 1 and 2 are independent of each other.
