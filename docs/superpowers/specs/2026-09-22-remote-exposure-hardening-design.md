# Remote Exposure Hardening — Design

**Date:** 2026-09-22
**Status:** Approved (brainstormed with owner)
**Roadmap:** answers the §10 open questions on site connectivity and remote access; follows
`docs/superpowers/specs/2026-09-21-unattended-operation-design.md`.

## Goal

The simulation host is reachable from the internet through Traefik at
`ice.hpz440.ohr3023.org`, and the real machine will be too. Nothing on that
path may let a stranger publish MQTT commands, guess the dashboard login, or
replay a logged-in browser's credentials against the control endpoints.

## Findings being fixed (2026-09-21 review)

- Mosquitto is published on host port 1883 with `allow_anonymous true` in the
  Traefik-fronted compose file. Anyone who reaches the port can publish
  `cmd/*` to the VMC.
- The dashboard is HTTP Basic auth only, with no failed-login limit and no
  CSRF guard. Browsers replay Basic credentials on cross-site POSTs, so a
  hostile page can hit `/action/*`, `/inventory/delete/*`, `/faults/*/clear`.
- The weak-password warning fires only for the literal `changeme`. The sim
  host's `data/config.json` has no `web` section and therefore runs the
  default `admin`/`changeme` behind the public hostname.
- TLS could not be confirmed from the repo. Confirmed on the host on
  2026-09-22: Traefik v3.3 terminates TLS on `websecure` with a Cloudflare
  DNS-challenge wildcard for `*.hpz440.ohr3023.org` and redirects `web` to
  HTTPS. Only explicit router labels are missing.

## Decisions

1. **Port 1883 stays published on the LAN only; authentication is
   required.** Home Assistant on the LAN and the e2e tests use it. One broker
   user is shared by the VMC, the simulators and Home Assistant. The trust
   boundary is the LAN: the router forwards only 80/443 to Traefik, never
   1883, and the compose file binds the listener to a configurable host
   address (`MQTT_BIND_ADDR`, set to the LAN interface on hpz440) rather
   than all interfaces. MQTT over TLS on 1883 is deferred: the credential
   only ever crosses the LAN, and adding certificates to Home Assistant and
   the simulators buys nothing until a broker leaves the LAN.
2. **Credentials come from a gitignored `.env`.** A committed `.env.example`
   documents the variables. A one-shot init container generates the mosquitto
   password file; nothing secret is committed.
3. **Basic auth stays.** Three guards make it safe enough for a single-owner
   machine: a password policy enforced at startup, an HTMX header requirement
   on every mutating route, and a failed-login limiter. A cookie login page
   is not worth the churn now.
4. **Fail closed at startup.** A non-loopback dashboard with a weak password
   exits with an explanatory error; an explicit env flag re-enables it for
   the simulation host only.

## 1. Broker authentication

### Files

- `.env.example` (new, committed):

  ```
  # Copy to .env (gitignored). Shared by the broker init, the VMC, the
  # simulators and Home Assistant.
  MQTT_USERNAME=vmc
  MQTT_PASSWORD=change-me
  # Host address the broker listens on. Use the LAN interface address so
  # 1883 is never reachable through a stray port-forward or a second NIC.
  MQTT_BIND_ADDR=0.0.0.0
  # Docker network(s) Traefik reaches the VMC from; X-Forwarded-For is
  # trusted only from these (comma-separated CIDRs).
  ICE_COLDER_TRUSTED_PROXIES=
  ```

  On hpz440: `MQTT_BIND_ADDR=192.168.86.26`,
  `ICE_COLDER_TRUSTED_PROXIES=172.25.0.0/16` (the `harbor` network).

- `.gitignore`: add `.env`.
- `docker-compose.yml`:
  - new service `mosquitto-init`: image `eclipse-mosquitto:2`, mounts
    `./docker/mosquitto/config:/mosquitto/config`, command
    `sh -c 'mosquitto_passwd -c -b /mosquitto/config/passwd "$MQTT_USERNAME" "$MQTT_PASSWORD" && chmod 600 /mosquitto/config/passwd'`,
    `restart: "no"`, environment from `.env` with
    `MQTT_PASSWORD: ${MQTT_PASSWORD:?set MQTT_PASSWORD in .env}` so a missing
    file fails `docker compose up` with a readable message.
  - `mosquitto`: `depends_on: mosquitto-init: condition: service_completed_successfully`;
    mount `./docker/mosquitto/config/mosquitto-prod.conf:/mosquitto/config/mosquitto.conf`
    and the config directory for `passwd`; healthcheck uses
    `mosquitto_sub -u "$MQTT_USERNAME" -P "$MQTT_PASSWORD" ...`.
  - `mosquitto` `ports`: `"${MQTT_BIND_ADDR:-0.0.0.0}:1883:1883"`.
  - `vmc`, `sim-*`: `environment` gains `MQTT_USERNAME=${MQTT_USERNAME}` and
    `MQTT_PASSWORD=${MQTT_PASSWORD}`; `vmc` also gets
    `ICE_COLDER_TRUSTED_PROXIES=${ICE_COLDER_TRUSTED_PROXIES:-}`.
  - Watchtower must not restart `mosquitto-init`; it carries no watchtower
    label.
- `docker/docker-compose.prod.yml`: same init service and env wiring.
- `docker/mosquitto/config/mosquitto-prod.conf`: unchanged
  (`allow_anonymous false`, `password_file /mosquitto/config/passwd`).
- `docker/mosquitto/config/.gitignore`: `passwd`.
- `docker/mosquitto/config/mosquitto.conf` and `docker/docker-compose.yml`
  keep anonymous access for local development only; a header comment says so.

## 2. Clients read credentials

- `main.py`: after the `MQTT_BROKER_HOST` override, apply `MQTT_USERNAME`
  and `MQTT_PASSWORD` (wrapped in `SecretStr`) to `live_config.mqtt` when
  set. Log the username, never the password.
- `simulators/base.py`: `ESP32Simulator.__init__` accepts
  `username: str | None = None, password: str | None = None` (plain
  strings); `entry_point` fills them from the loaded config's
  `mqtt.username` and `mqtt.password.get_secret_value()` (the field is a
  `SecretStr`; passing it raw would send the masked value), overridden by
  the same env vars; `run()` passes them to `aiomqtt.Client`.
- `tests/test_integration_e2e.py`: `_check_broker` and every client use
  `MQTT_USERNAME` / `MQTT_PASSWORD` from the environment when present.

## 3. Traefik labels

`docker-compose.yml` `vmc` labels gain
`traefik.http.routers.ice.entrypoints=websecure` and
`traefik.http.routers.ice.tls=true`.

## 4. Dashboard password policy

- `services/auth_policy.py` (new):

  ```python
  WEAK_PASSWORDS = {"changeme", "admin", "password", "ice-colder"}
  MIN_PASSWORD_LENGTH = 12

  def password_problem(password: str) -> str | None:
      """Why this admin password must not face a network, or None."""

  def generate_admin_password() -> str:  # secrets.token_urlsafe(15)
  ```

- `main.py` first run (`_create_default_config`): set
  `web.admin_password` to `generate_admin_password()` before saving; log once
  at WARNING: `First run: dashboard login is admin / <password> — change it
  in config.json`.
- `main.py` startup check (replaces the `changeme` warning): if
  `web.host` is not `127.0.0.1`/`localhost`/`::1` and
  `password_problem(...)` is not None and `ICE_COLDER_ALLOW_WEAK_PASSWORD`
  is not `"1"`, log an error naming the problem and the two remedies and
  `sys.exit(1)`. With the flag set, log a warning instead.
- Neither compose file sets `ICE_COLDER_ALLOW_WEAK_PASSWORD`; the root
  compose is the internet-exposed stack, so committing the bypass there would
  void the policy. The flag exists only for a local shell or an uncommitted
  `docker-compose.override.yml`. Consequence for hpz440: its
  `data/config.json` has no `web` section and the VMC will refuse to start
  on the new image until one with a strong password is added, so that edit
  is made before merging (see §7).

## 5. Dashboard CSRF guard

- `web_interface/routes.py`: `require_htmx(request)` dependency raises 403
  `"HTMX request required"` unless `request.headers.get("HX-Request") == "true"`.
  Applied to every `@router.post(...)` via `dependencies=[Depends(require_htmx)]`.
  The dashboard's own HTMX calls already carry the header; a cross-site form
  cannot add it, and a cross-origin `fetch` with a custom header triggers a
  CORS preflight the app never answers.
- `tests/test_web_routes.py`: the `client` fixture gains
  `c.headers["HX-Request"] = "true"`; one test per POST route asserts 403
  without the header.

## 6. Failed-login limiter

- `web_interface/auth.py` (new):

  ```python
  class LoginLimiter:
      def __init__(self, max_failures: int = 10, window_seconds: float = 900.0,
                   lockout_seconds: float = 900.0, clock=time.monotonic): ...
      def client_ip(self, request) -> str   # see below
      def check(self, ip: str) -> float | None   # seconds remaining if locked, else None
      def record_failure(self, ip: str) -> None
      def record_success(self, ip: str) -> None  # clears the entry
  ```

  Failures older than `window_seconds` are pruned on every call; a lockout
  starts when the pruned count reaches `max_failures` and lasts
  `lockout_seconds`. State is per process and in memory; a restart clears it,
  which is acceptable for a single machine.

  `client_ip` keys on `request.client.host` unless that peer address falls
  inside one of `WebConfig.trusted_proxies` (a list of CIDRs, default empty,
  overridable by the `ICE_COLDER_TRUSTED_PROXIES` env var). Only then is the
  **rightmost** `X-Forwarded-For` entry used: that is the hop the trusted
  proxy appended. Traefik on hpz440 runs without `forwardedHeaders.trustedIPs`,
  so it discards any `X-Forwarded-For` a client supplies and sends a single
  real address; the rightmost rule stays correct even if a proxy were later
  configured to preserve client headers. With the list empty every request
  is keyed on the peer, which behind a proxy collapses to one shared bucket:
  a remote attacker could then lock the owner out of the dashboard (not the
  machine), which is why the compose file sets the list.
- `web_interface/routes.py`: module-level `login_limiter = LoginLimiter()`;
  `require_auth` gets `request: Request`, calls `check` first and raises 429
  with `Retry-After`, records failure or success after the comparison.

## 7. Docs and deployment

- `README.md`: `.env` setup, the password rule and the
  `ICE_COLDER_ALLOW_WEAK_PASSWORD` flag, how to give Home Assistant the
  broker user.
- `CLAUDE.md`: the env overrides, `services/auth_policy.py`,
  `web_interface/auth.py`, the HTMX header requirement on POST routes.
- `ROADMAP.md` §10: answer the two remote-access bullets (Traefik TLS, Basic
  auth with limiter and CSRF guard, authenticated broker; VPN not required
  for the owner; technicians get the same login until roles exist).
- Before merge, on hpz440 (the new image refuses to start otherwise):
  create `.env` with the values in §1 and add a `web` section with a strong
  password to `data/config.json`. After merge: `git pull`,
  `docker compose up -d`, update Home Assistant's MQTT integration, and
  confirm from another LAN host that 1883 answers on 192.168.86.26 only and
  that the router forwards nothing but 80/443.

## Non-goals

Cookie login and roles; read-only broker ACL for Home Assistant; TLS on
1883; changes to the Traefik instance itself; secret scanning of `.env`.

## Testing

- `tests/test_auth_policy.py`: each weak value rejected, short rejected, a
  strong one accepted, generated password length and charset.
- `tests/test_main_startup.py` (or extend `test_first_run.py`): first run
  writes a generated password and logs it once; weak password on
  `0.0.0.0` exits 1; loopback host passes; flag downgrades to warning;
  `MQTT_USERNAME`/`MQTT_PASSWORD` land in `config.mqtt`.
- `tests/test_login_limiter.py`: lockout after N failures inside the window,
  expiry after `lockout_seconds`, reset on success, window pruning,
  forwarded-IP parsing.
- `tests/test_web_routes.py`: 429 after repeated bad logins; 403 on every
  POST without `HX-Request`; existing tests pass with the header.
- `tests/test_simulator_base.py`: credentials reach `aiomqtt.Client`
  (monkeypatched factory captures kwargs); env override wins over config.
- CI: a `compose-config` step runs
  `docker compose --env-file .env.example -f docker-compose.yml config -q`
  and
  `docker compose --env-file .env.example -f docker/docker-compose.prod.yml config -q`
  so both stacks are parsed and interpolated.
