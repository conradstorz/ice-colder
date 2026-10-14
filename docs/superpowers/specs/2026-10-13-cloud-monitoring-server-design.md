# Cloud Monitoring Server (Stub) — Design Spec

Date: 2026-10-13. Scope: the new `ice_cloud` package, the `ice_contracts.cloud` models, and the edge uplink
that feeds them. Companion to [`docs/vending-system-reference-architecture.md`](../../vending-system-reference-architecture.md)
§7–§8 and [`2026-10-13-layer-contracts-design.md`](2026-10-13-layer-contracts-design.md) §1 and §4.

The stub answers one question: can an operator away from the machine see the machine's own dashboard,
rendered from the machine's own templates, even while the machine is offline? Everything else the
reference architecture puts in the cloud is deferred (§10).

## 1. Scope

The stub is a **read-only fleet portal plus ingest**:

- **Ingest:** machines push a `MachineReport` (§6) and, once per UI version, a UI bundle (§3).
- **Portal:** a fleet list, plus the machine's Home, hero, KPI, pill, Health and Faults pages, rendered
  from the latest stored report.

Explicitly **out of scope**:

- authentication;
- multi-tenancy;
- shadow and commands (typed stubs only — `ShadowService` and `CommandService` raise `NotImplementedError`);
- the mobile route app;
- video;
- MQTT/mTLS transport;
- payment webhooks.

Reference architecture §8 and layer-contracts spec §4 remain the target. Nothing here weakens them; the
stub is the smallest thing on the path to them that is useful on its own.

## 2. UI parity rule

- The cloud renders **the same template files** as the machine (`ice_ui`). No portal page has its own copy
  of a machine template, and no machine template branches on "am I in the cloud" except through the one
  `remote` banner include (§4).
- Only cloud-only pages (the fleet list) live in `ice_cloud/templates`, prepended through
  `make_templates(..., extra_dirs=[cloud templates])`. `fleet.html` and `not_remote.html` extend the
  shared `base.html`.
- The viewer's permissions are `REMOTE_VIEWER_PERMISSIONS` (today `frozenset({Permission.view_status})`,
  `ice_contracts.access`). Per reference architecture §7 — "local and remote use the same API … subset of
  the cloud portal, filtered by the same permission model" — the machine dashboard is a subset of the
  portal, filtered by the same permission model. Templates already hide every control the viewer's
  `perms` lack, so the remote viewer sees no Clear, Record sale, or Discard button, with no template edit.
- **Package boundary.** `ice_edge` and `ice_cloud` are import-linter siblings and may not import each
  other; both may import `ice_ui` and `ice_contracts`. Any computation both sides need therefore moves
  down into `ice_ui` (Task 12's move list), so the cloud never re-implements it:
  - `pill_state(health) -> tuple[str, str]` — the pill's three-way logic from `ice_edge/web/routes/home.py`,
    including its severity ranking (`_SEVERITY_RANK`, `_highest_priority_fault`):
    `("neutral", "—")` when `vmc_missing`; `("ok", "OK")` when `is_healthy`; otherwise
    `("fault", <top fault's code>)`, or `("fault", "ISSUE")` when there is no active fault;
  - `health_tiles(...)` in `ice_ui.tiles` — the `/health` four-tile list built inline today in
    `ice_edge/web/routes/health/overview.py` (titles Subsystems / Faults / Availability / Logs, their
    `LEVEL_HEALTH_*` urls, icon `"health"`, `coming_soon=False`, Logs enabled only with `view_logs`),
    taking the four context lines and `perms`;
  - the tile context-line formatters, with the edge's missing-data behaviour exactly:
    - fault count: `"{n} active fault(s)"`, or `"—"` when no VMC is wired (remotely: `health.vmc_missing`);
    - availability: `"Payment enabled"`, `"Payment disabled ({n})"`, or `"—"` when no Availability is
      wired (remotely: `health.payment_enabled is None`).
  `fault_gate` stays on the edge: the edge precomputes `gate` into each `FaultView` (§6).
- **Home tiles.** The portal's `GET /` calls `ice_ui.tiles.build_tiles(REMOTE_VIEWER_PERMISSIONS, lines)`
  and never keeps its own tile table. With `{view_status}` only the **Health** tile is enabled; its line is
  the fault-count formatter over `len(report.faults)` (`"—"` when `health.vmc_missing`). The other seven
  lines may be passed as `""`; disabled tiles are not rendered.
- **Health sub-tiles** (`/health`): the portal calls `health_tiles(...)` with Subsystems →
  `"{subsystems_ok}/{subsystems_total} OK"` from `health_page`; Faults → the fault-count formatter;
  Availability → the availability formatter over `health.payment_enabled` and
  `len(health.payment_reasons)`; Logs is disabled (no `view_logs`). Subsystems and Availability link to
  pages the portal answers with "Not available remotely yet" (§5).

## 3. UI version: "both"

The machine and the portal can run different `ice_ui` versions. The stub handles that both ways: the
portal renders its own UI when it matches, and the machine's own UI when it can.

- **Digest.** `ui_digest` is the sha256 over the sorted (posix relpath, NUL, file bytes, NUL) of every
  file under `templates/` and `static/`, followed by the canonical JSON (`sort_keys=True`,
  `separators=(",", ":")`) of `levels_manifest()`.
  - Relpaths are relative to the `ice_ui` package root, so they start `templates/` or `static/`.
  - `levels_manifest()` is every module-level `Level` in `ice_ui.levels` as `LevelEntry(title, url,
    parent_url)`, sorted by `url` — it is in the digest because breadcrumbs and Back links come from
    Python, not from template files.
  - Bytes are hashed as installed. A CRLF checkout yields a different digest from the wheel; that is a
    harmless mismatch (the cached bundle is used), not an error.
- **Reporting.** The machine reports its digest in every report (`ui.digest`).
- **Resolution.** The cloud renders with its installed `ice_ui` when digests match. Otherwise it uses a
  cached bundle for the reported digest. If none is cached, it falls back to the installed `ice_ui` and
  shows a visible "UI version mismatch" banner.
- **Upload.** Machines upload with `PUT /api/v1/ui-bundles/{digest}`, as a tar.gz containing
  `manifest.json` (`format: 1`, `digest`, `ice_ui_version`, `levels`), `templates/**` and `static/**`.
  Responses: 201 newly stored, 200 already present, 400 with the rejection reason.
- **Upload rejections:** absolute paths, `..` components, symlinks or hardlinks, non-regular members,
  members outside those three roots, total uncompressed size over 20 MB, or a recomputed digest that
  differs from the URL. The digest is recomputed from the extracted `templates/` and `static/` files plus
  the manifest's `levels`; the manifest's own `digest` must also equal the URL. Extraction goes to a
  temporary directory and is renamed into `cloud_data/ui/<digest>/` only after every check passes.
- **Sandboxing.** Cached bundles render in a Jinja `SandboxedEnvironment`. The installed `ice_ui` renders
  in the ordinary environment. Both get the same filters (`humanize_seconds`, `sku_url_segment`).

| Reported digest | Templates | `/static` | Mismatch banner |
|---|---|---|---|
| = installed | installed `ice_ui` | installed | no |
| cached bundle | bundle, sandboxed | bundle | no |
| unknown | installed `ice_ui` | installed | **yes** |

## 4. Offline semantics

- The cloud stores the latest `MachineReport` per machine (`cloud_data/machines/<machine_id>.json`,
  atomic tmp + rename). A report with the same `boot_id` and a `seq` ≤ the stored one is ignored; a new
  `boot_id` always replaces.
- A machine is **stale** when `now − sent_at > 3 × report_interval_seconds`, where `now` is the portal's
  clock at render time and `report_interval_seconds` is the value carried in that report.
- A stale machine still renders every remote page from its last report, read-only, under the banner
  "Offline — showing last report from <local time> (<age> ago)". `<local time>` is `sent_at` in the
  portal host's local timezone; `<age>` is `now − sent_at` through `humanize_seconds`.
- The cloud never contacts a machine; it only reads stored reports. There is no poll, no callback, no
  "refresh now".
- **Time-relative values are rendered as of `sent_at`, never extrapolated.** These are:
  - `since_seconds` on every fault (shown as "4m" / "4m ago");
  - `health_page.vmc_build.uptime_seconds` ("up 3h");
  - every KPI value (a trailing 24 h window ending at `sent_at`);
  - the `"N errors in last 24h"` and `"Stale subsystems: …"` lines inside `health.issues`;
  - `health_page.subsystems_ok` (staleness judged at `sent_at`).

  An online machine's report is at most `3 × report_interval_seconds` old, so the error is small; a stale
  machine carries the banner, which states the age.
- **Banner partial.** `base.html` gains one `{% if remote %}{% include "partials/remote_banner.html" %}{% endif %}`
  as the first child of `<body>`. The edge never passes `remote`, so the machine's own pages are
  byte-identical (golden test, Task 12). `remote` carries `machine_id`, `last_report_local`, `age_text`,
  `stale`, `ui_mismatch`, `report_digest` and `portal_digest`. The mismatch line reads
  "UI version mismatch: machine <report_digest[:8]>, portal <portal_digest[:8]>".

## 5. Machine selection

- Templates use absolute URLs (`/status`, `/health/faults`, `/static/app.css`). To keep them
  byte-identical, the portal selects a machine with a cookie (`ice_cloud_machine`, set by
  `POST /fleet/select/{machine_id}`) and serves the machine pages at the same paths.
  - The cookie is a selection, not a credential: `HttpOnly`, `SameSite=Lax`, value = `machine_id`.
  - `POST /fleet/select/{machine_id}` returns 303 to `/`; an unknown machine returns 404.
  - With no cookie (or a cookie naming a machine with no stored report), every machine page returns 303
    to `/fleet`.
- A per-machine URL prefix was rejected for the stub because it would change every template.
- `/static/{path}` is served from the selected machine's resolved UI, with `Path.resolve()` containment
  (an escape returns 404). With no machine selected, it serves the installed `ice_ui` static, so
  `/fleet` styles correctly.
- **Served paths.** `GET /fleet`, `POST /fleet/select/{machine_id}`, and the machine pages `GET /`,
  `/status`, `/pill`, `/kpi`, `/health`, `/health/faults`, each with the **same template name and context
  keys** as the edge route. Any other GET renders `not_remote.html` ("Not available remotely yet") with
  404. `base.html`'s Lock button posts to `/logout`, which the portal does not serve (404); acceptable for
  the stub.
- `current_user` is a `RemoteViewer(display_name="Fleet viewer")`. `base.html` reads no attribute of
  `current_user` today; if a later template does, `RemoteViewer` grows that attribute.

## 6. Data contract `MachineReport` v0.1.0

Module: `ice_contracts.cloud.report`. Every model is `ConfigDict(extra="forbid", frozen=True)`. The
generated JSON Schema lives under `docs/contracts/edge-cloud/schemas/`, drift-tested like the board
schemas. This contract follows layer-contracts §1.3's versioning rule; it does **not** yet use the §1.1
envelope.

### 6.1 Top level

| Field | Type | Notes |
|---|---|---|
| `contract` | `Literal["0.1.0"]` | `CLOUD_CONTRACT_VERSION` |
| `machine_id` | `str`, `^[A-Za-z0-9_.-]{1,64}$` | must equal the URL path's `machine_id` (else 422) |
| `sent_at` | `datetime`, UTC | |
| `boot_id` | `str`, uuid4 | one per edge process |
| `seq` | `int ≥ 0` | monotonic per `boot_id`, incremented per attempt |
| `report_interval_seconds` | `int`, 10–3600 | from `cloud.report_interval_seconds` |
| `edge_build` | `str` | commit short |
| `ui` | `UiIdentity{digest, ice_ui_version}` | `digest` is `^[0-9a-f]{64}$` |
| `health` | `HealthSummary` | §6.2 |
| `kpi` | `KpiSummary` | §6.3 |
| `faults` | `list[FaultView]` | §6.4 |
| `health_page` | `HealthPageSummary` | §6.5 — **addition beyond the Task 10 brief** |

### 6.2 `health: HealthSummary`

Exactly the fields of today's `health_snapshot()` dict (`ice_edge/web/context.py`), typed. Task 12's
parity test asserts `model_dump(mode="json")` equals the legacy dict, so no field is added or dropped,
even ones no template reads.

| Field | Type | Read by |
|---|---|---|
| `vmc_missing` | `bool` | `/status` route (inline "VMC not initialized" fragment), `/pill` |
| `status` | `StatusView \| None` | `status_fragment.html` |
| `is_healthy` | `bool \| None` | `status_fragment.html`, `/pill` |
| `issues` | `list[str]` | `status_fragment.html` (time-relative lines, §4) |
| `active_faults` | `list[ActiveFaultView]` | `status_fragment.html`, `/pill` ranking |
| `payment_enabled` | `bool \| None` | `status_fragment.html`, Availability tile line |
| `payment_reasons` | `list[str]` | `status_fragment.html`, Availability tile line |
| `machine_stopped` | `bool \| None` | `status_fragment.html` (tri-state) |
| `maintenance` | `str \| None` | `status_fragment.html` (holder display name) |

`StatusView` is all four `VMC.get_status()` keys: `state: str`, `selected_product: str | None` (the
selected product's **name**, not its sku; not read by a template; kept for parity), `credit_escrow: float`, `last_payment_method: str | None`.

`FaultBase` holds the seven `active_faults()` keys; `ActiveFaultView` (here) and `FaultView` (§6.4) both
extend it, so the shared keys are defined once. `ActiveFaultView` is the seven `FaultService.active_faults()` keys plus the `since_seconds` that
`health_snapshot` joins in — and nothing else (parity):
`key: str`, `sku: str | None`, `product: str | None`, `code: str`, `severity: str` (a `Severity` value),
`scope: str` (`"product"` | `"machine"`), `description: str`, `since_seconds: float | None`
(time-relative).

### 6.3 `kpi: KpiSummary`

Exactly the fields `partials/kpi_fragment.html` reads, which is two objects, each nullable as the route
passes them:

- `summary: KpiWindow | None` — `None` when no event recorder is wired (four placeholder cards).
  `KpiWindow`: `money_in: float`, `products_out: int`, `vends_failed: int`, `errors: int`,
  `uptime_pct: float`. Source: `EventRecorder.get_summary(24)`; its other four keys (`ice_cycles`,
  `service_door_opens`, `temp_exceedances`, `refunds`) are not read and are projected away.
- `average: KpiAverage | None` — `None` when no recorder. `KpiAverage`: `money_in`, `products_out`,
  `errors`, `uptime_pct`, each `float | None` (rounded means; all `None` with under two active days).
  Source: `get_historical_average(24)`, same projection.

All KPI values are time-relative (24 h ending at `sent_at`).

### 6.4 `faults: list[FaultView]`

The keys `FaultService.active_faults()` emits plus the optional `pending_sale` that `health_faults.html`
reads — **plus `since_seconds` and `gate`**, which that template also reads (the brief omitted them):

`key`, `sku`, `product`, `code`, `severity`, `scope`, `description` (inherited from `FaultBase`, §6.2);
`since_seconds: float | None`; `gate: Literal["safety", "fulfillment", "alert"]` (computed on the edge
by `fault_gate`); `pending_sale: PendingSaleView | None`.

`PendingSaleView` is exactly what the template reads: `sku: str`, `price: float`,
`methods: dict[str, float]`. The edge's `pending_sale_for_recovery()` dict also carries `name`, `slot`
and `saved_at`; the builder drops them. `dom_key` is excluded: the template reads it only inside
`{% if can_clear %}`, which is never true remotely. The portal passes `can_clear=False` and `oob=False`.

### 6.5 `health_page: HealthPageSummary`

`health.html` and its sub-tiles read data that `health_snapshot()` does not carry (it comes from
`HealthMonitor.get_summary()` and `_subsystem_summary()`), and §6.2's parity rule forbids adding it to
`HealthSummary`. So it gets its own field:

- `mqtt_connected: bool | None`, `vmc_state: str | None`;
- `vmc_build: BuildView | None` — `commit: str`, `commit_short: str`, `source: str`,
  `uptime_seconds: int` (time-relative), `machine_id: str | None` — the five keys `health.html` reads;
  `get_summary()["vmc"]`'s `build_time` and `python_version` are dropped. `None` when no health monitor
  (`health.html` then hides the identity strip);
- `subsystems_ok: int`, `subsystems_total: int` — the Subsystems tile's counts.

### 6.6 Example

```json
{
  "contract": "0.1.0", "machine_id": "m-0042", "sent_at": "2026-10-13T14:05:00Z",
  "boot_id": "6f1c2a7e-3d4b-4e0a-9b51-2c8f0d7a1e93", "seq": 118,
  "report_interval_seconds": 60, "edge_build": "6fa3512",
  "ui": {"digest": "3b9f0c2d8e41a7b65f90d3c1e28a4b7f6d05c9e1a3b2f48d7e6c5a0b9d1f2e3a", "ice_ui_version": "0.1.0"},
  "health": {
    "vmc_missing": false,
    "status": {"state": "idle", "selected_product": null, "credit_escrow": 0.0,
               "last_payment_method": "cash"},
    "is_healthy": false,
    "issues": ["ICE-301 Ice bin low (Bag of Ice)", "2 errors in last 24h"],
    "active_faults": [{"key": "ICE-10LB", "sku": "ICE-10LB", "product": "Bag of Ice",
                       "code": "ICE-301", "severity": "lockout", "scope": "product",
                       "description": "Ice bin low", "since_seconds": 412.3}],
    "payment_enabled": true, "payment_reasons": [], "machine_stopped": false,
    "maintenance": null
  },
  "kpi": {
    "summary": {"money_in": 42.0, "products_out": 14, "vends_failed": 1, "errors": 2,
                "uptime_pct": 99.3},
    "average": {"money_in": 38.5, "products_out": 12.4, "errors": 0.6, "uptime_pct": 98.9}
  },
  "faults": [{"key": "ICE-10LB", "sku": "ICE-10LB", "product": "Bag of Ice", "code": "ICE-301",
              "severity": "lockout", "scope": "product", "description": "Ice bin low",
              "since_seconds": 412.3, "gate": "fulfillment", "pending_sale": null}],
  "health_page": {
    "mqtt_connected": true, "vmc_state": "idle",
    "vmc_build": {"commit": "6fa3512c0e9d4b7a1f2e3c5d6b8a9f0e1d2c3b4a", "commit_short": "6fa3512", "source": "image",
                  "uptime_seconds": 10820, "machine_id": "m-0042"},
    "subsystems_ok": 3, "subsystems_total": 3
  }
}
```

(The fault code and description are illustrative; real values come from `FAULT_TABLE`.)

### 6.7 What replaces it

A later contract version replaces this HTTP snapshot with spec §4's envelope, `seq` watermark and outbox.
v0.1.0's `seq` is only a supersede marker; it is not a watermark and nothing is acked.

## 7. Transport (stub)

- The edge sends `POST /api/v1/machines/{machine_id}/report` (JSON → 204) and the bundle `PUT` above,
  both over plain HTTP.
- The edge's `cloud.enabled` defaults to **false**. `CloudConfig` also carries
  `url` (default `http://127.0.0.1:26124`) and `report_interval_seconds` (default 60, 10–3600), re-read
  each pass so an edit takes effect on the next report.
- Order: the bundle `PUT` first (retried once per interval until accepted), then reports forever.
- Uploads never block a sale: they run in a supervised background task with a 5 s timeout, and failures
  are logged and dropped. There is no outbox yet; each report supersedes the previous one.
- Building a report is a pure read of machine state: it changes nothing, raises no fault, and writes no
  event row.

## 8. Security (stub)

The stub is for development only: it must not be deployed anywhere reachable from beyond localhost.

There is no authentication. `ice_cloud` binds to `127.0.0.1` by default and its compose file is marked
dev-only. Auth and mTLS (spec §4) are **required before any deployment beyond localhost**.

Known exposures this accepts, all closed by that requirement:

- anyone who can reach the port can read every machine's report and push reports for any `machine_id`;
- a pushed bundle's `static/` files (JavaScript included) are served from the portal's origin. The
  sandbox constrains Jinja, not the browser;
- the selection cookie is not a credential and carries no integrity.

## 9. Deploy

- Separate image `packages/cloud/Dockerfile` (the edge pattern, `--package ice-cloud` only), port 26124.
- Data dir `ICE_CLOUD_DATA` (default `./cloud_data`, gitignored), holding `machines/` and `ui/`.
- Bind host `ICE_CLOUD_HOST` (default `127.0.0.1`).
- `docker/docker-compose.cloud.yml` sets `ICE_CLOUD_HOST=0.0.0.0` (needed inside the container so the
  port mapping can reach the server), mounts `./cloud_data`, and starts with
  `# dev only — no auth (cloud spec §8)`. Its port mapping is `127.0.0.1:26124:26124`: the host side stays
  loopback-only, so the container's `0.0.0.0` bind never exposes the portal to the LAN.
- CI builds the image on every PR and never publishes it. The `compose-config` job also checks the cloud
  compose file.

## 10. Future work

Matching gap analysis §4.4, in roughly this order:

1. **Device shadow** — `desired`/`reported` sections, all-or-nothing apply (layer-contracts §4.4).
2. **Signed remote commands with actor** — `body.actor`, cloud signature, edge re-check.
3. **Event stream with `seq` watermark** — durable outbox, `ack{through_seq}`, `cmd.resend`
   (layer-contracts §4.3); replaces §6's snapshot.
4. **Alerts** — per-role alert routing and escalation.
5. **Multi-tenancy** — `fleet/<tenant>/<machine>/…`.
6. **Offline-capable mobile route app.**
7. **Video** — clips on request or flagged event, never continuous.

Each item adds pages the portal serves at the machine's own paths, under a viewer whose permissions
widen beyond `REMOTE_VIEWER_PERMISSIONS` only once authentication exists.
