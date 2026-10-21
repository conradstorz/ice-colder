# TODO — operational review findings (2026-10-21)

Source: full operations review of `main` at `c0796ba`, 2026-10-21. Each item carries
the evidence that was verified, the failure scenario, and a suggested fix. Severity is
the reviewer's ranking; reorder as you see fit. Already handled and therefore not listed:
the Watchtower crash loop on hpz440 and the process-level monitoring plan.

Line numbers are as of `c0796ba`.

Suggested order: Backups (D1) → Money M1, M2 → Runtime R1 → Money M3 as the next spec →
Cloud C1, C2 → the rest.

---

## M. Money and sale correctness (`packages/edge/src/ice_edge/sale/`, `vend/`)

- [x] **M1 — HIGH: leftover credit after a vend is never refunded.**
  `sale/vmc.py:566` `on_complete_transaction` routes to `interacting_with_user` when
  `credit_escrow > 0` but nothing re-arms the session timer; the only arm sites are
  `on_start_interaction` (:537), `deposit_funds` (:722) and `_fail_vend` (:661).
  Scenario: $5 paid for a $3 item, customer walks away, $2 held indefinitely.
  Fix: arm `_session_timer` in `on_complete_transaction` when credit remains. Add a test
  (`tests/edge/sale/`) that fires `session_timeout` after a vend with change and asserts
  a refund.

- [x] **M2 — HIGH: a duplicate terminal dispenser report during the awaited sale write
  re-enters the success path.**
  `sale/vmc.py:345` `_dispense_succeeded` cancels only the timeout before
  `await self._cycle.record()`; state is still `dispensing`, `_cycle` is still set, and
  `vend/dispense_cycle.py:294` `classify` has no settled flag.
  Scenario: QoS-1 redelivery of `complete` inside that window writes a second `sales`
  row; a late `jam`/`error` there calls `_fail_vend` and restores credit after the
  product is out and recorded.
  Fix: mark the cycle settled synchronously before the await (or set `self._cycle = None`
  first and hold a local reference); `classify` returns `None` once settled. Test: a
  same-slot duplicate `complete` during `record()` (`test_vmc_flows.py:219` only covers a
  different-slot duplicate).

- [ ] **M3 — HIGH: the edge does not consume the new payment contract
  (`ice_contracts.board.payment` 1.1.0).**
  `hal/mqtt_inbound.py:185` still routes legacy `payment/credit` to `on_payment_credit`,
  which validates the deprecated `PaymentEvent` (amount/method/timestamp only). Nothing
  in `ice_edge` or `ice_sims` imports `ice_contracts.board.payment`; no `payment/event`
  consumer, no `event_id`/`boot_id`/`seq` de-duplication, no `payment/event/ack`, no
  `cash_sale`, `PAY-105` never raised.
  Scenario: `mqtt_client.py:218` subscribes at QoS 1; a redelivery or session replay
  calls `deposit_funds` twice for one coin; escrow can drift from what the MDB board
  holds with no gap detection.
  Fix: this is a spec-sized item (it gates the MDB board). Write the edge-migration spec:
  payment-event consumer that acks by `(boot_id, seq)`, dedups on `event_id`, raises
  `PAY-105` on a gap. Until it lands, dedup legacy credits on a message id.

- [ ] **M4 — MEDIUM: availability is checked only at selection, not at payment.**
  `_refusal` runs in `select_product` (`sale/vmc.py:856`); `process_payment` (:903-928)
  checks only credit amount and catalog membership.
  Scenario: customer selects ice, then `WTR-104`/`ENV-102` or an `ICE-101` lockout
  arrives while waiting for money; the next deposit still dispenses.
  Fix: re-run `_refusal`/`sale_available` in `process_payment` before `consume_fifo`;
  on refusal keep waiting or refund.

- [ ] **M5 — MEDIUM-LOW: PAY-104 "Record as sale" can duplicate an already-recorded sale.**
  The live `record_sale` uses a fresh `time.time()` and is not idempotent; the recovery
  route (`web/routes/health/recovery.py:196`) uses `ts=saved_at, idempotent=True`. The
  keys can never match.
  Scenario: crash after the live insert commits but before the idle snapshot clear →
  boot sees `uncertain` → operator records a second row. Window is milliseconds.
  Fix: write the live sale with the dispensing snapshot's `saved_at` as `ts` and
  `idempotent=True`, or have recovery check for a row with the same sku at/after
  `saved_at`.

- [ ] **M6 — LOW: the `error` state would lose a sale if anything entered it.**
  `on_error` (`sale/vmc.py:667`) keeps the sale with shares already deducted and cancels
  the cycle without restoring credit or recording; `on_reset` then `_drop_sale`s.
  `error_occurred` has no production caller today.
  Fix: delete the trigger, or make it restore shares / record the sale.

- [ ] **M7 — LOW: stale `process_payment` retry chains.**
  Each button press schedules its own 1 s then 5 s chain (`sale/vmc.py:896`, :941). A
  stale chain firing with `selected_product is None` after a vend that left credit calls
  `cancel_sale`, refunding with a misleading "no longer available" message.
  Fix: one tracked handle, cancelled before scheduling a new one.

- [ ] **M8 — LOW (accepted trade-offs, document them):** a dispense timeout with no
  report restores credit though product may have come out (PAY-102 raised, later report
  ignored); reports with no `request_id` are accepted by slot alone, so old firmware can
  settle a new sale on the same slot; `classify` reads `sale.product.slot` from the live
  catalog object, so editing a slot mid-dispense turns the terminal report into a slot
  mismatch and a timeout.
  If the dispense timeout wins first, a genuine `complete` arriving afterwards is
  ignored (credit restored though product may be out) -- pre-existing, documented, not
  changed here.

## R. Edge runtime resilience

- [ ] **R1 — HIGH: one failed email loses the alert permanently.**
  `monitoring/health_monitor.py:530` adds the dedup key to `_fired_alerts` before calling
  the callback; `notify/notifier.py:68` sets the cooldown before delivery; :146 logs and
  swallows the SMTP failure; `_deliver` also returns silently when the gateway is
  unconfigured. Same when the cloud defers an alert (:77-80) and later stops vouching.
  Scenario: SMTP down when a leak is raised → never re-sent until the fault clears and
  re-raises.
  Fix: `send` returns success; on failure `clear_alert(key)` or retry with backoff; set
  the cooldown only on success.

- [ ] **R2 — HIGH: a bad `data/` volume stops the whole machine at boot, with exit 0.**
  `ledger/event_recorder.py:268` re-raises any non-corruption `OperationalError`
  (read-only, locked, cannot open); `__main__.py:131` constructs it unguarded;
  `@logger.catch()` on `main` (`__main__.py:45`) logs it and returns, so the exit code
  is 0 and the restart loop looks like clean exits.
  Fix: catch init failure, set `db_unavailable`, raise a fault (as the corrupt-DB path
  does); `@logger.catch(onerror=lambda _: sys.exit(1))`.

- [ ] **R3 — MEDIUM-HIGH: a failed session-snapshot save on deposit is only logged.**
  `sale/outputs.py:167-186` saves via `fire_and_forget(persistent=True)`;
  `supervisor/task_runner.py:119-125` only logs a failure. The dispense path is correct
  (`PAY-102 snapshot_failed`); the deposit and refund-pending paths are not.
  Scenario: disk full when cash is deposited, then power loss → no PAY-104 evidence, money
  gone silently.
  Fix: raise a machine-scope fault (e.g. `DATA-103`) on save failure, clear on next
  success.

- [ ] **R4 — MEDIUM: no persistent MQTT session; a broker drop mid-vend can pay out twice.**
  `ice_common/mqtt_client.py:190-199` sets no persistent session, so messages published
  while the VMC is disconnected are lost, including the terminal dispenser report; after
  120 s `dispense_timeout` fires PAY-102 and restores credit although the ice was
  delivered. Related: `publish` (:113-115) returns normally when disconnected, so callers
  cannot tell a message was dropped.
  Fix: persistent session (`clean_session=False`, QoS 1 on the dispenser topic) or
  reconcile from retained dispenser state after reconnect; make `publish` report the
  drop.

- [ ] **R5 — MEDIUM-LOW: uvicorn bind failure skips cleanup.**
  uvicorn `sys.exit(1)`s inside a task, bypassing `main`'s `finally`
  (`__main__.py:212-220`); the recorder is not flushed. MQTT, recorder, health and uplink
  are all wired before the bind attempt.
  Fix: bind-check early, or move cleanup into an outer try/finally.

- [ ] **R6 — MEDIUM-LOW: event-row and inventory write failures are invisible.**
  `_writer_loop` logs a failed insert (`event_recorder.py:876-886`);
  `InventoryManager._save` logs an error (`monitoring/inventory_manager.py:73-74`).
  Neither raises a fault; KPIs go silently wrong or counts revert after restart.
  Fix: alert-class faults.

- [ ] **R7 — LOW: future `saved_at` treated as corrupt.** `ledger/session_store.py:129-130`.
  A Pi with no RTC booting with a clock behind the last save loses the recovery details
  and gets a generic PAY-104. Fix: relax the check or store a boot id alongside wall time.

- [ ] **R8 — LOW: unbounded in-memory keys and queues.** `_temperatures`, `_channels`,
  `_signals`, `_subsystems`, `capabilities` keyed by payload strings
  (`hal/mqtt_inbound.py:118,132,145`); recorder queue is an unbounded `queue.Queue()`
  (`event_recorder.py:312`); aiomqtt incoming queue unbounded
  (`max_queued_incoming_messages` unset, `mqtt_client.py:190`); handlers run serially, so a
  slow `await record()` delays every other message.
  Fix: validate names against expected subsystems, cap dicts and queues.

- [ ] **R9 — LOW: minor loop blocking.** `tests_level/log.py:39` calls
  `recorder.flush()` on the loop (busy-wait up to 5 s); `InventoryManager.decrement`
  writes synchronously by default; `build_bundle()`/`ui_digest()` run synchronously at
  uplink start and on every supervisor restart (`uplink/loop.py:69`).

- [ ] **R10 — LOW: logging.** File sink has no level (`ice_common/logging_setup.py:15-22`),
  so it records DEBUG including every sensor/heartbeat payload and the config model dump
  (`__main__.py:57`); no size cap on the 300-day retention; `mqtt_client.py:264` logs
  handler errors without traceback. Fix: file sink at INFO, `logger.exception` for
  handler errors.

- [ ] **R11 — LOW: heartbeats are stored as event rows.** On the sim host 797k of 863k
  rows in `events.db` (56 MB after 4 weeks) are `heartbeat`. The 90-day prune bounds it
  (~170 MB) but it is wasted SD-card wear on a Pi. Fix: keep liveness in memory and
  record only transitions (alive→dead, dead→alive).

- [ ] **R12 — LOW: a restart mid-sale always produces a PAY-104.** 35 `session_uncertain`
  events on the sim host, one per deploy that landed mid-sale. On a real machine every
  auto-update that lands mid-sale needs an operator. Fix: see D3 (idle-window updates)
  and/or let boot evaluation auto-resolve a snapshot whose sale row is already present.

## C. Cloud portal (`packages/cloud/`) — public internet, 1 vCPU / 512 MB

- [ ] **C1 — HIGH: unauthenticated registration has no rate limit or machine cap.**
  `ingest.py:33` `register` accepts any Ed25519 key; `require_machine`
  (`machine_auth.py:180-187`) only checks fingerprint matches key; `post_report` then
  accepts up to 256 KiB per report from any registered machine, claimed or not, written
  file-per-machine by `SnapshotStore.save` (`store/snapshots.py:57-79`). `/fleet` for
  admins does a `store.latest` read and an `alert_count_since` query per machine
  (`portal.py:156-181`).
  Fix: IP rate limit on register (Caddy or in-app `Backoff`), global machine cap, TTL
  purge of never-claimed machines, smaller body cap for unclaimed reports.

- [ ] **C2 — HIGH: bundle tar caps bytes but not member count.**
  `packages/ui/src/ice_ui/bundle.py:183` sums `member.size` against 20 MiB; zero-byte
  members add nothing, so a ~20 MB gzip can expand to tens of millions of 512-byte
  headers, each added to `seen` (RAM) and written as an inode. `UiRegistry.register`
  runs this under `self._lock` (`ui_registry.py:88`), the same lock `_templates_for` and
  `resolve` take, so every portal page stalls. Only claimed machines can upload, but any
  Google account can claim its own fake machine.
  Fix: cap member count (~2000) and cumulative header/name bytes; extract outside the
  shared lock.

- [ ] **C3 — MEDIUM: unsigned 20 MiB PUTs are buffered before the signature is checked.**
  `body_limit.py:224-227` grants 20 MiB to any `PUT /api/v1/ui-bundles/*`;
  `require_machine` calls `await request.body()` (`machine_auth.py:177`) before verifying.
  A few concurrent unsigned PUTs exhaust the droplet.
  Fix: check headers, timestamp, known+claimed machine before reading the body; keep an
  unauthenticated cap at 256 KiB.

- [ ] **C4 — MEDIUM: unbounded alert tables and a full-table scan per page.**
  `store/sqlite.py:1004` `deliveries_for_events` does `SELECT * FROM alert_deliveries`
  with no WHERE on every `/machines/{short}/alerts` view and JSON-parses every row.
  Nothing prunes `alert_events`, `alert_deliveries` (incl. failed rows with attempts ≥ 5),
  `bundle_uploads` or invitations. `MachineReport.faults` has no length cap beyond the
  body cap and no max string lengths, so a claimed fake machine flapping its faults list
  creates thousands of event rows per report.
  Fix: filter by fingerprint/event ids; retention job (90 days); cap `faults` length and
  string lengths in `ice_contracts.cloud.report`.

- [ ] **C5 — MEDIUM: a replayed or stale report still triggers alert processing.**
  `ingest.py:67` ignores `store.save()` returning `False` for a stale `seq` and always
  calls `_handle_alerts`; it also refreshes `last_report_at`, delaying the offline alert.
  No nonce cache; `canonical_message` (`ice_contracts/cloud/signing.py:68`) covers
  method, path, timestamp, body hash, so a replay needs a captured request within 300 s
  (TLS mitigates). A replayed `DELETE claim-code` could wipe a newer code.
  Fix: skip alert processing when `save` returns `False`; small cache of seen signatures
  within the skew window.

- [ ] **C6 — MEDIUM: any signed-in account can lock a machine out of being claimed.**
  `claim.py:357` `backoff.check("claim", subject, client, trusted=False)`; the
  per-subject budget (`ice_common/backoff.py:126-130`) throttles all clients after 20
  failures/hour, cap 3600 s; unparseable fingerprints share subject `"unknown"`.
  `_prune` is O(n) per check/record (:99-107), so spraying distinct subjects is a CPU
  sink.
  Fix: apply the per-subject budget only to attempts that failed the code check on a
  resolved machine; bound the dictionary.

- [ ] **C7 — MEDIUM-LOW: scrypt shares the default thread pool with SQLite.**
  Each failed claim `to_thread(verify_secret)` (`claim.py:371`, :395), N=2^14 r=8
  (~16 MB, tens of ms). On 1 vCPU the default executor (~5 threads) is shared with every
  DB call and report ingest.
  Fix: dedicated 1–2 thread executor or a semaphore for scrypt.

- [ ] **C8 — LOW-MEDIUM: no revocation for sessions or machine keys.**
  Sessions are stateless HMAC cookies (`auth.py:107-130`); logout only deletes the
  cookie; a stolen cookie lives 30 d idle / 90 d absolute unless the account is
  suspended. `unbind_machine` keeps the public key valid; no "suspend machine"; no key
  rotation.
  Fix: per-account session epoch; `machines.blocked_at`.

- [ ] **C9 — LOW: fleet-scope invitations evaluated at accept time**
  (`store/sqlite.py:674-675`) grant machines claimed after the invite was issued.
  Fix: snapshot the machine list at invite time, or label it in the UI.

- [ ] **C10 — LOW: OIDC flow cookie is not single-use** (replayable for 10 min; PKCE and
  nonce limit impact).

- [ ] **C11 — LOW: `resolve_short` uses a 64-bit prefix** (`store/sqlite.py:379-387`);
  ambiguous prefixes return `None`. ~2^64 work to exploit; note only.

## D. Deployment, data and secrets

- [x] **D1 — HIGH: nothing is backed up.**
  Edge: `./data:/app/data` (`docker-compose.yml:26-27`) holds `events.db` (sales and
  `cash_collections` are never pruned — the financial record), `access.json` (users,
  PINs), `cloud_identity.json` (losing it = a new unclaimed machine), `config.json`,
  `dispensers.toml`, `session.json`. No doc, script or service mentions backup.
  Cloud: `cloud.db` is the only record of owners, techs and claims
  (`docs/deploying-the-cloud-server.md:186` says so); no cron on the droplet, DO backups
  not enabled.
  Fix: nightly `sqlite3 events.db ".backup ..."` plus a tarball of `data/` (excluding
  `session.json`) shipped off-host; the same for `cloud.db`; a written restore section;
  consider enabling DO droplet backups.

- [ ] **D2 — HIGH: `events.db` has no migration path.**
  Tables are `CREATE TABLE IF NOT EXISTS` only (`event_recorder.py:421-455`), no
  `user_version`; the next added column cannot reach a deployed database and the first
  insert after a pulled image would fail. `cloud.db` already has the pattern
  (`schema_version` + transactional `MIGRATIONS`, `store/sqlite.py:277-292`).
  Fix: copy the cloud's migration pattern into `EventRecorder`.

- [ ] **D3 — HIGH: `:latest` auto-pull with no healthcheck, no rollback, no idle window.**
  `vmc` and sims pull `ghcr.io/conradstorz/ice-colder:latest` with the Watchtower label
  (`docker-compose.yml:14-17`, :44-47); no `healthcheck` on `vmc` (only mosquitto, :179),
  no `HEALTHCHECK` in `Dockerfile:49-51`, so a wedged edge looks healthy. Watchtower
  restarts without waiting for idle (see R12). CI publishes `sha-<commit>` tags
  (`ci.yml:156-165`) but rollback is undocumented.
  Fix: `HEALTHCHECK` on `/pill`; pin production machines to `sha-` tags and keep
  Watchtower for the sim host only, or monitor-only mode plus an idle-window update
  script; document rollback.

- [ ] **D4 — MEDIUM: `config.json` and `.bak` are written with umask permissions.**
  `config/config_store.py:70-77` uses plain `open(tmp, "w")` and `shutil.copy2`; both
  carry real Stripe/PayPal/SMTP/MQTT secrets (`_config_json`, :51). On the host (root,
  umask 022) they are 0644, readable by any local user or any container bind-mounting
  `./data` read-only (the simulators do). The `.bak` keeps the previous secrets after a
  rotation. Contrast `access/access.py:321` (`os.open(..., 0o600)`).
  Fix: `os.open(..., 0o600)` for the tmp file, chmod the `.bak`.

- [ ] **D5 — MEDIUM: edge compose hygiene.**
  No `logging: max-size`/`max-file` (container logs unbounded); no `stop_grace_period`
  (10 s default vs uvicorn drain + up to 3 s persistence drain + up to 5 s recorder
  flush); no `mem_limit`/`deploy.resources` on any app service. `uv run` is the CMD
  (`Dockerfile:51`); `uv run` execs on Unix so SIGTERM reaches Python, but exec'ing
  python directly removes the doubt.
  Fix: `stop_grace_period: 30s`, log caps, exec python directly.

- [ ] **D6 — MEDIUM: Dockerfiles use unpinned `uv:latest`** (`Dockerfile:6`,
  `packages/cloud/Dockerfile:12`); `python:3.12-slim`, `eclipse-mosquitto:2`,
  `caddy:2-alpine` float. `uv.lock` pins Python deps. Fix: pin uv to a version.

- [ ] **D7 — MEDIUM: session/device cookies are `Secure` only when the request is HTTPS.**
  `web/auth.py:94` via `backoff.is_https` (`ice_common/backoff.py:88-95`) honours
  `X-Forwarded-Proto` only from a trusted proxy; default deployment is plain HTTP on
  `0.0.0.0:26123` (`__main__.py:160`), so session, device (365 d, `DEVICE_COOKIE_MAX_AGE`)
  and enroll cookies cross the LAN in clear. Sniffed device cookie + shoulder-surfed PIN =
  no OTP needed. Mosquitto is likewise cleartext on 1883 with username/password.
  Fix: document that TLS at the proxy is required, or bind the dashboard to loopback by
  default behind the proxy; consider MQTT TLS on the LAN.

- [ ] **D8 — MEDIUM: no edge-only production compose.** The three sim containers live in
  the root compose; `docker/docker-compose.prod.yml` is broker-only. A real Pi needs a
  compose with vmc + broker and no sims; `docker-compose.yml:2-10` does not say so.

- [ ] **D9 — LOW: CI caveats.** arm64 edge image is built under QEMU and never
  smoke-tested (only amd64, `ci.yml:175`); `test` job has no timeout; actions pinned to
  major versions, not SHAs; the MDB hardware conformance skip is a standing gap until an
  MDB simulator exists.

- [ ] **D10 — LOW: `/health/logs` tail reads byte-by-byte with a seek per byte**
  (`web/context.py:209-228`); a log with long lines makes it do millions of seeks
  (authenticated CPU DoS only). Fix: read fixed-size blocks backwards.

- [ ] **D11 — LOW: `/static` is mounted without auth** (`web/server.py:10-13`); serves
  only app assets today. Note so a future file placed there is not accidentally public.

- [ ] **D12 — LOW: documentation debt.** `ROADMAP.md:97,208,266` cite
  `services/availability.py`, `controller/vmc.py`, `services/session_store.py` (removed
  roots); `PLAN.md:82,96,136` cite `main.py`; `docker-compose.override.yml` comment cites
  `services/auth_policy.py`. Traefik (edge) vs Caddy (cloud) is intentional but
  `README.md:101-107` vs the deploy doc could say so in one place.

## T. Tests

- [ ] **T1 — coverage gaps.** Named in 0 test files: `ice_cloud/body_limit.py`,
  `ingest.py`, `machine_auth.py`, `oidc.py`, `oidc_google.py`; `alerts/engine.py` in 1.
  Thin: `notify/notifier.py` (3), `reporting/report_scheduler.py` (2),
  `app/startup_dispensers.py`, `startup_recovery.py` (2 each). The cloud routes may
  exercise some indirectly; the Google OIDC callback and body-limit middleware deserve
  direct tests. Add tests alongside the M1, M2, R1 fixes.
- [ ] **T2 — no multi-service integration test.** CI runs a real mosquitto and a
  single-container smoke test of the simulated machine image, but no compose-stack test
  and no edge→cloud end-to-end against a running cloud.
- [ ] **T3 — ratchet tests.** `test_test_isolation.py`, `test_import_contracts.py`,
  `test_skip_policy.py`, `test_routes_shape.py` are load-bearing. `test_vmc_complexity.py`
  (line count over one file) and the private-access budget (exempt with one comment) are
  drift guards only; decide whether to keep them.

## Verified as correct (no action; listed so nobody re-audits them)

Awaited WAL sale write + idempotent journal replay on `(ts, sku)`; corrupt-DB
quarantine; same-`request_id` command and refund retries; `is_test` carried through
snapshot and recovery; FIFO escrow restores exact shares; `SVC-102` exemption keyed on the
sale; CSRF (`HX-Request`) on all 49 edge POSTs and every cloud mutating route, no CORS;
cookies HttpOnly/SameSite=Lax; scrypt PINs and emergency codes with dummy-hash timing
defence, `access.json` 0600; XFF ignored unless a proxy is trusted; Jinja autoescape on,
no `|safe` in ui or cloud; no path traversal; API docs disabled; cloud tenancy via
`require_machine_access` (404 for non-members, no IDOR found); Ed25519 signing over
method+path+timestamp+body hash, fingerprint bound on register; OIDC checks iss/aud/exp/
nonce/PKCE/email_verified; bundle sandboxing (SandboxedEnvironment, CSP sandbox, link and
`..` members rejected); cloud container uid 1000 / read-only / no caps; Caddy HSTS +
nosniff + frame-deny; `BodyLimitMiddleware` counts bytes as they arrive; offline sweep
persisted and compare-and-swapped; atomic `save_config` with fsync; `.env` secrets never
in the image; real firmware build + native tests + golden drift in CI; `latest` cannot go
backward.
