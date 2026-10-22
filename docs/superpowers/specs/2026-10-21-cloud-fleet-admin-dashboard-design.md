# Cloud fleet admin dashboard — design

**Date:** 2026-10-21
**Status:** designed autonomously at the owner's request ("develop this webview without any further input from me"); builds on `2026-10-19-cloud-identity-tenancy-design.md` and `2026-10-19-cloud-alert-forwarding-design.md`.
**Decisions taken in this spec (no owner input available):** `/admin` becomes the dashboard and the existing management lists move under it (§3); "silent" reuses the portal's 3 × interval rule (§2.2); cloud templates join the Tailwind content scan so the dashboard is not limited to classes the edge happens to use (§6); every number on the page is computed by one pure function from data already stored, no new wire data and no new tables (§4).

## 1. Goal

The platform admin opens `/admin` and sees the whole fleet at a glance: how many machines exist, how
many owners, how many unclaimed machines are calling in, how many have gone silent, what is faulted,
what the fleet earned in the last day, which software it runs — then drills into any of those numbers
to the list behind it, and from any list row to the machine's own portal pages.

**Non-goals:** charts rendered by JavaScript (the dashboard is server-rendered HTML plus HTMX, like
everything else), history or trends (the cloud keeps only the latest report per machine; trend
storage is a later spec), per-owner dashboards (owners keep `/fleet`), remote commands, exporting
CSV, and any change to what the edge sends.

## 2. Vocabulary

Every term below is computed in one place, `ice_cloud/fleet_view.py` (§4), and used identically by
the overview, the drill-down lists and the tests.

### 2.1 Machine status

A machine is one `machines` row (a registered key). Its status is one of:

| Status | Rule |
|---|---|
| `online` | has a stored report and `now - report.sent_at <= 3 * report.report_interval_seconds` |
| `silent` | has a stored report and the age exceeds that limit |
| `never_reported` | registered, no stored report |

`STALE_FACTOR = 3` moves from `portal.py` to `fleet_view.py`; `portal.py` imports it from there so
the fleet list's Online/Offline badge and the dashboard can never disagree. The offline sweep's
`OFFLINE_INTERVALS = 3` is the same number by convention and stays where it is (it is an alerting
rule, not a display rule).

### 2.2 The four headline counts

| Headline | Definition |
|---|---|
| **Machines** | all registered machines; sub-line: claimed / unclaimed |
| **Owners** | distinct accounts that own at least one machine; sub-line: total accounts, suspended |
| **Unclaimed calling in** | `owner_id is None` and status `online` — a machine someone installed and never claimed; sub-line: unclaimed but silent or never reported |
| **Silent** | status `silent`, claimed or not; sub-line: never reported |

### 2.3 Secondary counts

| Count | Definition |
|---|---|
| Faulted | `online` machines whose report has at least one fault; sub-line: of which with a `safety`-gate fault |
| Payment blocked | `online` machines whose `health.payment_enabled` is `False` |
| Alerts (24 h) | `raised` + `offline` events across all machines in the last 24 h (same rule as `/fleet`'s per-machine count) |
| Revenue (24 h) | sum of `kpi.summary.money_in` over `online` machines with a summary; sub-line: products out, vends failed |
| Uptime (24 h) | mean of `kpi.summary.uptime_pct` over the same machines; `—` when none |
| Software | distinct `edge_build` values across machines with a report; sub-line: machines whose `ui.digest` differs from the cloud's installed `ice_ui` |

Silent machines are excluded from revenue and uptime because their 24 h window is stale; the card
says "across N online machines".

### 2.4 Attention list

The overview ends with "Needs attention": up to 12 rows, in this order, then by label:

1. `online` machines with a `safety`-gate fault — reason: the fault's code and description;
2. `silent` machines, longest silence first — reason: "silent for 3h 12m, expected every 60s";
3. `online` machines with `payment_enabled == False` — reason: the first payment reason;
4. unclaimed `online` machines — reason: "unclaimed, calling in since <registered_at>".

A machine appears once, under its first matching rule. Each row links to the machine (§5.3) and
shows the owner's email or "unclaimed".

## 3. Pages and URLs

All routes live under `/admin`, gated by `require_admin` (404 for anyone else, as today). The
existing `/admin` page is replaced; its two lists move to their own levels so the dashboard is the
landing page. Existing POST routes keep their URLs.

| URL | Level title | Content |
|---|---|---|
| `GET /admin` | Admin | the overview (§5.1) |
| `GET /admin/overview` | — | the overview's main content as an HTMX fragment, polled every 60 s |
| `GET /admin/machines` | Machines | every machine as a table (§5.2); query `status=online\|silent\|never_reported`, `claimed=yes\|no`, `faulted=yes`, `payment=blocked`, `owner=<account_id>`; filters AND together; unknown values are ignored |
| `GET /admin/owners` | Owners | one row per owner (§5.4) |
| `GET /admin/accounts` | Accounts | the existing account list with Suspend/Unsuspend (moved from `/admin`) |
| `GET /admin/alerts` | Alerts | the latest 100 alert events across the fleet (§5.5) |
| `GET /admin/machines/{short}/unbind/confirm`, `POST /admin/machines/{short}/unbind` | — | unchanged |
| `POST /admin/accounts/{id}/suspend`, `.../unsuspend`, `POST /admin/test-email` | — | unchanged |

Levels: `LEVEL_ADMIN = Level("Admin", "/admin")` (no parent, so the bar's Back goes to `/`, as
today); `LEVEL_ADMIN_MACHINES`, `LEVEL_ADMIN_OWNERS`, `LEVEL_ADMIN_ACCOUNTS`, `LEVEL_ADMIN_ALERTS`
are its children. A filtered machine list is `Level.child(LEVEL_ADMIN_MACHINES, <filter title>,
<url with query>)`, so the breadcrumb reads Admin / Machines / Silent.

`/fleet` gains, for admins only, a one-line link "Fleet dashboard" to `/admin` (owners and techs see
no change). The SMTP status line and "Send test email" button move to `/admin/accounts`' header,
beside the people they concern.

## 4. The view model (`ice_cloud/fleet_view.py`, pure)

No I/O, no FastAPI, no SQL. One entry point:

```python
def build_fleet_view(
    *,
    machines: Sequence[Machine],
    accounts: Sequence[Account],
    memberships: Sequence[Membership],
    reports: Mapping[str, MachineReport],      # fingerprint -> latest report
    alert_counts: Mapping[str, int],           # fingerprint -> raised+offline in 24 h
    installed_digest: str,
    now: datetime,
) -> FleetView
```

Frozen dataclasses:

- `MachineRow`: `fingerprint`, `short`, `label`, `owner_id`, `owner_email` (`None` when unclaimed),
  `status: MachineStatus`, `age_seconds: float | None`, `age_text`, `interval_seconds: int | None`,
  `fault_count`, `top_fault: FaultView | None` (highest gate: safety > fulfillment > alert, then code),
  `has_safety_fault`, `payment_enabled: bool | None`, `payment_reason: str | None`,
  `edge_build: str | None`, `ui_mismatch: bool`, `alerts_24h: int`, `claim_open: bool`,
  `registered_at`, `last_seen_at`, `vmc_missing: bool`.
- `Headline`: `label`, `value: int | str`, `sub: str`, `url: str`, `tone: Literal["neutral","good","warn","bad"]`.
  Tone rules: Silent and Unclaimed calling in are `bad` when > 0 else `good`; Faulted is `bad` when
  any safety fault, `warn` when > 0, else `good`; Payment blocked `warn` when > 0; everything else
  `neutral`.
- `StatusMix`: `online_ok`, `online_faulted`, `silent`, `never_reported` counts plus the same four
  as integer percentages that sum to 100 (largest-remainder rounding; all zero when there are no
  machines), for the one-bar status strip.
- `BuildRow`: `edge_build`, `count`, `online`.
- `OwnerRow`: `account_id`, `email`, `name`, `suspended`, `machines`, `online`, `silent`,
  `never_reported`, `faulted`, `techs` (distinct tech account ids over the owner's machines),
  `alerts_24h`, `url` (`/admin/machines?owner=<id>`).
- `AttentionRow`: `row: MachineRow`, `reason: str`, `tone`.
- `FleetView`: `headlines: tuple[Headline, ...]` (the four of §2.2 in order), `secondary:
  tuple[Headline, ...]` (§2.3 in order), `mix: StatusMix`, `attention: tuple[AttentionRow, ...]`,
  `builds: tuple[BuildRow, ...]` (count desc, then build), `owners: tuple[OwnerRow, ...]` (machines
  desc, then email), `rows: tuple[MachineRow, ...]` (status severity silent > never > online, then label).

Helpers exported for routes and tests: `classify(report, now) -> MachineStatus`,
`is_stale(report, now) -> bool` (what `portal._is_stale` becomes), `filter_rows(rows, *, status=None,
claimed=None, faulted=None, payment=None, owner=None) -> list[MachineRow]`, and `humanize_seconds`
is reused from `ice_ui.filters`.

## 5. Rendering

### 5.1 Overview (`admin.html` + `partials/admin_overview.html`)

`admin.html` extends `base.html` and contains one `<div id="fleet-overview" hx-get="/admin/overview"
hx-trigger="every 60s" hx-swap="outerHTML" hx-target="this">` that includes the partial; the
fragment route renders the partial alone with the same context, so the first paint and every poll
agree (the `/health/subsystems/{name}/live` pattern). `hx-target="this"` is load-bearing for the
same reason it is on the edge's panels: `<body>` carries `hx-target="main"`.

Inside the partial, top to bottom:

1. **Headline strip** — four stat cards (`partials/admin_stat.html`: label, big value, sub-line,
   whole card is a link; tone sets the value colour: `good` green-700, `warn` amber-600, `bad`
   red-600, `neutral` gray-900). Grid `grid-cols-2 lg:grid-cols-4`.
2. **Status strip** — one full-width bar made of four `<div>` segments whose `style="width: N%"`
   comes from `StatusMix` percentages (green online-ok, amber online-faulted, red silent, gray
   never-reported), with a legend of the four counts linking to the matching filtered machine list.
   Inline `style` is used because a width is data, not a class; a zero-width segment is omitted.
3. **Secondary strip** — six smaller stat cards for §2.3, `grid-cols-2 md:grid-cols-3 lg:grid-cols-6`.
4. **Needs attention** — the §2.4 rows as a bordered list; a tone dot, label, short fingerprint,
   owner, reason, age. "Nothing needs attention." when empty.
5. **Two half-width panels** on `lg`: **Owners** (top eight `OwnerRow`s: email, machines, online,
   silent, faulted, with "All owners →") and **Software** (`BuildRow`s with a count bar, and the UI
   mismatch count).
6. A footer line: "N machines · computed at HH:MM:SS UTC · refreshes every minute".

### 5.2 Machines (`admin_machines.html`)

A filter bar of pill links (All, Online, Silent, Never reported, Unclaimed, Faulted, Payment
blocked; the active one filled slate-800) followed by a table in an `overflow-x-auto` card:
Status (badge), Machine (label, short fingerprint, `vmc_missing` marker), Owner (email or
"unclaimed", "claim open" when a code is live), Last report (age; "never"), Faults (count + top code
coloured by gate), Payment (On/Off/—), Build (`edge_build`; "UI differs" when `ui_mismatch`),
Alerts 24 h, Actions (Unbind for a claimed machine, the existing two-tap partial). Row count and the
active filter appear above the table. An `owner=` filter shows the owner's email in the heading and
the breadcrumb. Empty state: "No machines match."

### 5.3 Where a row links

- Label → `/m/{short}` (selects the machine and lands on `/fleet`, the portal's own safe deep link).
- Alerts count → `/machines/{short}/alerts`.
- Owner email → `/admin/machines?owner=<id>`.

### 5.4 Owners (`admin_owners.html`)

Table: Owner (email, name, Suspended badge), Machines, Online, Silent, Never, Faulted, Techs,
Alerts 24 h; the machines count links to the owner-filtered list. Sorted by machines desc. Footer:
"N owners · M accounts own nothing" (tech-only or idle accounts).

### 5.5 Alerts (`admin_alerts.html`)

The newest 100 `alert_events` across all machines: time (UTC), machine label + short fingerprint
(linking to that machine's alert page), kind badge (`raised` red, `cleared` green, `offline` amber,
`online` gray), code, description, product. No delivery lines (that detail stays on the per-machine
page). Empty state: "No alerts yet."

### 5.6 Accounts (`admin_accounts.html`)

The existing accounts list and its Suspend/Unsuspend buttons, unchanged in behaviour, plus the SMTP
status line and the test-email button that used to sit at the top of `/admin`.

## 6. Styling and the stylesheet

The portal serves `ice_ui`'s committed `app.css`, and Tailwind's content scan today covers only
`packages/ui/src/ice_ui/templates/**`. The cloud's templates are added to `content` in
`packages/ui/tailwind.config.js` so any Tailwind utility the dashboard uses is compiled in; `app.css`
is rebuilt with the standalone binary and committed as before. `tests/cloud/test_cloud_static_css.py`
already fails on a class the stylesheet lacks and keeps doing so. The change to `app.css` changes
`ui_digest()`, which is fine: edge and cloud ship the same `ice_ui` package and the parity test
compares rendered HTML, not digests.

Visual language stays the dashboard v2 shell's: slate-50 page, white `rounded-xl` cards with
`border-gray-200 shadow-sm`, uppercase `text-xs` labels, `text-3xl font-bold` values, the existing
green/amber/red status colours, `touch-target` buttons. No new fonts, no JavaScript beyond HTMX.

## 7. Data access

Three `Repository` methods are added (SQL only in `store/sqlite.py`):

- `all_memberships() -> list[Membership]`.
- `alert_counts_since(since: datetime) -> dict[str, int]` — one `GROUP BY fingerprint` over
  `raised`/`offline` events (the fleet page's N-query loop may switch to it later; out of scope).
- `recent_alert_events(*, limit: int) -> list[tuple[StoredEvent, Machine]]` — newest first, joined
  to the machine for its label.

The overview route gathers `all_machines`, `all_accounts`, `all_memberships`,
`alert_counts_since(now - 24 h)` and `store.machines()` (one directory scan; each machine's JSON
is read once), each in `asyncio.to_thread`, then calls `build_fleet_view`. Nothing is cached; at a
few hundred machines this is a handful of milliseconds plus one file read per machine.

## 8. Module layout

`ice_cloud/admin.py` becomes the package `ice_cloud/admin/`:

| Module | Holds |
|---|---|
| `__init__.py` | `router` (includes the sub-routers), `LEVEL_ADMIN` and the child levels |
| `common.py` | `admin_context(...)`, `templates(request)`, `machine_or_404`, `load_fleet_view(request) -> FleetView` |
| `overview.py` | `GET /admin`, `GET /admin/overview` |
| `machines.py` | `GET /admin/machines`, the unbind confirm/POST (moved) |
| `owners.py` | `GET /admin/owners` |
| `accounts.py` | `GET /admin/accounts`, suspend/unsuspend, test-email (moved) |
| `alerts.py` | `GET /admin/alerts` |

`app.py`'s `from ice_cloud import admin` and `admin.router` keep working. Each sub-module exposes
its own module-level `router`, matching the edge's routes convention.

## 9. Security

Everything under `/admin` is `require_admin` (404 otherwise). Every mutating route keeps
`require_htmx`. Query parameters are compared against fixed vocabularies and an `owner` id is only
ever used as a dictionary key; nothing is interpolated into SQL. The overview fragment is served
only to admins and renders no untrusted bundle (always the installed `ice_ui` plus cloud templates).
Owner emails are shown to the admin, who already sees them on today's `/admin`.

## 10. Testing

- `tests/cloud/test_fleet_view.py` — the pure model: `classify` at the boundary (exactly
  3 × interval is `online`, one second more is `silent`); each headline and tone from a small
  synthetic fleet (two owners, one unclaimed online, one silent, one never reported); `StatusMix`
  percentages sum to 100 and are all zero for an empty fleet; attention ordering and
  one-row-per-machine; owner aggregation including distinct tech count; `filter_rows` for each
  filter and their conjunction; revenue excludes silent machines.
- `tests/cloud/test_admin_dashboard.py` — routes through `TestClient`: non-admin gets 404 on every
  new GET; `/admin` shows the four headline values for a seeded fleet; `/admin/overview` renders the
  same numbers and no `<html>`; each `/admin/machines` filter returns the right labels and an
  unknown filter value is ignored; `owner=` narrows to that owner's machines and names them in the
  heading; `/admin/owners` counts; `/admin/alerts` lists events newest first and links to the
  machine; `/admin/accounts` still suspends; `/fleet` shows the dashboard link to an admin and not to
  an owner.
- `tests/cloud/test_repository.py` (or the existing repo test module) — the three new methods.
- Existing `tests/cloud/test_admin.py` moves its list assertions from `/admin` to `/admin/machines`
  and `/admin/accounts`.
- `tests/cloud/test_cloud_static_css.py` and `tests/ui/test_static_css.py` unchanged and green after
  the rebuild.

## 11. Documentation

`packages/cloud/README.md` "Platform admin" describes the dashboard, its levels and vocabulary;
`CLAUDE.md`'s Cloud section gets one bullet; this spec records as-built deviations inline if any.
