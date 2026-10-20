# Cloud alert forwarding — design

**Date:** 2026-10-19
**Status:** approved in brainstorm; builds on `2026-10-19-cloud-identity-tenancy-design.md` (branch `cloud/identity-tenancy`)
**Owner decisions in this spec:** events derived from reports (A); email via cloud SMTP (A); role defaults with per-person mute (A); the cloud *replaces* the edge's own alert email for a claimed machine (B); a per-machine alert history page (A).

## 1. Goal

When a machine raises or clears a fault, or stops reporting, the cloud emails the machine's owner and
its technicians, and the people who can open the machine in the portal can see what was sent and to
whom. The edge keeps its local alert path only as a fallback for when the cloud is unreachable.

**Out of scope** (later specs): SMS, escalation chains, per-fault routing, acknowledging or
snoozing an alert, forwarding `HealthMonitor` alerts that are not registry faults (temperature out of
range stays edge-local), invitation and claim emails (they get the mailer this spec adds but no new
behaviour), the public machine finder, remote commands.

## 2. Where alerts come from

Nothing new crosses the wire from the edge. Every `MachineReport` already carries the complete list of
active faults (`faults: list[FaultView]`, keyed by `key`, with `code`, `severity`, `scope`, `gate`,
`description`, `since_seconds`). The cloud derives events by comparing each report with the state it
kept from the previous one.

### 2.1 `AlertEngine` (`ice_cloud/alerts/engine.py`, pure)

```python
@dataclass(frozen=True)
class AlertEvent:
    fingerprint: str
    kind: Literal["raised", "cleared", "offline", "online"]
    at: datetime                      # cloud clock at ingest or sweep
    key: str | None                   # fault key for raised/cleared, None otherwise
    code: str | None
    severity: str | None
    gate: Literal["safety", "fulfillment", "alert"] | None
    description: str | None
    sku: str | None
    product: str | None

def diff_reports(previous_keys: frozenset[str], report: MachineReport, *, now: datetime, fingerprint: str) -> list[AlertEvent]
```

- A fault key in `report.faults` but not in `previous_keys` → `raised`.
- A key in `previous_keys` but not in the report → `cleared` (the stored state carries only keys, so
  a `cleared` event copies `code`/`description` from the last `raised` event with that key when the
  router needs them; the engine itself fills `code` from the key's `CODE[:sku]` form).
- Same key in both → nothing. A fault that raises and clears inside one report interval is never seen;
  the owner accepted that in exchange for no new edge contract.
- Events within one call are ordered `cleared` first, then `raised`, each by key.

*(As built: `diff_reports(previous: Sequence[FaultView], report, *, now, fingerprint)` takes the previous report's full `FaultView` list, not bare keys, because a product fault's wire `key` is the bare sku, so a `CODE[:sku]` key cannot be parsed back into a code. A `cleared` event copies `code`, `severity`, `gate`, `description`, `sku` and `product` from the previous `FaultView`; no key parsing exists anywhere.)*

### 2.2 Per-machine alert state

A new table `machine_alert_state (fingerprint PK, fault_keys_json, last_report_at, report_interval_seconds, offline_since)`
is the engine's memory. It is written by ingest on every accepted report and read on the next; the
snapshot file under `machines/` is never re-read for this purpose.

- The first report after `register` (a new `boot_id`, or no state row) diffs against the empty set, so
  faults that survived a machine restart are "raised" again once. This is intended: the owner's mailbox
  should say the machine came back with the fault still active.
- `claim_machine` deletes the row, so the new owner's first report starts clean and the previous owner's
  last faults are never re-sent to them.
- `unbind_machine` deletes the row and the machine's `alert_events`, `alert_deliveries` and `alert_prefs`.

*(As built: the state row's column is `faults_json`, the previous report's full `FaultView` list, and `AlertState.faults` is a tuple of `FaultView`; `AlertState.keys` remains as a derived property. The repository also exposes `delete_alert_state`.)*

### 2.3 Offline and online

`OfflineSweep` (`ice_cloud/alerts/sweep.py`) is an asyncio task started in the app lifespan and run every
60 s (`settings`-independent constant `SWEEP_SECONDS = 60`). For every **claimed** machine with a state
row and `offline_since IS NULL`: if `now - last_report_at > 3 * report_interval_seconds`, emit one
`offline` event and set `offline_since = now`. Ingest emits `online` when a report arrives for a machine
whose `offline_since` is set, and clears it. The threshold is the same 3 × rule the portal's Offline
banner uses, so the page and the email agree.

Unclaimed machines never produce events of any kind. A machine with no state row (registered, never
reported) is not "offline"; nothing has promised reports yet.

*(As built: startup grace. `OfflineSweep` records `started_at` at construction and never flags a machine offline while `now - started_at <= 3 * report_interval_seconds` for that machine, so a cloud restart after downtime does not email the fleet "offline" and then "online" as reports resume. The offline event and the `offline_since` mark are committed together by `record_alert_pass` before dispatch, so a crash cannot emit twice. Each tick calls `dispatch` for new offline events, then `flush` for leftovers.)*

### 2.4 Ingest hook

`POST /api/v1/machines/{fingerprint}/report` (`ice_cloud/ingest.py`) after `store.save(...)`:

1. `state = repo.alert_state(fingerprint)`
2. `events = diff_reports(state.keys, report, now=now(), fingerprint=fingerprint)` plus `online` if
   `state.offline_since`.
3. `repo.record_alert_events(events)` and `repo.put_alert_state(fingerprint, keys, report.sent_at→now, interval, offline_since=None)` in one transaction.
4. `await app.state.alerts.dispatch(fingerprint, events)` (section 3). Dispatch failures are logged and
   never fail the report (the machine still gets its 204).

All repository calls go through `asyncio.to_thread`, as the existing handlers do.

*(As built: steps 3 and 4 differ. Events and state are written by one repository call, `record_alert_pass(events, state)`, in a single transaction, used by both ingest and the sweep, so a crash between the two writes cannot leave events without state or the reverse. An unclaimed machine (`owner_id is None`) is skipped before any repository call. Ingest then calls `alerts.dispatch(fingerprint, machine, stored_events)` followed by `alerts.flush(now)`.)*

## 3. Routing and delivery

### 3.1 `Router` (`ice_cloud/alerts/router.py`, pure)

```python
@dataclass(frozen=True)
class Recipient:
    account_id: str
    email: str
    role: Literal["owner", "tech"]

def recipients_for(events: list[AlertEvent], people: list[tuple[Recipient, bool]]) -> dict[str, list[AlertEvent]]
```

`people` is every account with access to the machine paired with its `muted` flag. Rules:

| event | owner | tech |
|---|---|---|
| `raised`, gate `safety` or `fulfillment` | yes | yes |
| `raised`, gate `alert` (e.g. `DATA-101`) | yes | no |
| `offline`, `online` | yes | yes |
| `cleared` | rides along (3.3) | rides along (3.3) |

A muted person receives nothing and the delivery row says `muted`. A suspended account is excluded
before routing (the repository's `people_for_alerts` filters `suspended_at IS NOT NULL`).

### 3.2 `alert_prefs`

`alert_prefs (account_id, fingerprint, muted INTEGER NOT NULL, PRIMARY KEY (account_id, fingerprint))`.
No row means not muted. Owners and techs both have rows here; `revoke` and `claim_machine` (owner
change) delete the affected rows, so a mute never outlives access.

### 3.3 One email per recipient per machine per pass, with cooldown

`AlertDispatcher` (`ice_cloud/alerts/dispatcher.py`):

- For each recipient the router selects, collect the triggering events plus every `cleared` event for
  that machine since that recipient's last successful email (`alert_deliveries` tells it). `cleared`
  alone never sends; it waits for the next `raised`, `offline` or `online` email to that recipient.
- Cooldown: **10 minutes** per `(recipient, fingerprint)` (`COOLDOWN_SECONDS = 600`), measured from
  the last successful send. Events inside the window are written to `alert_deliveries` with status
  `pending` and go out, bundled, when the window closes; nothing is dropped.
- A pass is triggered by ingest (with the new events) and by the sweep (with its `offline` events).
  Both also flush any `pending` rows whose cooldown has expired, so a quiet machine's deferred email
  still leaves within the window plus one sweep.
- Delivery rows: `alert_deliveries (id, fingerprint, account_id, event_ids_json, status, attempted_at, sent_at, error)` with `status ∈ {pending, sent, failed, muted}`. A `failed` row is retried
  on the next pass and the cooldown is not consumed by a failure; after **5** failures it stays `failed`
  and the next event starts a fresh row.

*(As built: `dispatch(fingerprint, machine, events)` folds the recipient's other due rows (`pending`, or `failed` with `attempts < 5`) into the same email, and every folded row receives the outcome. Both ingest and the sweep therefore call `dispatch` first and `flush` after; flushing first would send the older rows alone and hold the new event behind the cooldown. A `failed` row is retried under the same per-recipient cooldown gate as a `pending` one (the gate reads the last `sent` delivery, so a failure never consumes the cooldown). One `asyncio.Lock` serialises `dispatch` and `flush`. The dispatcher truncates error text to 500 characters. A muted recipient gets a `muted` delivery row at dispatch time, and `flush` marks a due row `muted` if the person was muted after it was queued. A due row whose machine or recipient has vanished is marked `failed` with "machine or recipient no longer available" and burns an attempt on each later flush.)*

### 3.4 Email

Plain text, one per bundle:

```
Subject: [ice-colder] <label> (<short>): <n> raised, <m> cleared      (or "offline" / "back online")

Machine <label> (<xxxx-xxxx-xxxx-xxxx>)
<kind line, e.g. "Stopped reporting at 2026-10-19 14:02 UTC (last report 14:00)">

Raised
  WTR-104  critical  machine   Sump high-level float tripped
  CFG-101  product_unavailable  product Ice (sku ICE)  No valid dispenser profile

Cleared since your last email
  COM-101  Vending board heartbeat lost

Open the machine: <public_url>/m/<short>
You receive this as <owner|technician> of this machine. Mute: <public_url>/machines/<short>/people
```

The link uses `settings.public_url`; with no public URL the link lines are omitted.

*(As built: a bundle holding both `offline` and `online` events takes its subject from the newest of them ("offline" or "back online") and prints both kind lines in time order. The kind lines are "Stopped reporting at <ts> UTC (last report HH:MM)", "Reporting again at <ts> UTC" and, for fault bundles, "Fault changes at <ts> UTC". A fault bundle's subject counts the raised events and the cleared events riding along.)*

### 3.5 Mailer

`ice_common/mailer.py` receives the edge's `smtp_send(email_config, msg)` body, generalised to
`smtp_send(*, host, port, username, password, use_starttls, msg)`; `ice_edge.notify.mailer` keeps its
signature and calls through (as the edge re-exports `Backoff` and `hash_secret`). The cloud defines:

```python
class Mailer(Protocol):
    async def send(self, msg: EmailMessage) -> None: ...   # raises on failure

class SmtpMailer(Mailer)        # asyncio.to_thread(smtp_send, ...)
class LogOnlyMailer(Mailer)     # logs subject + recipients at INFO; used when SMTP is unset
class RecordingMailer(Mailer)   # tests: .sent list, optional .fail_next
```

Settings (`CloudSettings`): `ICE_CLOUD_SMTP_HOST`, `ICE_CLOUD_SMTP_PORT` (default 587),
`ICE_CLOUD_SMTP_USER`, `ICE_CLOUD_SMTP_PASSWORD`, `ICE_CLOUD_SMTP_FROM`, `ICE_CLOUD_SMTP_STARTTLS`
(`1` default). Host unset ⇒ `smtp_configured == False`, `LogOnlyMailer`, one warning at boot, and
`alerts_handled` is false for every machine (section 4). Host set without `FROM` is a `SettingsError`.
`.env.example` and `docker/docker-compose.cloud.yml` carry the block.

*(As built: `ICE_CLOUD_SMTP_USER` without `ICE_CLOUD_SMTP_PASSWORD` is also a `SettingsError`, as is a non-numeric or out-of-range `ICE_CLOUD_SMTP_PORT`. `SmtpMailer` takes an `SmtpConfig` (host, port, username, password, `use_starttls`, from); `make_mailer(settings)` is the entry point and returns `LogOnlyMailer` when the host is unset.)*

## 4. Edge hand-off (owner decision B) and fallback

### 4.1 Contract 0.2.0 → 0.3.0 (additive)

`RegisterResponse` gains `alerts_handled: bool = False`. True when the machine is claimed **and**
`settings.smtp_configured`. `CLOUD_CONTRACT_VERSION = "0.3.0"`; `MachineReport.contract` accepts
`Literal["0.2.0", "0.3.0"]` on the cloud (ingest keeps accepting 0.2.0 reports from un-upgraded edges)
and the edge sends `"0.3.0"`. `docs/contracts/edge-cloud/CONTRACT.md` gains a 0.3.0 section.

### 4.2 Edge

`MachineIdentity` gains in-memory (never persisted) fields set by the uplink loop:
`alerts_handled: bool` and `last_report_ok_at: datetime | None`. `HttpUplink.register` returns the
parsed `RegisterResponse` flag to the loop; `post_report` success stamps `last_report_ok_at`. A 401,
a failed register, or re-registering clears `alerts_handled`.

```python
def cloud_handles_alerts(identity: MachineIdentity | None, interval_seconds: int, now: datetime) -> bool:
    return (identity is not None and identity.alerts_handled
            and identity.last_report_ok_at is not None
            and now - identity.last_report_ok_at <= timedelta(seconds=3 * interval_seconds))
```

`Notifier.__init__` takes an optional `cloud_handles_alerts: Callable[[], bool]` (default
`lambda: False`); `send()` checks it after the cooldown bookkeeping and, when true, logs
`"Notifier: deferred to cloud: <key>"` at INFO and returns without delivering. `__main__` wires it only
when the identity exists. The cooldown entry is still recorded, so flipping back to local does not
replay a burst.

Consequences, stated for the owner: a claimed machine whose cloud becomes unreachable emails locally
again after at most 3 × `report_interval_seconds` (3 minutes at the default). A fault raised during
that gap is emailed by the cloud when the first report gets through, because the cloud's stored state
predates it, so it appears as `raised`. A fault that both raised and cleared inside the gap is emailed
by nobody. This is the accepted cost of option B; the portal's fault list is unaffected.

`/health` shows "Alerts: cloud" or "Alerts: local" beside the fingerprint, from the same predicate.

## 5. Portal and admin

- `GET /machines/{short}/alerts` (`require_machine_access()`, owners and techs; `ice_cloud/alerts/pages.py`,
  template `alerts.html` in the cloud's own templates, not `ice_ui`): last **200** events newest first,
  each with time, kind, code, severity, description, and one line per recipient from its delivery row
  (`sent 14:03`, `pending`, `failed: <error>`, `muted`). Level `LEVEL_ALERTS` child of the machine
  level, crumbs through `/fleet`.
- `/machines/{short}/people`: a Mute/Unmute toggle (htmx `POST /machines/{short}/people/{account_id}/mute`
  with `muted=0|1`) beside each person including the owner. The owner may toggle anyone; a tech may
  toggle only themselves (same route, `403` otherwise — this is the one 403 on a machine page, since the
  caller already has access). The owner's own row appears on this page for this purpose.
- `/fleet`: "N alerts / 24 h" per machine from `repo.alert_count_since(fingerprint, now - 24h)`.
- `/admin`: SMTP status ("configured: host" or "log-only") and `POST /admin/test-email` sending a
  fixed test message to the admin's own sign-in address through the live `Mailer`, reporting success or
  the error inline. `require_htmx` on every POST, as elsewhere.

## 6. Repository additions

```python
@dataclass(frozen=True)
class AlertState: fingerprint: str; keys: frozenset[str]; last_report_at: datetime; report_interval_seconds: int; offline_since: datetime | None
@dataclass(frozen=True)
class StoredEvent(AlertEvent): id: int
@dataclass(frozen=True)
class Delivery: id: int; fingerprint: str; account_id: str; event_ids: tuple[int, ...]; status: str; attempted_at: datetime | None; sent_at: datetime | None; error: str | None; attempts: int

def alert_state(self, fingerprint) -> AlertState | None
def put_alert_state(self, state: AlertState) -> None
def claimed_machines_with_state(self) -> list[tuple[Machine, AlertState]]
def record_alert_events(self, events: list[AlertEvent]) -> list[StoredEvent]
def alert_events(self, fingerprint, *, limit: int) -> list[StoredEvent]
def alert_count_since(self, fingerprint, since: datetime) -> int
def cleared_since_last_sent(self, fingerprint, account_id) -> list[StoredEvent]
def people_for_alerts(self, fingerprint) -> list[tuple[Recipient, bool]]      # owner + techs, with muted
def set_muted(self, account_id, fingerprint, muted: bool) -> None
def create_delivery(self, fingerprint, account_id, event_ids, status, now) -> Delivery
def pending_deliveries(self, *, before: datetime) -> list[Delivery]           # pending whose cooldown expired, and failed with attempts < 5
def mark_delivery(self, id, *, status, now, error=None) -> None
def last_sent_at(self, fingerprint, account_id) -> datetime | None
def deliveries_for_events(self, event_ids) -> dict[int, list[Delivery]]
```

*(As built: the repository also has `alert_events_by_ids(ids)`, `record_alert_pass(events, state)` (events and state in one transaction), `is_muted(account_id, fingerprint)` and `delete_alert_state(fingerprint)`. `record_alert_events` takes `AlertEventRow` objects, `AlertState` holds `faults` rather than `keys`, and `alert_count_since` does not count `cleared` events.)*

Three migrations appended to `MIGRATIONS`: `machine_alert_state`, `alert_events` + `alert_deliveries`
(with `attempts INTEGER NOT NULL DEFAULT 0`), `alert_prefs`. Existing `claim_machine`, `revoke`,
`unbind_machine` gain the cascades named in 2.2 and 3.2, in the same transaction they already open.

## 7. Error handling

- Ingest never fails because of alerts: the engine/dispatcher call is wrapped, logged with
  `logger.exception`, and the report still returns 204. The state row is written before dispatch, so a
  dispatcher crash cannot cause a double "raised" on the next report.
- SMTP errors mark the delivery `failed` with the exception text (truncated to 500 chars) and are retried
  as in 3.3. The dispatcher sends sequentially; one bad address does not block the others, and each
  recipient's row records its own outcome.
- A `MachineReport` with contract `0.2.0` is processed identically; nothing in alerts depends on 0.3.0.
- The sweep catches and logs its own exceptions per machine and keeps the loop alive (it runs under the
  app, not `supervise`, so it guards itself).
- Clock: every `at` is the cloud's `now()` (injectable in `create_app`), never the report's `sent_at`,
  so a machine with a wrong clock cannot post-date or back-date alerts; `sent_at` is shown only as
  "last report" text.

## 8. Testing

- `tests/cloud/alerts/test_engine.py`: raise, clear, unchanged, mixed, restart re-raise, ordering.
- `test_router.py`: the role table above, muted, suspended excluded, `cleared` never triggers alone.
- `test_dispatcher.py` (with `RecordingMailer`, injected `now`): one email per recipient per pass,
  bundling of cleared-since-last, cooldown defers to `pending` and flushes, failure retry and the 5-cap,
  email body golden for raised/offline/online.
- `test_sweep.py`: offline at `3 × interval + 1 s`, not at `3 × interval`, once only, `online` on next
  report, unclaimed never, no state row never.
- `test_ingest_alerts.py`: full path through `POST .../report` → rows → mail; alerts exception leaves 204.
- `test_alert_pages.py`: `/machines/{short}/alerts` listing and delivery lines; mute toggle
  authorisation (owner any, tech self only, non-member 404); `/fleet` count; `/admin` SMTP status and
  test email (`RecordingMailer`); every POST needs `HX-Request`.
- `tests/contracts/cloud/test_identity.py`: `RegisterResponse` parses with and without `alerts_handled`;
  version pins 0.3.0; CONTRACT.md has a 0.3.0 section.
- `tests/common/test_mailer.py`: `smtp_send` argument plumbing with a fake SMTP class; edge wrapper
  still passes its config through.
- `tests/edge/uplink/`: loop stores the flag and `last_report_ok_at`, clears on 401; `cloud_handles_alerts`
  truth table around the 3 × boundary.
- `tests/edge/notify/test_notifier.py`: deferred-to-cloud logs and does not deliver, cooldown still
  recorded, falls back when the predicate turns false.
- `tests/cloud/test_ui_parity.py` untouched (no `ice_ui` template changes). Repository tests extend
  `tests/cloud/test_sqlite_repository.py` for the cascades and the new methods.

## 9. Deployment notes

`packages/cloud/README.md` gains an "Alerts" section: the SMTP block, that alerts are log-only until
`ICE_CLOUD_SMTP_HOST` is set and that the edge keeps emailing locally until then, the 10-minute
cooldown, how to mute, and that `FORWARDED_ALLOW_IPS` is unrelated here. `CLAUDE.md`'s Cloud section
gets one bullet. Open items carried forward: no SMTP rate limiting beyond the cooldown; the sweep is
a single in-process task (one cloud instance assumed, as the SQLite choice already assumes).
