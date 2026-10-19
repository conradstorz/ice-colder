# Cloud identity and tenancy — design

Date: 2026-10-19. Status: approved in brainstorming; supersedes cloud spec
(`2026-10-13-cloud-monitoring-server-design.md`) §8 "Security (stub)" and
retires its "never beyond localhost" rule for this server once implemented.

## 1. Goal

Turn the single-tenant, no-auth `ice_cloud` stub into a multi-owner fleet server:

- Each machine has exactly one owner account. Owners see only their own machines.
- Owners invite their own technicians; a tech sees the machines they were granted.
- A machine proves its identity on every request with a key it generated itself.
- Ownership is claimed, and transferred on resale, with a code minted **at the machine**, mirroring the
  edge's existing setup-code and transfer-code flow (roles-and-access spec §3).
- People sign in with Google. A platform admin (the operating company) is an allowlist of Google emails.

This spec is the first of a series. **Out of scope** (each gets its own spec): alert forwarding to owners
and techs, the public marketing site and machine finder, remote commands, device shadow, the durable event
stream, Apple or Microsoft sign-in, PostgreSQL.

The edge is unchanged in purpose: its local technician dashboard and the customer `/screen` keep working
offline at the machine with no cloud at all. Everything here is additive and off unless `cloud.enabled`.

## 2. Decisions

| # | Question | Decision |
|---|---|---|
| D1 | Who creates owner accounts | Self-signup via Google **and** platform admin tooling. Self-signup is what lets a buyer open an account and receive a resold machine. |
| D2 | One transfer or two | **One**, minted at the machine. The machine's setup/transfer/link code is also the cloud claim code; the cloud only ever holds its hash. First enrollment and resale are the same mechanism. |
| D3 | Techs | Owner-invited, scoped per machine (or whole current fleet at acceptance). No self-registered techs, no platform-assigned techs. |
| D4 | Sign-in | Google OpenID Connect only, hand-rolled against the discovery document (no auth library). Provider behind one interface so Apple/Microsoft are a config entry later. |
| D5 | Platform admin | `ICE_CLOUD_ADMINS`, a comma-separated allowlist of Google emails in the server environment. No admin-management UI. |
| D6 | Storage | SQLite now, behind a `Repository` protocol with every query in one module, so Postgres later is a second implementation. Report snapshots keep their file-per-machine store. |
| D7 | Machine credential | Ed25519 keypair generated on the edge at first use; every request signed. No bearer tokens, no mTLS. HTTPS is the reverse proxy's job. |
| D8 | Machine identity | The **public-key fingerprint**, never config `machine_id` (editable, collidable). `machine_id` is a display label. |

## 3. Entities and roles

One SQLite database, `<ICE_CLOUD_DATA>/cloud.db`, four tables:

| Table | Primary key | Columns |
|---|---|---|
| `accounts` | `id` (uuid4) | `google_sub` (unique), `email` (unique, lower-cased), `name`, `created_at`, `suspended_at` (null) |
| `machines` | `fingerprint` | `public_key` (base64 raw Ed25519), `label` (last reported `machine_id`), `owner_id` (null = unclaimed, FK accounts), `registered_at`, `last_seen_at`, `claim_code_hash` (null), `claim_code_expires_at` (null), `claimed_at` (null) |
| `memberships` | (`account_id`, `fingerprint`) | `role` (`tech` only for now), `granted_by` (FK accounts), `granted_at` |
| `invitations` | `token` (32 random bytes, urlsafe base64) | `inviter_id`, `email` (lower-cased), `scope` (`fleet` or a JSON list of fingerprints), `created_at`, `expires_at` (7 days), `accepted_by` (null), `accepted_at` (null) |

Plus `schema_version` (single row) for forward migrations.

**Roles are derived, not stored on the account.** For a given account and machine:

- **owner** iff `machines.owner_id == account.id`;
- **tech** iff a `memberships` row exists;
- **platform admin** iff the account's email is in `ICE_CLOUD_ADMINS` (checked at request time, so editing the env and restarting is the whole admin-management procedure).

One account may be owner of some machines and tech on others. Suspension (`suspended_at` set) makes every request from that account behave as signed out.

**Fingerprint** = lower-case hex SHA-256 of the raw 32-byte public key. The **short form** shown to humans and typed into the claim form is its first 16 hex characters, grouped `xxxx-xxxx-xxxx-xxxx`. URLs carry the short form; the repository resolves short form to full fingerprint and refuses an ambiguous prefix (practically impossible, defensively coded).

**Single ownership is structural:** `owner_id` is one nullable column and a claim sets it in one transaction (§5). A fleet-scoped invitation expands to one membership row per machine the inviter owns **at acceptance time**; a machine bought later needs a fresh grant. That is the safe default: a tech's access never grows without the owner acting.

## 4. Machine identity and signed requests

### 4.1 Edge: `ice_edge.uplink.identity`

- `MachineIdentity.load_or_create(path) -> MachineIdentity`: reads `data/cloud_identity.json`; if absent, generates an Ed25519 keypair and writes the file atomically (tmp + rename + `fsync_dir`, mode 0600, like `data/access.json`). The private key never leaves this file. Regeneration never happens automatically; deleting the file is the only way to get a new identity (and the machine becomes a new, unclaimed machine on the cloud — documented).
- The file also holds the **pending claim-code notice** (§5.3), the one piece of uplink state that must survive a restart: `{"claim_code_hash", "expires_at", "acked": bool}` or null.
- Exposes `fingerprint`, `short_fingerprint`, `public_key_b64`, `sign(method, path, timestamp, body) -> str`.
- The edge calls `load_or_create` only when `cloud.enabled` is true, at uplink start. The path is `DATA_DIR / "cloud_identity.json"` (`ice_common.paths`).

### 4.2 Contracts: `ice_contracts.cloud.identity` and `ice_contracts.cloud.signing`

Both edge and cloud need the models and the signing helpers; `ice_contracts` is the only package both may import. This adds `cryptography` as a dependency of `ice_contracts`.

`signing.py` (pure functions, no I/O):

- `canonical_message(method, path, timestamp, body_bytes) -> bytes` = `"{METHOD}\n{path}\n{timestamp}\n{sha256_hex(body)}"` encoded UTF-8. `path` is the request path only (no scheme/host/query).
- `sign(private_key_bytes, message) -> str` (base64) and `verify(public_key_bytes, message, signature_b64) -> bool`.
- `fingerprint(public_key_bytes) -> str`, `short_fingerprint(fp) -> str`, `parse_short_fingerprint(text) -> str | None` (accepts with or without dashes, case-insensitive).
- `MAX_CLOCK_SKEW_SECONDS = 300`, `is_timestamp_fresh(timestamp_iso, now) -> bool`.

Headers (constants in `signing.py`): `X-Ice-Machine` (full fingerprint), `X-Ice-Timestamp` (ISO-8601 UTC, seconds), `X-Ice-Signature` (base64).

`identity.py` (Pydantic, `extra="forbid"`): `RegisterMachine{public_key_b64, label}`, `ClaimCodeNotice{claim_code_hash, expires_at}`, `RegisterResponse{fingerprint, claimed: bool}`. `CLOUD_CONTRACT_VERSION` bumps **0.1.0 → 0.2.0** (additive: new endpoints and headers; the report body is unchanged).

### 4.3 Cloud: verifying

`ice_cloud.machine_auth.require_machine` is a FastAPI dependency on every `/api/v1/machines/*` route. In order: all three headers present → timestamp fresh → fingerprint known (or, for `register` only, derived from the body's public key and allowed to be unknown) → signature verifies over the canonical message → return the `Machine` row, updating `last_seen_at`. Any failure is **401 with an empty body**. The existing `boot_id`/`seq` check in `SnapshotStore.save` already makes a replayed report a no-op.

### 4.4 Registration

`POST /api/v1/machines/register` body `RegisterMachine`, signed with the key whose public half is in the body. The cloud verifies that the header fingerprint equals `fingerprint(public_key)`. First call inserts an unclaimed `machines` row; a repeat with the same key updates `label` and `last_seen_at` only. Response `RegisterResponse` (200 both times).

The uplink loop (`ice_edge.uplink.loop.run_uplink`) calls register **before** the UI-bundle `PUT`, and retries it each interval until it gets 200; no report is sent until registered. An existing deployment therefore gains an identity on its next start with no manual step.

The report route becomes `POST /api/v1/machines/{fingerprint}/report`; the path fingerprint must equal the authenticated machine's, else 401. `machine_id` inside the report body stays the label. `SnapshotStore` keys files by fingerprint (one-line change; the `check_machine_id` regex already accepts hex).

Reports from an unclaimed machine are stored but visible only to a platform admin.

## 5. Claiming and transfer

### 5.1 Minting, at the machine only

A claim code is the edge's existing 8-digit code (`generate_code(CODE_DIGITS)`, scrypt hash via `hash_secret`, plaintext shown once, `TRANSFER_TTL_DAYS` = 7 days). It is minted on three occasions:

1. **First-run setup** — the setup code (`AccessStore.pending_setup_code`), as today.
2. **Ownership transfer** — the transfer code (`AccessStore.start_transfer`), as today.
3. **Link to cloud** — new: `POST /users/ownership/link` on the edge, gated on `manage_ownership` with the owner's PIN (same `_check_owner_pin` helper as transfer), mints a code with a 7-day expiry stored as `AccessStore.pending_link` (`{"code_hash", "expires_at"}`) and shows it once with the machine's short fingerprint. Covers a machine set up before the cloud existed, and a lost code. Cancel: `POST /users/ownership/link/cancel`.

The `/users/ownership` page and `/health` show the short fingerprint whenever `cloud.enabled`.

### 5.2 Which code the cloud accepts

Exactly one code is live at a time, in this priority: a pending transfer code, else a pending link code, else the pending setup code (unfinalised). `AccessStore.cloud_claim_code() -> tuple[hash, expires_at] | None` returns the live one; starting a transfer cancels a pending link code.

### 5.3 Telling the cloud

Whenever the live code changes (minted, cancelled, consumed, expired), the edge records the new notice in the identity file with `acked=false` and the uplink loop sends `POST /api/v1/machines/{fingerprint}/claim-code` with `ClaimCodeNotice`, or `DELETE` on the same path when there is no live code. On 2xx the notice is marked acked. Unacked notices are retried once per report interval, before the report. The cloud only ever sees the **hash**.

The edge learns about code changes through a `subscribe_claim_code_change(cb)` observer on `AccessStore` (the `ice_common.observers.Subscribers` pattern), wired by `ice_edge/__main__.py` when the uplink starts. `AccessStore` itself has no cloud knowledge.

### 5.4 Claiming on the cloud

`GET/POST /claim` (signed-in account): form fields `fingerprint` (short form) and `code`. Server order:

1. Back-off check keyed `("claim", short_fingerprint, client_ip)` using a port of the edge's `Backoff` class into `ice_cloud.backoff` (same exponential rules, no hard cap). On back-off: 429.
2. Resolve the machine; no machine, no live hash, or expired → 403 "no claim is open for this machine" (same message for all three; the back-off counter increments).
3. `verify_secret(code, claim_code_hash)` false → 403, counter increments.
4. Success, **one transaction**: `owner_id = account.id`, `claimed_at = now`, delete every `memberships` row for the machine, clear `claim_code_hash`/`claim_code_expires_at`. Redirect to `/m/{short}/`.

The old owner loses access the moment the new owner claims, matching the local swap in `AccessStore.complete_transfer`; techs are dropped because the new owner has not vouched for them.

The claim **does not** consume the code at the machine: the incoming owner still completes `/setup` locally with it. The two completions are independent in time and either order works. The cloud learns about the local transfer only through claim-code notices: `complete_transfer` moves the hash into the unfinalised `setup` record, so the live code is unchanged and the observer re-sends the same hash (idempotent); `finalize_setup` clears it and the observer sends the `DELETE`. Both transitions fire the observer.

A completed local transfer the cloud never heard about is not a special case: the new owner mints a Link-to-cloud code and claims.

### 5.5 Platform admin

`/admin`: every machine with owner email or "unclaimed", last seen, claim open or not; **Unbind** (owner → null, memberships deleted; two-tap confirm) and **Suspend/Unsuspend account**. Admin can never set an owner directly: ownership always passes through a code minted at the machine.

## 6. People: sign-in, sessions, invitations, portal

### 6.1 Google sign-in (`ice_cloud.auth`)

- `OidcProvider` protocol: `authorize_url(state, nonce, code_verifier) -> str`, `exchange(code, code_verifier) -> IdentityClaims{sub, email, email_verified, name}`.
- `GoogleProvider(client_id, client_secret, redirect_uri, http)`: fetches `https://accounts.google.com/.well-known/openid-configuration` once at start (cached, refreshed on JWKS miss), authorization-code flow with PKCE (S256) and `state`/`nonce` held in a short-lived signed cookie, exchanges the code with `httpx`, verifies the ID token's signature against the JWKS, `iss`, `aud`, `exp`, `nonce`, and requires `email_verified == true`. ID-token verification uses `cryptography` directly (RS256), no JWT library.
- Routes: `GET /login` (page with one button), `GET /auth/google/start`, `GET /auth/google/callback`, `POST /logout`.
- First sign-in creates the account from `sub`, `email`, `name`. A later sign-in with a known `sub` updates `email`/`name`.
- Tests use a `FakeProvider` that returns fixed claims; no network, no Google.

### 6.2 Sessions

A signed cookie `ice_cloud_session` = `base64(json{account_id, issued_at, last_active_at}) + "." + HMAC-SHA256(ICE_CLOUD_SESSION_KEY)`. `Secure` (unless `ICE_CLOUD_INSECURE_COOKIES=1` for local dev over http), `HttpOnly`, `SameSite=Lax`. Idle limit 30 days, absolute 90 days; `last_active_at` is refreshed at most once per hour to avoid a Set-Cookie on every poll. A suspended or deleted account → treated as signed out. CSRF: every mutating route requires the `HX-Request` header, the same guard the edge uses (`require_htmx`).

`current_account(request) -> Account | None` dependency; `require_signed_in` redirects to `/login?next=`; `require_machine_access(fingerprint, *, owner_only=False)` resolves the machine, checks owner/tech/admin, else 404 (not 403: a non-member must not learn the machine exists).

### 6.3 Portal routes

| Route | Who | Behaviour |
|---|---|---|
| `GET /` | signed-in | **My machines**: owned and tech-granted machines, each with label, short fingerprint, online/offline (existing 3× interval rule), fault count, your role. Empty state links to `/claim`. |
| `GET/POST /claim` | signed-in | §5.4 |
| `GET /m/{short}/`, `/status`, `/pill`, `/kpi`, `/health`, `/health/faults`, `/static/…` | owner, tech, admin | The existing machine pages, byte-for-byte what they render today, mounted under the prefix. Links and `hx-get` URLs inside the templates are prefixed through the existing `remote` context (`ice_ui` already renders from a base path for the cloud). Any other sub-path: the existing 404 "Not available remotely yet". |
| `GET /m/{short}/people` | owner | Memberships list with Revoke (two-tap), Invite form (email + scope: this machine / all my machines), pending invitations with Cancel, and a "Transfer ownership" panel that explains the code is minted at the machine (no action here). |
| `POST /m/{short}/people/invite`, `/people/{account_id}/revoke`, `/people/invitations/{token}/cancel` | owner | As named. Invite emails are **out of scope** for this spec (no mailer on the cloud yet); the owner is shown the invitation link to pass on. |
| `GET/POST /invite/{token}` | signed-in | Accept: the account's email must equal the invitation's email (lower-cased), token unexpired and unaccepted. Fleet scope expands to one membership per machine the inviter **currently** owns. Then redirect to `/`. |
| `GET /admin`, `POST /admin/machines/{short}/unbind`, `POST /admin/accounts/{id}/suspend` | platform admin | §5.5 |

The current `/fleet`, `/fleet/select/{machine_id}` and the selection cookie are removed; the fingerprint in the URL is the selection. `/` was the selected machine's home; it is now My machines, and the machine home moves to `/m/{short}/`.

Both owners and techs get `REMOTE_VIEWER_PERMISSIONS` on machine pages for now; the role is recorded so alert routing and remote commands can widen it later.

### 6.4 Bundle sandbox hardening

Uploaded UI bundles render in the existing `SandboxedEnvironment`, but the context passed to a bundle template loses `request` (and so `request.app.state`); the installed UI keeps the full context. A bundle from one owner's machine must not be able to reach the store or the registry. `tests/cloud/test_ui_registry.py` gains the test.

## 7. Storage

`ice_cloud.store` becomes a package: `store/__init__.py` re-exports; `store/snapshots.py` is today's `SnapshotStore` keyed by fingerprint; `store/repository.py` defines the `Repository` protocol (accounts, machines, memberships, invitations, claim; every method named for the use case, e.g. `claim_machine(fingerprint, account_id, now) -> bool` does §5.4 step 4 atomically, `machines_for_account(account_id) -> list[MachineAccess]`); `store/sqlite.py` is the only module that contains SQL, opening one connection per call with WAL and `synchronous=NORMAL` like the edge's `EventRecorder`, `schema_version` row, migrations as an ordered list of SQL scripts applied on start. Routes and services depend on `Repository`, never on `SqliteRepository`.

## 8. Configuration and deployment

Environment (all read in `ice_cloud/__main__.py` and passed into `create_app` as a `CloudSettings` dataclass, so tests construct it directly):

| Variable | Meaning | Default |
|---|---|---|
| `ICE_CLOUD_DATA` | data dir (`machines/`, `ui/`, `cloud.db`) | `./cloud_data` |
| `ICE_CLOUD_HOST` | bind host | `127.0.0.1` |
| `ICE_CLOUD_PUBLIC_URL` | external base URL, for the OIDC redirect URI and invitation links | required when Google is configured |
| `ICE_CLOUD_GOOGLE_CLIENT_ID`, `ICE_CLOUD_GOOGLE_CLIENT_SECRET` | Google OAuth client | unset → `/login` says sign-in is not configured; the machine API still works |
| `ICE_CLOUD_ADMINS` | comma-separated admin emails | empty |
| `ICE_CLOUD_SESSION_KEY` | ≥ 32 bytes, cookie HMAC key | required; refuse to start without it |
| `ICE_CLOUD_INSECURE_COOKIES` | `1` drops the `Secure` flag for http dev | unset |

`docker/docker-compose.cloud.yml` and `.env.example` gain these with placeholders; the compose file's "dev only — no auth" comment goes. The portal still binds loopback by default; HTTPS termination is a reverse proxy in front of it, documented in the cloud README, not coded.

Edge side: nothing new in `config.json`. `cloud.enabled` and `cloud.url` are enough; identity is in `data/`.

## 9. Error handling

- Machine API: any auth failure → 401, empty body, one `warning` log line with the fingerprint and reason. Oversized or invalid bodies keep their current 413/422.
- Claim: back-off before the hash check (429), then one 403 message for every failure mode so the form cannot be used to discover machines.
- Non-member on a machine URL → 404.
- Edge: every cloud failure is a logged warning and never touches a sale (unchanged). The claim-code notice is the one retried-until-acked message, once per report interval; a notice superseded by a newer one is dropped, never sent out of order (the identity file holds only the latest).
- Cloud start: missing `ICE_CLOUD_SESSION_KEY` or a short one → exit 1 with a clear message. Google unconfigured is not an error.

## 10. Testing

- `tests/contracts/test_cloud_signing.py`: sign/verify round trip, tampered body/path/method/timestamp each fail, skew window edges, fingerprint and short-form parsing.
- `tests/cloud/test_machine_auth.py`: each header missing, stale timestamp, unknown fingerprint, bad signature, wrong path fingerprint → 401; valid → 204 and `last_seen_at` moves.
- `tests/cloud/test_register.py`: first register inserts unclaimed; repeat updates label only; header/body fingerprint mismatch → 401.
- `tests/cloud/test_repository.py` (temp SQLite): every `Repository` method, `claim_machine` atomicity (memberships gone, owner set, hash cleared), short-fingerprint resolution, migrations idempotent.
- `tests/cloud/test_auth.py`: `FakeProvider` sign-in creates/updates the account, `email_verified=false` refused, bad `state`/`nonce` refused, session cookie tamper → signed out, suspended → signed out, idle/absolute expiry.
- `tests/cloud/test_claim.py`: wrong code 403 and back-off 429, expired code, success swaps owner and drops techs, old owner then gets 404, local-order independence (claim before or after the `DELETE` notice).
- `tests/cloud/test_people.py`: invite, accept with matching/mismatching email, fleet scope expands only to machines owned at acceptance, revoke, cancel, non-owner 404.
- `tests/cloud/test_admin.py`: allowlist, unbind, suspend; non-admin 404.
- `tests/cloud/test_ui_parity.py`: unchanged assertions, now against `/m/{short}/…` with a claimed machine fixture.
- `tests/cloud/test_ui_registry.py`: bundle context has no `request`.
- `tests/edge/uplink/test_identity.py`: file created once (mode 0600 on POSIX), never regenerated, fingerprint stable across loads; claim-code notice persisted and `acked` flips only on 2xx; superseded notice replaces, never queues.
- `tests/edge/uplink/test_loop.py`: register precedes bundle and report; no report until registered; notice retried each interval.
- `tests/edge/access/`: `cloud_claim_code()` priority order; `pending_link` mint/cancel/expiry; observer fires on every live-code transition including `complete_transfer` and `finalize_setup`.
- `tests/edge/web/test_users_ownership.py`: Link-to-cloud button gated on `manage_ownership` and PIN, shows code and short fingerprint once with `Cache-Control: no-store`.
- Existing guards stay: import-linter (`ice_cloud` never imports `ice_edge`), `test_routes_shape`, the private-access ratchet, `test_cloud_static_css`.

## 11. Open items

- Invitation and claim emails from the cloud (needs a cloud mailer; the alerts spec will add one).
- Rate limiting on `/login` and the machine API beyond the claim back-off (reverse-proxy concern for now).
- Key rotation for a machine (delete the file and re-claim is the documented procedure; a signed "rotate" command belongs with remote commands).
