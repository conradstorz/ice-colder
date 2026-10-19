# Cloud Identity and Tenancy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn `ice_cloud` into a multi-owner fleet server: machines sign every request with a self-generated Ed25519 key, owners claim machines with a code minted at the machine, owners invite techs, people sign in with Google, a config allowlist names platform admins.

**Architecture:** Signing helpers and wire models live in `ice_contracts.cloud` (the one package edge and cloud share). The edge gains `ice_edge.uplink.identity` (keypair + persisted claim-code notice) and an `AccessStore` observer that tells the uplink when the live claim code changes. The cloud gains a `Repository` protocol over SQLite, a `require_machine` signature dependency, Google OIDC sign-in with a signed session cookie, and portal routes gated on owner/tech/admin. Machine pages stay at the machine's own paths with an access-checked selection cookie, so `tests/cloud/test_ui_parity.py` keeps passing.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, `cryptography` (Ed25519, RS256 for Google ID tokens), `httpx`, SQLite (stdlib `sqlite3`, WAL), Jinja2/HTMX via `ice_ui`, pytest.

Spec: `docs/superpowers/specs/2026-10-19-cloud-identity-tenancy-design.md`. Section numbers below (§) refer to it.

## Global Constraints

- Package layers (import-linter): `ice_contracts` → `ice_common` → `ice_ui` → (`ice_edge` | `ice_cloud` | `ice_sims`). `ice_cloud` never imports `ice_edge`. Test isolation: cloud tests may import `ice_edge` only in `tests/cloud/test_ui_parity.py` (already does).
- Plan style (user rule): tasks specify interfaces and tests, not full code bodies. Implementers write the code.
- `uv run pytest`, `uv run ruff check --fix .` then `uv run ruff format .` before every commit. Always `uv run ruff`, never the global ruff.
- Do not chain shell commands with `&&`; separate calls.
- Commit trailer, exactly: `Assisted-by: <model> (Claude Code)`; no `Co-Authored-By`.
- Private-access ratchet (`tests/meta/test_no_private_access.py`): new tests reach no `_private` attribute; use public seams. Budgets only move down.
- `tests/ui/test_static_css.py` and `tests/cloud/test_cloud_static_css.py`: every CSS class a template uses must exist in the committed `packages/ui/src/ice_ui/static/app.css`. New templates use only classes already present (copy from `users_ownership.html`, `fleet.html`, `health.html`), or rebuild and commit `app.css` with the Tailwind CLI (its `content` glob is `packages/ui/src/ice_ui/templates/**/*.html`; cloud templates under `packages/cloud/src/ice_cloud/templates/` are **not** scanned, so they may use only existing classes).
- Codes: 8 digits (`CODE_DIGITS`), scrypt via `hash_secret`/`verify_secret`, 7-day TTL (`TRANSFER_TTL_DAYS`), plaintext shown once with `Cache-Control: no-store`.
- Fingerprint = lower-case hex SHA-256 of the raw 32-byte Ed25519 public key; short form = first 16 hex grouped `xxxx-xxxx-xxxx-xxxx`.
- Headers: `X-Ice-Machine`, `X-Ice-Timestamp` (ISO-8601 UTC, seconds), `X-Ice-Signature` (base64). Clock skew limit 300 s. Auth failures: 401, empty body.
- Cloud contract version `0.1.0 → 0.2.0`.
- No `TBD`/`TODO` in delivered code or docs.

## File map

| Path | Responsibility |
|---|---|
| `packages/contracts/src/ice_contracts/cloud/signing.py` (new) | canonical message, sign/verify, fingerprint helpers, header names, skew check |
| `packages/contracts/src/ice_contracts/cloud/identity.py` (new) | `RegisterMachine`, `RegisterResponse`, `ClaimCodeNotice` |
| `packages/contracts/src/ice_contracts/cloud/report.py` | `CLOUD_CONTRACT_VERSION = "0.2.0"` |
| `packages/contracts/pyproject.toml` | add `cryptography` |
| `packages/common/src/ice_common/backoff.py` (new) | `Backoff` moved verbatim from `ice_edge.access.access` |
| `packages/edge/src/ice_edge/access/access.py` | re-export `Backoff`; `pending_link`, `start_link`, `cancel_link`, `cloud_claim_code`, `subscribe_claim_code_change` |
| `packages/edge/src/ice_edge/uplink/identity.py` (new) | `MachineIdentity` file: keypair + pending notice |
| `packages/edge/src/ice_edge/uplink/client.py` | signed requests; `register`, `post_claim_code`, `delete_claim_code` |
| `packages/edge/src/ice_edge/uplink/loop.py` | order: register → notice → bundle → reports |
| `packages/edge/src/ice_edge/__main__.py` | identity load, observer wiring |
| `packages/edge/src/ice_edge/web/routes/users/ownership.py` | `/users/ownership/link`, `/users/ownership/link/cancel` |
| `packages/edge/src/ice_edge/web/routes/health/overview.py` | short fingerprint in context when `cloud.enabled` |
| `packages/ui/src/ice_ui/templates/users_ownership.html`, `health.html` | Link-to-cloud panel; fingerprint line |
| `packages/cloud/src/ice_cloud/settings.py` (new) | `CloudSettings` from env |
| `packages/cloud/src/ice_cloud/store/` (new package: `__init__.py`, `snapshots.py`, `repository.py`, `sqlite.py`) | report files by fingerprint; `Repository` protocol; the only SQL |
| `packages/cloud/src/ice_cloud/machine_auth.py` (new) | `require_machine` dependency |
| `packages/cloud/src/ice_cloud/ingest.py` | register, report-by-fingerprint, claim-code notice routes |
| `packages/cloud/src/ice_cloud/auth.py` | `OidcProvider`, `GoogleProvider`, sessions, dependencies, sign-in routes |
| `packages/cloud/src/ice_cloud/portal.py` | fleet, select, deep link, access-gated machine pages |
| `packages/cloud/src/ice_cloud/claim.py` (new) | `/claim` |
| `packages/cloud/src/ice_cloud/people.py` (new) | memberships, invitations, accept |
| `packages/cloud/src/ice_cloud/admin.py` (new) | `/admin` |
| `packages/cloud/src/ice_cloud/ui_registry.py` | restricted bundle context |
| `packages/cloud/src/ice_cloud/templates/*.html` | `login.html`, `fleet.html` (reworked), `claim.html`, `people.html`, `invite.html`, `admin.html` |
| `packages/cloud/src/ice_cloud/app.py`, `__main__.py` | `create_app(settings, *, now, provider)` |
| `docker/docker-compose.cloud.yml`, `.env.example`, `packages/cloud/README.md` (new), `CLAUDE.md`, cloud spec §8 note | deployment and docs |

## Dependency graph (for parallel dispatch)

- T1 → T2 → {T3, T4, T6-cloud-side} ; T4 → T5 ; {T1, T3} → T6 ; {T4, T6} → T7
- T2 → T8 → T9 → T10 (needs T1) → T11 → T12 → {T13 (needs T8 backoff move = T4a), T14, T15}
- T16 independent after T8. T17 last.
- Safe parallel groups: {T1, T4a} · {T3, T4, T8} · {T5, T6, T9} · {T7, T10} · {T11} · {T12, T16} · {T13, T14, T15} · {T17}.

---

### Task 1: Signing helpers in `ice_contracts.cloud.signing`

**Files:**
- Create: `packages/contracts/src/ice_contracts/cloud/signing.py`
- Modify: `packages/contracts/pyproject.toml` (add `"cryptography>=43"`), then `uv sync`
- Test: `tests/contracts/test_cloud_signing.py`

**Interfaces:**
- Produces:
  - `HEADER_MACHINE = "X-Ice-Machine"`, `HEADER_TIMESTAMP = "X-Ice-Timestamp"`, `HEADER_SIGNATURE = "X-Ice-Signature"`, `MAX_CLOCK_SKEW_SECONDS = 300`
  - `generate_private_key() -> bytes` (32 raw bytes), `public_key_from_private(private: bytes) -> bytes` (32 raw bytes)
  - `fingerprint(public_key: bytes) -> str` (64 hex), `short_fingerprint(fp: str) -> str` (`xxxx-xxxx-xxxx-xxxx`), `parse_short_fingerprint(text: str) -> str | None` (16 hex lower-case without dashes; accepts dashes/spaces/upper-case; `None` otherwise)
  - `canonical_message(method: str, path: str, timestamp: str, body: bytes) -> bytes` = `f"{method.upper()}\n{path}\n{timestamp}\n{sha256(body).hexdigest()}".encode()`
  - `sign(private_key: bytes, message: bytes) -> str` (base64 std), `verify(public_key: bytes, message: bytes, signature_b64: str) -> bool` (never raises; bad base64/length → `False`)
  - `format_timestamp(now: datetime) -> str` (`YYYY-MM-DDTHH:MM:SSZ`), `is_timestamp_fresh(timestamp: str, now: datetime) -> bool` (unparsable → `False`; `abs(delta) <= MAX_CLOCK_SKEW_SECONDS`)

- [ ] **Step 1: Write failing tests** (`tests/contracts/test_cloud_signing.py`): `test_sign_verify_round_trip`; `test_tampered_body_fails`, `test_tampered_path_fails`, `test_tampered_method_fails`, `test_tampered_timestamp_fails` (each flips one input of `canonical_message`); `test_verify_with_wrong_key_is_false`; `test_verify_garbage_signature_is_false_not_raise`; `test_fingerprint_is_sha256_of_public_key`; `test_short_fingerprint_format_and_parse` (round trip, dashes optional, upper-case accepted, 15 hex → `None`); `test_timestamp_fresh_window_edges` (±300 s true, ±301 s false, garbage false); `test_format_timestamp_is_utc_seconds`.
- [ ] **Step 2:** `uv run pytest tests/contracts/test_cloud_signing.py -v` → fails on import.
- [ ] **Step 3:** Add the dependency, `uv sync`, implement the module with `cryptography.hazmat.primitives.asymmetric.ed25519`.
- [ ] **Step 4:** Tests pass; `uv run lint-imports` passes.
- [ ] **Step 5:** Commit `feat(contracts): Ed25519 request signing helpers for the cloud`.

---

### Task 2: Identity models and contract bump

**Files:**
- Create: `packages/contracts/src/ice_contracts/cloud/identity.py`
- Modify: `packages/contracts/src/ice_contracts/cloud/report.py` (`CLOUD_CONTRACT_VERSION = "0.2.0"`), `packages/contracts/src/ice_contracts/generate.py` (add the three models to `EDGE_CLOUD_MODELS`), regenerate `docs/contracts/edge-cloud/schemas/` with `uv run python -m ice_contracts.generate`
- Test: `tests/contracts/test_cloud_contract.py` (version assertions), `tests/contracts/test_contract_schemas.py` (drift check must pass after regeneration)

**Interfaces:**
- Produces (`extra="forbid"`, frozen, like `report._Model`):
  - `RegisterMachine(public_key_b64: str, label: str)` — `public_key_b64` must decode to exactly 32 bytes (validator); `label` 1–64 chars.
  - `RegisterResponse(fingerprint: str, claimed: bool)`
  - `ClaimCodeNotice(claim_code_hash: str, expires_at: AwareDatetime | None)` — hash is the edge's `hash_secret` string, 1–256 chars; `None` expiry means a setup code, which never expires.
- Check `tests/cloud/reports.py` and any test asserting `"contract": "0.1.0"`; update to the report's `contract` field semantics (the report body is unchanged; only the version string moves).

- [ ] **Step 1:** Tests: `test_register_machine_rejects_short_key`, `test_register_machine_forbids_extra`, `test_claim_code_notice_requires_aware_datetime`, `test_contract_version_is_0_2_0`.
- [ ] **Step 2:** Run → fail. **Step 3:** Implement, bump, regenerate schemas. **Step 4:** `uv run pytest tests/contracts tests/cloud -q` passes (fix fixtures that pin `0.1.0`).
- [ ] **Step 5:** Commit `feat(contracts): cloud identity models; edge-cloud contract 0.2.0`.

---

### Task 3: Edge `MachineIdentity` file

**Files:**
- Create: `packages/edge/src/ice_edge/uplink/identity.py`
- Test: `tests/edge/uplink/test_identity.py`

**Interfaces:**
- Consumes: Task 1 helpers.
- Produces:
  - `IDENTITY_FILE = DATA_DIR / "cloud_identity.json"` (from `ice_common.paths`)
  - `@dataclass(frozen=True) ClaimNotice(claim_code_hash: str | None, expires_at: str | None, acked: bool)` — `claim_code_hash is None` means "no live code" (a pending `DELETE`).
  - `class MachineIdentity` with `@classmethod load_or_create(path: Path = IDENTITY_FILE) -> MachineIdentity`; properties `fingerprint: str`, `short_fingerprint: str`, `public_key_b64: str`, `label_hint: None`-free; `sign(method: str, path: str, timestamp: str, body: bytes) -> str`; `notice: ClaimNotice | None` (property); `set_notice(claim_code_hash: str | None, expires_at: str | None) -> None` (writes `acked=False`, replaces any previous notice); `mark_notice_acked() -> None`; `path: Path`.
  - File JSON: `{"version": 1, "private_key_b64": ..., "public_key_b64": ..., "created_at": iso, "notice": {...} | null}`; written atomically (tmp + `os.replace` + `fsync_dir`), `os.chmod(path, 0o600)` on POSIX (follow `AccessStore._commit`'s pattern, but do not import it).
  - A corrupt file raises `IdentityError` (subclass `RuntimeError`); it is never silently regenerated.

- [ ] **Step 1:** Tests: `test_creates_file_once_and_reloads_same_fingerprint`; `test_mode_0600_on_posix` (skip on Windows); `test_corrupt_file_raises_not_regenerates`; `test_sign_verifies_with_public_key` (uses Task 1 `verify`); `test_set_notice_persists_unacked_and_ack_flips`; `test_newer_notice_replaces_older` (one slot, never a queue); `test_set_notice_none_hash_means_delete_pending`.
- [ ] **Steps 2–4:** fail → implement → pass. `uv run lint-imports` (identity sits in `ice_edge.uplink`, imports only `ice_common`, `ice_contracts`).
- [ ] **Step 5:** Commit `feat(edge): machine cloud identity file with persisted claim-code notice`.

---

### Task 4a: Move `Backoff` to `ice_common.backoff`

**Files:**
- Create: `packages/common/src/ice_common/backoff.py`
- Modify: `packages/edge/src/ice_edge/access/access.py` (delete the class and the helpers only it uses; `from ice_common.backoff import Backoff  # noqa: F401` re-export)
- Test: existing `tests/edge/access/test_access.py::TestBackoff*` unchanged and green; add `tests/common/test_backoff.py` importing from the new home with one smoke test (`first attempt allowed; failure then delay`).

**Interfaces:** `Backoff` public API unchanged (`__init__(clock, trusted_proxies)`, `check(kind, subject, client, *, trusted=False) -> float | None`, `record_failure(...)`, `record_success(...)`, `client_key`-related helpers stay wherever they are today; move only what the class itself needs: `ipaddress`, `deque`, `time`).

- [ ] **Step 1:** Write the smoke test → fails on import. **Step 2–3:** Move verbatim; keep every existing edge import path working. **Step 4:** `uv run pytest tests/edge/access tests/common -q`, `uv run lint-imports`, `uv run python scripts/check_package_isolation.py`.
- [ ] **Step 5:** Commit `refactor(common): move Backoff to ice_common for cloud reuse`.

---

### Task 4: `AccessStore` link code and claim-code observer

**Files:**
- Modify: `packages/edge/src/ice_edge/access/access.py`
- Test: `tests/edge/access/test_access.py` (new class `TestCloudClaimCode`)

**Interfaces:**
- Produces on `AccessStore`:
  - persisted key `pending_link`: `{"code_hash", "started_at", "expires_at"}` or null (add to `load`/`_commit`/the `_raw_extra` exclusion list).
  - `pending_link -> dict | None` property (expiry-checked like `pending_transfer`).
  - `start_link(started_by_user_id: str) -> str` (8-digit plaintext, shown once; raises `AccessError` if a transfer is pending); `cancel_link() -> None`.
  - `cloud_claim_code() -> tuple[str, str | None] | None` → `(hash, expires_at_iso | None)` by priority: live transfer → live link → unfinalised setup. A setup code has no expiry, so it returns `expires_at = None`; `ClaimCodeNotice.expires_at` is `AwareDatetime | None` (Task 2 defines it that way), and the cloud treats `None` as "no expiry".
  - `subscribe_claim_code_change(cb: Callable[[], None]) -> Callable[[], None]` using `ice_common.observers.Subscribers`; published after `_commit()` in: `begin_setup`, `finalize_setup`, `start_transfer`, `cancel_transfer`, `complete_transfer`, `start_link`, `cancel_link`. `start_transfer` also clears `pending_link`.
  - The observer carries no payload; subscribers call `cloud_claim_code()`.

- [ ] **Step 1:** Tests: `test_start_link_returns_code_and_hash_verifies`; `test_link_expires_after_ttl`; `test_start_link_refused_while_transfer_pending`; `test_start_transfer_clears_pending_link`; `test_priority_transfer_then_link_then_setup`; `test_setup_code_has_no_expiry`; `test_observer_fires_on_each_transition` (seven transitions, count == 7, and `complete_transfer` leaves the same hash live, `finalize_setup` leaves `None`); `test_pending_link_survives_reload`.
- [ ] **Steps 2–4:** fail → implement → pass; `uv run pytest tests/edge/access -q`.
- [ ] **Step 5:** Commit `feat(edge): link-to-cloud claim code and claim-code change observer`.

---

### Task 5: Edge UI: Link to cloud panel and fingerprint display

**Files:**
- Modify: `packages/edge/src/ice_edge/web/routes/users/ownership.py`, `packages/ui/src/ice_ui/templates/users_ownership.html`, `packages/edge/src/ice_edge/web/routes/health/overview.py`, `packages/ui/src/ice_ui/templates/health.html`
- Modify: `packages/edge/src/ice_edge/web/context.py` (new global `machine_identity: MachineIdentity | None` + `set_machine_identity`; add to `tests/route_context.py::GLOBALS`)
- Test: `tests/edge/web/test_routes_users.py` (new tests), `tests/edge/web/test_route_context.py` (globals guard passes)

**Interfaces:**
- `POST /users/ownership/link` form `pin`; gates `manage_ownership` + `require_htmx`; `_check_owner_pin`; on success `start_link(user.id)` and render `users_ownership.html` with `link_code=<plaintext>` and `short_fingerprint`, `Cache-Control: no-store` (already unconditional in `_render_ownership`). Refused with the transfer-pending message when a transfer is live.
- `POST /users/ownership/link/cancel` form `pin`; same gates; `cancel_link()`.
- Template: a "Link to cloud" card shown only when `cloud_enabled` (pass `context.config.cloud.enabled`); shows `short_fingerprint` always when enabled, the code once, the pending-link state with expiry and Cancel, and the existing transfer card unchanged. Use only existing CSS classes.
- `/health`: when `context.machine_identity` is set, add `cloud_fingerprint=<short>` to the context and one `<span class="font-mono text-xs text-gray-500">` line in `health.html` beside the machine id. The parity test narrows the edge to `REMOTE_VIEWER_PERMISSIONS` and the cloud has no identity in that fixture, so render the line only when `cloud_fingerprint` is truthy; the parity fixture leaves `machine_identity` `None`.

- [ ] **Step 1:** Tests: `test_link_requires_manage_ownership` (tech → 403), `test_link_wrong_pin_no_code`, `test_link_shows_code_once_with_no_store`, `test_link_refused_while_transfer_pending`, `test_link_cancel`, `test_link_card_hidden_when_cloud_disabled`, `test_health_shows_short_fingerprint_when_identity_set`.
- [ ] **Steps 2–4:** fail → implement → pass; `uv run pytest tests/edge/web tests/ui tests/cloud/test_ui_parity.py -q`.
- [ ] **Step 5:** Commit `feat(edge): link-to-cloud code on Ownership; fingerprint on Health`.

---

### Task 6: Signed `HttpUplink` with register and claim-code calls

**Files:**
- Modify: `packages/edge/src/ice_edge/uplink/client.py`
- Test: `tests/edge/uplink/test_client.py`

**Interfaces:**
- `HttpUplink.__init__(base_url, identity: MachineIdentity, *, timeout=5.0, transport=None, now: Callable[[], datetime] = utc_now)` — replaces the `machine_id` argument.
- `_send` adds the three headers from Task 1 on **every** request (bundle `PUT` included), signing `canonical_message(method, path, timestamp, content)`.
- `post_report(report) -> bool` → `POST /api/v1/machines/{identity.fingerprint}/report`.
- `register(label: str) -> bool` → `POST /api/v1/machines/register` with `RegisterMachine`; `True` on 200.
- `post_claim_code(notice: ClaimCodeNotice) -> bool` → `POST /api/v1/machines/{fp}/claim-code`; `delete_claim_code() -> bool` → `DELETE` same path. Both `True` on 2xx.

- [ ] **Step 1:** Tests (httpx `MockTransport`, a real `MachineIdentity` in `tmp_path`): `test_every_request_carries_valid_signature` (handler verifies with Task 1 `verify` for PUT, POST report, register, claim-code POST/DELETE); `test_report_path_uses_fingerprint`; `test_register_body_is_RegisterMachine`; `test_stale_now_still_sends_but_server_401_is_false`; existing failure tests updated for the new constructor.
- [ ] **Steps 2–4.** **Step 5:** Commit `feat(edge): uplink signs every request; register and claim-code calls`.

---

### Task 7: Uplink loop order and `__main__` wiring

**Files:**
- Modify: `packages/edge/src/ice_edge/uplink/loop.py`, `packages/edge/src/ice_edge/__main__.py`
- Test: `tests/edge/uplink/test_loop.py`

**Interfaces:**
- `run_uplink(machine, config, uplink, *, identity: MachineIdentity, sleep=..., now=..., holder_name=None)`.
- Order per pass: (1) until registered: `uplink.register(config.machine_id)`, sleep, retry; (2) if `identity.notice` and not acked: `post_claim_code`/`delete_claim_code` (by `claim_code_hash is None`), `mark_notice_acked()` on `True`; (3) until bundle accepted: `put_ui_bundle` once per pass; (4) `post_report`. Steps 2–4 happen every pass; a failure in any step logs and continues to the next pass. No report before registration.
- `__main__`: when `cloud.enabled`: `identity = MachineIdentity.load_or_create()`; `routes.set_machine_identity(identity)`; `HttpUplink(url, identity)`; subscribe `access_store.subscribe_claim_code_change(lambda: identity.set_notice(*(access_store.cloud_claim_code() or (None, None))))`; seed the notice once at startup from `cloud_claim_code()` (so a code minted while the cloud was disabled is sent).

- [ ] **Step 1:** Tests with `FakeUplink` gaining `register`, `post_claim_code`, `delete_claim_code` with scripted results: `test_register_precedes_everything_and_blocks_reports`; `test_unacked_notice_sent_each_pass_until_2xx_then_acked`; `test_delete_notice_when_hash_none`; `test_bundle_still_once`; existing interval tests updated.
- [ ] **Steps 2–4.** **Step 5:** Commit `feat(edge): uplink registers first and retries claim-code notices`.

---

### Task 8: `CloudSettings` and app factory

**Files:**
- Create: `packages/cloud/src/ice_cloud/settings.py`
- Modify: `packages/cloud/src/ice_cloud/app.py`, `packages/cloud/src/ice_cloud/__main__.py`, `tests/cloud/conftest.py` (fixtures build settings), every `create_app(` call in `tests/cloud/`
- Test: `tests/cloud/test_settings.py`

**Interfaces:**
- `@dataclass(frozen=True) CloudSettings(data_dir: Path, public_url: str | None, google_client_id: str | None, google_client_secret: str | None, admins: frozenset[str], session_key: bytes, insecure_cookies: bool = False)`; `@property google_configured -> bool`; `@classmethod from_env(env: Mapping[str, str]) -> CloudSettings` raising `SettingsError` when `ICE_CLOUD_SESSION_KEY` is missing or < 32 bytes, or Google is configured without `ICE_CLOUD_PUBLIC_URL`; `admins` lower-cased, stripped, empty entries dropped.
- `create_app(settings: CloudSettings, *, now=utc_now, provider: OidcProvider | None = None) -> FastAPI`; `app.state.settings`, `app.state.repo` (Task 9), `app.state.provider`.
- `__main__`: `from_env(os.environ)`; on `SettingsError` print the message and `sys.exit(1)`.
- `tests/cloud/conftest.py`: `settings(tmp_path)` fixture with `session_key=b"k"*32`, `insecure_cookies=True`, admins `{"admin@example.com"}`; `cloud_app(settings, clock)`.

- [ ] **Step 1:** Tests: `test_from_env_minimal`, `test_missing_session_key_is_error`, `test_short_session_key_is_error`, `test_google_without_public_url_is_error`, `test_admins_normalised`.
- [ ] **Steps 2–4:** `uv run pytest tests/cloud -q` green after fixture migration (ingest and portal tests still pass; auth is not yet enforced in this task).
- [ ] **Step 5:** Commit `refactor(cloud): CloudSettings from env; create_app takes settings`.

---

### Task 9: `Repository` protocol and SQLite implementation

**Files:**
- Create: `packages/cloud/src/ice_cloud/store/__init__.py`, `store/snapshots.py` (today's `store.py` moved; key = fingerprint; `check_machine_id` → `check_fingerprint` accepting 64 hex), `store/repository.py`, `store/sqlite.py`
- Delete: `packages/cloud/src/ice_cloud/store.py`
- Test: `tests/cloud/test_repository.py`; `tests/cloud/test_store.py` updated for fingerprint keys

**Interfaces (`repository.py`, all frozen dataclasses; times are `datetime` UTC):**
- `Account(id, google_sub, email, name, created_at, suspended_at: datetime | None)`
- `Machine(fingerprint, public_key_b64, label, owner_id: str | None, registered_at, last_seen_at, claim_code_hash: str | None, claim_code_expires_at: datetime | None, claimed_at: datetime | None)`
- `Membership(account_id, fingerprint, role: str, granted_by, granted_at)`
- `Invitation(token, inviter_id, email, scope: str | tuple[str, ...], created_at, expires_at, accepted_by: str | None, accepted_at: datetime | None)` — `scope == "fleet"` or a tuple of fingerprints.
- `MachineAccess(machine: Machine, role: Literal["owner", "tech", "admin"])`
- `class Repository(Protocol)`:
  - accounts: `upsert_account_from_login(google_sub, email, name, now) -> Account`; `account(id) -> Account | None`; `account_by_email(email) -> Account | None`; `set_suspended(id, suspended_at: datetime | None) -> None`; `all_accounts() -> list[Account]`
  - machines: `register_machine(fingerprint, public_key_b64, label, now) -> Machine` (insert or update label/last_seen); `machine(fingerprint) -> Machine | None`; `resolve_short(short16: str) -> Machine | None` (prefix match; `None` if 0 or >1); `touch_seen(fingerprint, now)`; `set_claim_code(fingerprint, hash: str | None, expires_at: datetime | None)`; `claim_machine(fingerprint, account_id, now) -> bool` (one transaction: owner set, memberships deleted, claim cleared); `unbind_machine(fingerprint) -> None`; `all_machines() -> list[Machine]`
  - access: `machines_for_account(account_id) -> list[MachineAccess]` (owned + tech, no admin logic); `access_for(account_id, fingerprint) -> Literal["owner","tech"] | None`; `memberships(fingerprint) -> list[tuple[Membership, Account]]`; `grant_tech(account_id, fingerprint, granted_by, now)`; `revoke(account_id, fingerprint)`
  - invitations: `create_invitation(inviter_id, email, scope, now, ttl=timedelta(days=7)) -> Invitation`; `invitation(token) -> Invitation | None`; `invitations_by(inviter_id, fingerprint: str | None) -> list[Invitation]` (pending only); `accept_invitation(token, account_id, now) -> list[str]` (fingerprints granted; expands `fleet` to inviter's current machines; marks accepted; one transaction); `cancel_invitation(token) -> None`
- `sqlite.py`: `SqliteRepository(path: Path)`; `MIGRATIONS: list[str]` applied in order on construction under `schema_version`; one connection per call via a `_connect()` context manager with `PRAGMA journal_mode=WAL; synchronous=NORMAL; foreign_keys=ON`.

- [ ] **Step 1:** Tests for every method above, plus `test_claim_is_atomic_when_membership_delete_fails` (monkeypatch to raise mid-transaction; owner unchanged), `test_resolve_short_ambiguous_is_none`, `test_migrations_idempotent_on_reopen`, `test_fleet_scope_expands_only_current_machines`.
- [ ] **Steps 2–4.** **Step 5:** Commit `feat(cloud): Repository protocol with SQLite implementation; snapshots keyed by fingerprint`.

---

### Task 10: Machine authentication and the signed machine API

**Files:**
- Create: `packages/cloud/src/ice_cloud/machine_auth.py`
- Modify: `packages/cloud/src/ice_cloud/ingest.py`
- Test: `tests/cloud/test_machine_auth.py`, `tests/cloud/test_register.py`, `tests/cloud/test_ingest.py` (updated to sign); `tests/cloud/conftest.py` gains `signer` helper

**Interfaces:**
- `machine_auth.py`: `async def require_machine(request: Request) -> Machine` FastAPI dependency: headers present → `is_timestamp_fresh` with `app.state.now()` → body bytes (`await request.body()`; FastAPI caches it) → machine by `HEADER_MACHINE` (for the register path only, derive from the body's `public_key_b64` and require equality with the header; unknown allowed) → `verify` → `repo.touch_seen` → return. Failure: `HTTPException(401)` with `detail=None` (empty body via a custom `Response(status_code=401)`), one `logger.warning`.
- `ingest.py`: `POST /api/v1/machines/register` → `RegisterResponse` 200; `POST /api/v1/machines/{fingerprint}/report` 204 (path must equal `machine.fingerprint`, else 401; body `machine_id` is the label and is no longer checked against the path); `POST /api/v1/machines/{fingerprint}/claim-code` body `ClaimCodeNotice` → `repo.set_claim_code`, 204; `DELETE` same → `set_claim_code(fp, None, None)`, 204; `PUT /api/v1/ui-bundles/{digest}` now also requires `require_machine`.
- `tests/cloud/conftest.py`: `class Signer` (fresh key per test) with `headers(method, path, body=b"") -> dict` and `fingerprint`; `register(cloud, signer, label="m-0042") -> str` and `push(cloud, signer, report)` helpers replace the old `push`.

- [ ] **Step 1:** Tests: each missing header → 401; stale timestamp → 401; unknown fingerprint on report → 401; bad signature → 401; path/fingerprint mismatch → 401; register header/body mismatch → 401; register twice → same row, label updated, `claimed=false`; report after register → 204 and `last_seen_at` moved; claim-code POST then DELETE → row fields set then cleared; bundle PUT unsigned → 401; replayed report (same seq) → 204 and store unchanged (existing behaviour).
- [ ] **Steps 2–4:** `uv run pytest tests/cloud -q` — `test_ui_parity.py` will fail until Task 12; mark it with a clear `pytest.skip` reason **only inside this task's commit**, listed in `tests/skip_policy.py` is NOT allowed — instead update the parity fixture now to register and push through `Signer` (the select step still works unauthenticated until Task 11, which then updates it again).
- [ ] **Step 5:** Commit `feat(cloud): signed machine API; register, report by fingerprint, claim-code notices`.

---

### Task 11: Google sign-in, sessions, access dependencies

**Files:**
- Modify: `packages/cloud/src/ice_cloud/auth.py` (replaces the docstring-only stub)
- Create: `packages/cloud/src/ice_cloud/templates/login.html`
- Modify: `packages/cloud/src/ice_cloud/app.py` (include `auth.router`, pass `provider`)
- Test: `tests/cloud/test_auth.py`; `tests/cloud/conftest.py` gains `FakeProvider`, `login(cloud, email, sub=None) -> Account` helper

**Interfaces:**
- `IdentityClaims(sub: str, email: str, email_verified: bool, name: str)` (frozen dataclass).
- `class OidcProvider(Protocol)`: `def authorize_url(self, *, state: str, nonce: str, code_challenge: str) -> str`; `async def exchange(self, *, code: str, code_verifier: str, nonce: str) -> IdentityClaims` (raises `SignInError` on any verification failure).
- `GoogleProvider(client_id, client_secret, redirect_uri, *, http: httpx.AsyncClient | None = None)`: lazy discovery fetch, JWKS cache with one refresh on `kid` miss, RS256 verification with `cryptography`, checks `iss` ∈ {`https://accounts.google.com`, `accounts.google.com`}, `aud`, `exp`, `nonce`, `email_verified`. Unit-tested against a locally generated RSA key and `httpx.MockTransport` serving discovery, JWKS and token endpoints.
- Sessions: `SESSION_COOKIE = "ice_cloud_session"`, `IDLE_LIMIT = 30 days`, `ABSOLUTE_LIMIT = 90 days`, `REFRESH_EVERY = 1 hour`; `encode_session(key, account_id, issued_at, last_active_at) -> str`; `decode_session(key, value, now) -> SessionData | None` (HMAC-SHA256 compare_digest; expired or tampered → `None`).
- Dependencies: `current_account(request) -> Account | None` (decodes, loads, `None` if suspended); `require_signed_in(request) -> Account` (else `RedirectResponse(f"/login?next={path}", 303)` raised via `HTTPException` handler, or return the redirect from a dependency using FastAPI's exception pattern — implementer picks one and documents it); `require_machine_access(short: str, *, owner_only: bool = False)` factory returning `(Account, Machine, role)`; non-member → 404; admin counts as access and as owner-level for `owner_only=False` only (admin cannot invite/revoke); `is_admin(settings, account) -> bool`; `require_admin`.
- Routes: `GET /login` (renders `login.html`; "not configured" text when `not settings.google_configured`; shows the button otherwise); `GET /auth/google/start` (state+nonce+verifier in a 10-minute signed cookie `ice_cloud_oidc`); `GET /auth/google/callback` (state check, exchange, `repo.upsert_account_from_login`, set session, redirect to `next` if it is a relative path, else `/fleet`); `POST /logout` (`require_htmx`, clears cookie, 303 `/login`).
- `require_htmx` for the cloud: a tiny dependency in `auth.py` (do not import the edge's).
- `FakeProvider(claims: IdentityClaims)` in `tests/cloud/conftest.py`: `authorize_url` returns `/auth/google/callback?code=fake&state=<state>`; `exchange` returns its claims (or raises when `claims.email_verified` is false). `login()` helper drives start → callback with `follow_redirects=False` and returns the account.

- [ ] **Step 1:** Tests: `test_login_page_says_not_configured_without_google`; `test_sign_in_creates_account_then_updates_name_on_return`; `test_unverified_email_refused`; `test_bad_state_refused`; `test_session_tamper_is_signed_out`; `test_idle_and_absolute_expiry` (clock fixture); `test_suspended_is_signed_out`; `test_logout_requires_htmx_and_clears`; `test_google_provider_verifies_rs256_and_rejects_wrong_aud_nonce_exp` (mock transport); `test_signed_out_redirects_to_login_with_next`.
- [ ] **Steps 2–4.** **Step 5:** Commit `feat(cloud): Google OIDC sign-in, signed session cookie, access dependencies`.

---

### Task 12: Portal: My machines, select, access-gated machine pages

**Files:**
- Modify: `packages/cloud/src/ice_cloud/portal.py`, `packages/cloud/src/ice_cloud/templates/fleet.html`, `not_remote.html` as needed
- Test: `tests/cloud/test_portal.py` (rework), `tests/cloud/test_ui_parity.py` (fixture signs in and selects), `tests/cloud/test_banner_boosted_browser.py` (fixture only)

**Interfaces:**
- `GET /fleet` (`require_signed_in`): rows from `repo.machines_for_account(account.id)` (admins: `repo.all_machines()` with role `"admin"`), joined to `store.latest(fp)` for online/age/fault line (no report → "never reported"); `role` column; Select form posts `/fleet/select/{short}`; empty state links `/claim`; a `Claim a machine` link always; owner rows link to `/machines/{short}/people`.
- `POST /fleet/select/{short}` and `GET /m/{short}`: `require_machine_access`, set cookie to the full fingerprint (`httponly`, `samesite=lax`, `secure` unless `insecure_cookies`), 303 `/`.
- `_selected(request) -> MachineReport | None` becomes `_selected(request, account) -> tuple[Machine, MachineReport | None] | None`: cookie fingerprint → `repo.machine` → `access_for`/admin → `store.latest`. All machine pages use it; no access → `_to_fleet()` / 204 for htmx fragments. A selected machine with no report yet renders the existing `vmc_missing`-style page: reuse `VMC_MISSING_HTML` for `/status`, and for full pages show `not_remote.html` variant text "No report from this machine yet" (add `no_report.html`).
- `_remote(...)` adds `"short_fingerprint"`; `partials/remote_banner.html` in `ice_ui` shows it after the label (edge never renders the banner, so parity is unaffected; `tests/ui/test_static_css.py` must still pass).
- `current_user` for `base.html` is the signed-in account's `name` via `RemoteViewer(display_name=account.name)`.
- Catch-all `/{path:path}` stays 404 "Not available remotely yet" but now requires sign-in.

- [ ] **Step 1:** Tests: `test_fleet_lists_only_my_machines_with_roles` (owner of A, tech on B, stranger to C); `test_admin_sees_every_machine`; `test_select_non_member_404`; `test_machine_pages_require_access_and_redirect_to_fleet`; `test_fragment_without_access_is_204_for_htmx`; `test_cookie_alone_is_not_enough` (cookie set, then access revoked → 303); `test_deep_link_selects`; parity tests updated: owner signs in, machine registered+claimed via repo (`repo.claim_machine` directly in the fixture), `/fleet/select/{short}`, then the unchanged `_assert_parity`.
- [ ] **Steps 2–4:** `uv run pytest tests/cloud tests/ui -q`. **Step 5:** Commit `feat(cloud): My machines and access-gated machine pages`.

---

### Task 13: Claiming

**Files:**
- Create: `packages/cloud/src/ice_cloud/claim.py`, `packages/cloud/src/ice_cloud/templates/claim.html`
- Modify: `packages/cloud/src/ice_cloud/app.py` (router, `app.state.backoff = Backoff(clock=monotonic-from-now)`)
- Test: `tests/cloud/test_claim.py`

**Interfaces:**
- `GET /claim` (`require_signed_in`): form `fingerprint` (short, with placeholder `xxxx-xxxx-xxxx-xxxx`) and `code` (8 digits, `inputmode=numeric`); `POST /claim` (`require_signed_in`, `require_htmx`): §5.4 steps 1–4 exactly; `client` key = `request.client.host`; success → set the selection cookie and 303 `/`; 429 includes `Retry-After`.
- `Backoff` from `ice_common.backoff`, kind `"claim"`, subject = short fingerprint (or the literal `"unknown"` when unparsable), `trusted=False`.
- Expiry: `claim_code_expires_at is None` means no expiry (setup code); otherwise compare with `now()`.

- [ ] **Step 1:** Tests: `test_wrong_code_403_and_then_backoff_429_with_retry_after`; `test_no_open_claim_same_403_message`; `test_expired_code_403`; `test_setup_code_without_expiry_claims`; `test_success_sets_owner_drops_techs_clears_hash_and_selects`; `test_old_owner_then_gets_404_on_select`; `test_claim_before_delete_notice_and_after` (claim succeeds, then a signed `DELETE` arrives → still owned; and `DELETE` first → 403); `test_requires_sign_in`.
- [ ] **Steps 2–4.** **Step 5:** Commit `feat(cloud): claim a machine with the code minted at the machine`.

---

### Task 14: People: memberships and invitations

**Files:**
- Create: `packages/cloud/src/ice_cloud/people.py`, templates `people.html`, `invite.html`, `partials/people_revoke.html` (two-tap confirm, following `ice_ui/templates/partials/confirm_button.html`'s contract: honour `confirming=false`)
- Modify: `app.py` (router)
- Test: `tests/cloud/test_people.py`

**Interfaces:**
- `GET /machines/{short}/people` (owner only): memberships with Revoke two-tap (`GET /machines/{short}/people/{account_id}/revoke/confirm?confirming=`, `POST .../revoke`), invite form (`email`, `scope` radio `machine`/`fleet`), pending invitations (`invitations_by(owner, fp)` plus fleet-scoped ones) each with the shareable link `{public_url or request.base_url}/invite/{token}` and Cancel; a "Transfer ownership" panel of static text (§6.3).
- `POST /machines/{short}/people/invite` (owner, htmx): lower-case email, validate shape (one `@`, no spaces), `create_invitation`, re-render with the link shown.
- `POST /machines/{short}/people/invitations/{token}/cancel` (owner of that invitation, htmx).
- `GET /invite/{token}` (signed-in): shows inviter name, scope, and an Accept button; mismatched email → page says the invitation is for a different address (no accept); expired/accepted/unknown → 404.
- `POST /invite/{token}` (signed-in, htmx): `accept_invitation` → 303 `/fleet`.

- [ ] **Step 1:** Tests: `test_people_owner_only_404_for_tech_and_admin`; `test_invite_creates_pending_with_link`; `test_accept_matching_email_grants_machine_scope`; `test_accept_fleet_scope_grants_current_machines_only` (owner buys a machine after acceptance → not granted); `test_accept_wrong_email_refused`; `test_expired_token_404`; `test_revoke_two_tap_and_removes`; `test_cancel_invitation`; `test_invite_bad_email_422_or_error_message`.
- [ ] **Steps 2–4.** **Step 5:** Commit `feat(cloud): owners invite and revoke techs`.

---

### Task 15: Platform admin

**Files:**
- Create: `packages/cloud/src/ice_cloud/admin.py`, `templates/admin.html`, `partials/admin_unbind.html` (two-tap)
- Modify: `app.py`
- Test: `tests/cloud/test_admin.py`

**Interfaces:**
- `GET /admin` (`require_admin`): machines table (label, short fp, owner email or "unclaimed", last seen, claim open yes/no, Unbind two-tap, deep link `/m/{short}`); accounts table (email, name, suspended, Suspend/Unsuspend).
- `POST /admin/machines/{short}/unbind`, `GET .../unbind/confirm?confirming=` (two-tap), `POST /admin/accounts/{id}/suspend`, `POST /admin/accounts/{id}/unsuspend` — all `require_admin` + `require_htmx`.
- Admin never sets an owner; there is no route for it.

- [ ] **Step 1:** Tests: `test_non_admin_404`; `test_admin_sees_unclaimed_and_claimed`; `test_unbind_clears_owner_and_memberships`; `test_suspend_signs_account_out_and_unsuspend_restores`; `test_admin_cannot_assign_owner` (no such route: POST to `/admin/machines/{short}/owner` is 404).
- [ ] **Steps 2–4.** **Step 5:** Commit `feat(cloud): platform admin page`.

---

### Task 16: Bundle sandbox context hardening

**Files:**
- Modify: `packages/cloud/src/ice_cloud/ui_registry.py`, `packages/cloud/src/ice_cloud/portal.py` (`_context` builds a restricted dict when `ui.sandboxed`)
- Test: `tests/cloud/test_ui_registry.py`

**Interfaces:**
- `ResolvedUi` gains `sandboxed: bool` (true for an uploaded bundle).
- `_context(...)` omits `request` when `ui.sandboxed`; `Jinja2Templates.TemplateResponse` still needs a `request` argument — pass it positionally to FastAPI but strip the `"request"` key from the context mapping that the template sees (verify with a bundle template that renders `{{ request }}` → empty/undefined, never the app state).

- [ ] **Step 1:** Tests: `test_sandboxed_bundle_cannot_see_request_or_app_state` (upload a bundle whose `home.html` prints `{{ request.app.state }}`; response contains neither `SnapshotStore` nor `settings`); `test_installed_ui_keeps_request`.
- [ ] **Steps 2–4.** **Step 5:** Commit `fix(cloud): uploaded bundles render without request in context`.

---

### Task 17: Deployment, docs, CI

**Files:**
- Modify: `docker/docker-compose.cloud.yml` (env vars from `.env`, drop the "no auth" header), `.env.example` (`ICE_CLOUD_PUBLIC_URL`, `ICE_CLOUD_GOOGLE_CLIENT_ID`, `ICE_CLOUD_GOOGLE_CLIENT_SECRET`, `ICE_CLOUD_ADMINS`, `ICE_CLOUD_SESSION_KEY`, `ICE_CLOUD_INSECURE_COOKIES` with comments), `packages/cloud/Dockerfile` header comment, `.github/workflows/ci.yml` (compose-config still passes with `.env.example`)
- Create: `packages/cloud/README.md` (run locally, Google OAuth client setup with redirect URI `{PUBLIC_URL}/auth/google/callback`, reverse-proxy HTTPS note, claim/transfer procedure, admin env, key-rotation procedure = delete `data/cloud_identity.json` and re-claim)
- Modify: `CLAUDE.md` Cloud section (replace the "dev-only, no-auth" paragraph with the new model: fingerprint identity, claim codes, roles, env vars, SQLite repository, parity rule unchanged), `docs/superpowers/specs/2026-10-13-cloud-monitoring-server-design.md` §8 (one line: superseded by the 2026-10-19 spec), `docs/overview-plain.md` Cloud bullet (one sentence), `docs/contracts/edge-cloud/CONTRACT.md` if present (0.2.0 section: headers, register, claim-code).
- Test: `uv run pytest` full suite; `docker compose --env-file .env.example -f docker/docker-compose.cloud.yml config -q`.

- [ ] **Step 1:** Run the full suite and the compose config check; fix anything red. **Step 2:** Write docs. **Step 3:** `uv run ruff check --fix .`, `uv run ruff format .`, `uv run lint-imports`, `uv run python scripts/check_package_isolation.py`.
- [ ] **Step 4:** Commit `docs(cloud): identity and tenancy deployment, README, CLAUDE.md`.

---

## Self-review notes

- Spec coverage: §3 → T9; §4.1 → T3; §4.2 → T1, T2; §4.3–4.4 → T10, T6, T7; §5.1–5.3 → T4, T5, T7; §5.4 → T13; §5.5 → T15; §6.1–6.2 → T11; §6.3 → T12, T13, T14, T15; §6.4 → T16; §7 → T9; §8 → T8, T17; §9 is spread across T10, T13, T11, T7; §10 maps one-to-one onto the task tests.
- Type consistency: `MachineIdentity.notice: ClaimNotice | None`, `set_notice(hash | None, expires_at | None)`; `ClaimCodeNotice.expires_at: AwareDatetime | None` (decided in T4, applies to T2 — implement T2 with the optional field from the start); `Repository.set_claim_code(fp, hash | None, expires_at | None)`; `require_machine_access(short, *, owner_only)` returns `(Account, Machine, role)`.
- `HttpUplink` constructor change (T6) breaks `__main__` until T7; T6 and T7 are dispatched in sequence, and T6's commit may leave `__main__` red for one commit — acceptable only if T7 follows immediately in the same session. Prefer: T6 updates `__main__` minimally (`HttpUplink(url, MachineIdentity.load_or_create())`) so every commit is green.
