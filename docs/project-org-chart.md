# Project org chart: the human roles in ice-colder

Who the people around a machine are, which system knows about them, and exactly
what each one may do. There is an open-ended number of people but a finite set of
roles, and one real person can hold several at once. Sources of truth are cited so
this page can be checked against the code; when they disagree, the code wins.

## 1. Overview

Three independent trust domains exist, and nothing links an identity in one to an
identity in another except a one-time code:

| Domain | Who logs in, and how | Roles | Source of truth |
|---|---|---|---|
| **Edge** (one machine's dashboard on port 26123) | Named users with a 4–8 digit PIN, second factor on a new device | owner, secretary, tech, loader | `packages/contracts/src/ice_contracts/access.py`, `packages/edge/src/ice_edge/access/access.py` |
| **Cloud** (fleet portal, `vmc.ohr3023.org`) | Google sign-in (OIDC) | platform admin, machine owner, cloud tech | `packages/cloud/src/ice_cloud/auth.py`, `people.py`, `admin.py` |
| **Host / ops** (shell, Docker, `.env`) | SSH to the machine host or the cloud droplet | operator | `.env.example`, `docs/deploying-the-cloud-server.md` |

Plus two kinds of people neither app has an account for (customer, bench
technician) and one non-human principal (the machine's own Ed25519 key), covered in
§5 and §6.

```
Host / ops operator ──── sets env, keys, SMTP, backups; cannot be revoked by either app
        │
        ├─ Cloud (Google accounts)
        │     platform admin   ── every machine read-only, unbind, suspend
        │     machine owner    ── one per machine, claimed with a code minted at the edge
        │     cloud tech       ── invited by the owner
        │
        └─ Edge (PIN accounts, one machine each)
              owner            ── one per machine, all 13 permissions
              secretary        ── catalog, inventory, reports, contacts, users
              tech             ── health, logs, controls, tests, inventory
              loader           ── inventory restock, cash collection

Outside both:  customer (buttons and /screen) · bench tech (flash, provision)
Non-human:     the machine (registers, reports, uploads its UI bundle)
```

## 2. Edge roles

The edge's model is `Role` × `Permission` → `ROLE_PERMISSIONS`
(`packages/contracts/src/ice_contracts/access.py`). Thirteen permissions, four roles,
exactly one owner per machine (`AccessStore.create_user` raises `OwnerExistsError`
on a second). Roles are assigned by hand on `/users`.

### 2.1 Permission matrix

| Permission | owner | secretary | tech | loader |
|---|:-:|:-:|:-:|:-:|
| `view_status` | ✓ | ✓ | ✓ | ✓ |
| `collect_cash` | ✓ | ✓ | ✓ | ✓ |
| `edit_placement` | ✓ | ✓ | ✓ | ✓ |
| `edit_catalog` | ✓ | ✓ | | |
| `view_reports` | ✓ | ✓ | | |
| `edit_contacts` | ✓ | ✓ | | |
| `manage_users` | ✓ | ✓ | | |
| `clear_faults` | ✓ | | ✓ | |
| `view_logs` | ✓ | | ✓ | |
| `machine_controls` | ✓ | | ✓ | |
| `run_tests` | ✓ | | ✓ | |
| `edit_secrets` | ✓ | | | |
| `manage_ownership` | ✓ | | | |

### 2.2 What each permission unlocks

Routes from `ice_edge.web.routes`; the full gate table is in `CLAUDE.md`. "A or B"
means either permission admits the page.

| Permission | Surfaces |
|---|---|
| `view_status` | Home `/`, `/health`, `/health/subsystems[/{name}]`, `/health/faults` (read), `/health/availability`; the `/status`, `/kpi`, `/pill` fragments |
| `collect_cash` | `POST /inventory/collect` (record a cash pull) |
| `edit_placement` | `/inventory` and the ±1/±10 restock buttons, `/products/{sku}/placement`; `/products` and `/products/{sku}` (or `edit_catalog`) |
| `edit_catalog` | `/products/new`, `/products/{sku}/catalog`, `/products/{sku}/copy`, product delete |
| `view_reports` | `/reports`, `/reports/period`, `/reports/product[/{sku}]`, `/reports/method`, `/reports/collections`, CSV download |
| `edit_contacts` | `/settings/machine`, `/settings/contacts`, `/settings/reports`; `/settings` (or `edit_secrets`) |
| `manage_users` | `/users`, `/users/new`, `/users/{id}`, `/devices` (trusted devices) |
| `clear_faults` | Clear on `/health/faults`; PAY-104 "Record as sale" / "Discard" |
| `view_logs` | `/health/logs` (last 50 lines of `vmc.log`) |
| `machine_controls` | `/controls` (restart, reset, shutdown) |
| `run_tests` | `/tests/*`: take the maintenance lease (opportunistic or standby), run command tests, run a test sale, record verdicts, take over or end a lease |
| `edit_secrets` | `/settings/payments`, `/settings/comms`, `/settings/mqtt`, `/settings/web` |
| `manage_ownership` | `/users/codes` (emergency codes), `/users/ownership` (transfer, cloud claim code, email report); any write whose **target** is the owner's own account |

### 2.3 The four roles

**Owner** — the person whose machine it is. Holds all thirteen permissions, and is
the only role with `edit_secrets` and `manage_ownership`. Created by whoever
completes the setup wizard (§5.2) or an ownership transfer. Guards that apply only
to the owner (`routes/users/accounts.py`):

- cannot disable or delete their own account (`_guard_owner_self_lockout`);
- cannot change their own role away from owner (`_guard_owner_self_demotion`);
- the only way out is `start_transfer` → the incoming person finishes the wizard →
  `complete_transfer` swaps owners in one write and drops the old owner's device
  trust. The outgoing owner stays in control until that moment.

**Secretary** — the owner's administrative delegate. Products, placement,
inventory, reports, machine/contact/report settings, and user management for every
account except the owner's (`_guard_owner_target` returns 403 on any write
targeting the owner). Can create users of any role except owner. No secrets, no
controls, no tests, no fault clearing, no logs.

**Tech** — the service person. Everything under Health including logs and fault
clearing, Controls, and the whole Tests level: a tech can take the machine out of
service (`SVC-102`, payment withdrawn machine-wide), run board commands from the
`TESTABLE_COMMANDS` allowlist, and run a test sale that records no sale. Also
inventory and cash collection. No catalog, reports, settings or users.

**Loader** — the restocker. Home and Health read-only, `/inventory` with the
restock buttons, and cash collection. Nothing else renders for them.

### 2.4 Device trust and second factor

Every edge user logs in with name + PIN. On a device the store has not trusted for
that user, a second factor is required: a 6-digit OTP emailed to the user when
`communication.email_gateway` is configured, or an 8-digit emergency code, which
works offline. Emergency codes are minted, counted and regenerated only on
`/users/codes` (`manage_ownership`), so the owner controls who can enrol a new
device. `/devices` (`manage_users`) lists and revokes trusted devices. Back-off is
per `(kind, subject, client)` with a per-user budget for untrusted clients; there
are no hard lockouts.

Decision of 2026-10-21: all outbound human communication is to move to the cloud
server, so the emailed OTP becomes a cloud delivery and an unclaimed or offline
machine's first-device login relies on emergency codes alone.

## 3. Cloud roles

The cloud has no role table. `Role = Literal["owner", "tech", "admin"]`
(`ice_cloud/auth.py:66`) is **derived** per request from three facts: is the
account's email in `ICE_CLOUD_ADMINS`, is it `machines.owner_id`, is there a
membership row. Nothing is assignable from a form except tech membership.

| Role | How it is obtained | Scope |
|---|---|---|
| **Platform admin** | email listed in the `ICE_CLOUD_ADMINS` env var | every machine |
| **Machine owner** | redeemed that machine's claim code at `/claim` | one machine per claim |
| **Cloud tech** | accepted an owner's invite link (`/invite/{token}`) | the machines the invite named (machine- or fleet-scope) |

### 3.1 Platform admin

- `/admin`: unbind any machine (clears owner, techs, alert state, consumed claim
  hashes, so the next claim code wins), suspend or unsuspend any account except
  their own, see SMTP status and send a test email.
- `/fleet` lists every machine; machines they do not own appear with role `admin`
  (`portal.py:156`).
- `require_machine_access()` admits an admin on any machine as role `admin`
  (`auth.py:243`), so the read-only portal (`/`, `/status`, `/pill`, `/kpi`,
  `/health`, `/health/faults`) and `/machines/{short}/alerts` are open on every
  machine, including other owners' sales KPIs and every recipient's email.
- Cannot: invite or revoke techs or mute another person on a machine they do not
  own (`people_page` and the invite/revoke/cancel routes are `owner_only`), set an
  owner directly, or reach any edge dashboard. There is no audit row for admin
  views; see §7.

### 3.2 Machine owner

- Read-only portal for the machine, rendered from the machine's own `ice_ui`
  templates with `REMOTE_VIEWER_PERMISSIONS` (= `view_status` only).
- `/machines/{short}/people`: invite techs (link shown, not emailed; machine- or
  fleet-scope), revoke them, cancel pending invites, see every alert recipient and
  mute any of them.
- `/machines/{short}/alerts`: full delivery history.
- Receives every alert event: `raised` for all three gates (`safety`,
  `fulfillment`, `alert`), `cleared`, `offline`, `online`
  (`alerts/router.py`).
- Re-claiming your own machine keeps your techs; a claim by a different Google
  account replaces the owner and deletes all tech memberships, alert state and
  prefs.

### 3.3 Cloud tech

- Same read-only portal as the owner.
- `/machines/{short}/alerts` shows only their own delivery rows; the mute route
  lets a tech mute only themself (`people.py:150`).
- Receives `safety` and `fulfillment` raises (never `alert`-gate ones), the
  matching clears, and offline/online.
- Membership is deleted when the machine's owner changes.

A signed-in account with no membership and no admin flag gets **404** on any
machine, never 403. Suspended accounts cannot sign in.

## 4. Host / ops operator

The person with a shell on the machine host (`hpz440` for the simulated fleet) or
the cloud droplet. This is the real top tier: neither app can grant or revoke it,
and everything below depends on what it sets.

- Cloud: `ICE_CLOUD_ADMINS` (who is platform admin), `ICE_CLOUD_SESSION_KEY`,
  Google OIDC client id/secret, `ICE_CLOUD_SMTP_*`, `ICE_CLOUD_TRUSTED_UI_DIGESTS`,
  the `cloud.db` file and its backups, Caddy/TLS, Watchtower updates.
- Edge host: `MQTT_USERNAME`/`MQTT_PASSWORD`, `ICE_COLDER_CONFIG` and
  `ICE_COLDER_DISPENSERS` paths, the bind-mounted `data/` directory (`config.json`,
  `access.json`, `cloud_identity.json`, `events.db`), backups. Deleting
  `access.json` puts the machine back in setup mode; deleting
  `cloud_identity.json` makes it a new unclaimed machine at the cloud.
- Can read secrets at rest (PIN hashes are scrypt; config secrets are plaintext in
  `config.json`). This is the floor any confidentiality design has to accept.

## 5. People without an account

### 5.1 Customer

No login. Interacts through the physical buttons and the customer screen
(`/screen`, the one page that keeps CDN references). Can insert money, select a
product, be dispensed to, and be refunded. Sees availability messages derived from
`Availability`. Nothing else is reachable.

### 5.2 Setup-code holder

Whoever stands at the machine while it is in **setup mode** (no owner yet) or
during an **ownership transfer**. The credential is the code shown only at the
machine (`begin_setup` / `start_transfer`), not an account; completing the wizard
makes that person the owner (§2.3). Every other route redirects to `/setup` while
setup mode is on.

### 5.3 Emergency-code holder

Anyone the owner has handed one of the 8-digit emergency codes. The code is a
second factor for enrolling a new device for an existing user; it does not create
a user or grant a role. Codes are single-use and the count is shown on
`/users/codes`.

### 5.4 Claim-code holder

Whoever receives the cloud claim code (setup code, transfer code, or the 7-day code
from `/users/ownership` → "Link to cloud") and redeems it at `/claim` becomes the
cloud **owner** (§3.2). The edge keeps only the scrypt hash; a consumed hash can
never be reused on that machine. Claim on the cloud before pressing Done at the
machine.

### 5.5 Bench / install technician

The person who flashes and provisions the ESP32 boards
(`scripts/flash_board.py`, `firmware/docs/*-board-interface.md`), wires the KC868
and MDB carrier, and runs the opt-in conformance suites. Needs physical access and
a workstation checkout; neither app knows them. Usually the same human as an edge
tech, but the capability is distinct: a bench tech can change what the board
reports, which no dashboard role can.

## 6. The machine as a principal

Not a human, but it holds the only credential that can mint an owner. The edge
generates an Ed25519 key in `data/cloud_identity.json` when `cloud.enabled`; its
SHA-256 fingerprint is the machine's identity everywhere (never `machine_id`,
which is a label). With it the machine may: register, send or delete a claim-code
notice, `PUT` its `ice_ui` bundle (only once claimed, at most 4 new digests per 30
days), and `POST` reports. It cannot read anything back except the
`alerts_handled` flag.

## 7. One person, several hats

The project's founder today is platform admin (env var), cloud owner of their own
machines (claim), and edge owner or tech by PIN on each machine: three separate
credentials, none implying another.

| Hat | Grants | Does not grant |
|---|---|---|
| Platform admin | read-only view of every machine and its alerts; unbind; suspend | any edge right; owner rights (invite/revoke/mute others) on a machine someone else claimed |
| Cloud owner | people page, alert prefs, full alert feed | anything on the edge |
| Edge owner | everything on that one machine | anything on the cloud, any other machine |
| Host operator | every env var and file | nothing inside the apps' own access checks except by editing those files |

Known gaps, recorded 2026-10-21 and deliberately left open:

- Admin reads of another owner's revenue KPIs have no consent step and no audit
  row. Before a second, unrelated owner joins, add an audit log the owner can see
  or drop money fields from `MachineReport` (owner opt-in). Cryptographic secrecy
  was considered and deferred.
- The only tool for a lost cloud owner is unbind + reclaim, which deletes the
  machine's techs and alert history.
- Platform admin gives no edge reach by design: if the admin needs in, the owner
  issues a tech PIN.

## 8. Where to look

| Question | File |
|---|---|
| Edge roles and permissions | `packages/contracts/src/ice_contracts/access.py` |
| Edge users, PINs, devices, codes, setup, transfer, claim code | `packages/edge/src/ice_edge/access/access.py` |
| Owner-targeting guards | `packages/edge/src/ice_edge/web/routes/users/accounts.py` |
| Route → permission table | `CLAUDE.md`, "Web Dashboard" |
| Cloud role derivation | `packages/cloud/src/ice_cloud/auth.py` (`require_machine_access`, `is_admin`) |
| Cloud people page, invites, mute | `packages/cloud/src/ice_cloud/people.py` |
| Cloud admin page | `packages/cloud/src/ice_cloud/admin.py` |
| Alert routing by role | `packages/cloud/src/ice_cloud/alerts/router.py` |
| Identity and claims design | `docs/superpowers/specs/2026-10-19-cloud-identity-tenancy-design.md` |
| Alert forwarding design | `docs/superpowers/specs/2026-10-19-cloud-alert-forwarding-design.md` |
