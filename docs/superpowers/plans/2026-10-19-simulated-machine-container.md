# Simulated Machine Container Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One Docker image that runs a whole simulated vending machine (VMC, three board sims, private mosquitto) under a Python launcher, env-configured so `docker compose up --scale machine=N` stands up N machines reporting to a remote cloud.

**Architecture:** A stdlib-only launcher at `docker/machine/launcher.py` seeds `/app/data/config.json` and `dispensers.toml` from env, starts mosquitto on loopback, then starts and supervises `ice_sims.vending_machine`, `ice_sims.ice_maker`, `ice_sims.mdb_gateway` and `ice_edge` as subprocesses. The fleet Dockerfile layers mosquitto and the launcher on the published edge image. A compose file with one scalable service and an anonymous `/app/data` volume gives each replica its own identity.

**Tech Stack:** Python 3.12 stdlib (`asyncio`, `signal`, `socket`), `ice_common.config_model.ConfigModel`, `ice_edge.config.config_store.save_config`, `ice_edge.vend.dispensers_doc.render_example`, eclipse mosquitto (Debian package), Docker Compose, pytest.

Spec: `docs/superpowers/specs/2026-10-19-simulated-machine-container-design.md`. Section numbers (§) refer to it.

## Global Constraints

- Plan style (user rule): tasks specify interfaces and tests, not full code bodies. Implementers write the code.
- The launcher lives at `docker/machine/launcher.py`, **outside `packages/`**; it may import `ice_common` and `ice_edge` (both in the base image) but nothing under `packages/` may import it. Tests load it by file path (§6).
- The launcher never imports `loguru`; it logs with `print("[launcher] ...", flush=True)` (§4.3).
- `uv run pytest`, `uv run ruff check --fix .` then `uv run ruff format .` before every commit. Always `uv run ruff`, never the global ruff. `ruff` is configured in `pyproject.toml` with no exclude, so `docker/machine/launcher.py` is linted too.
- Do not chain shell commands with `&&`; separate calls.
- Commit trailer, exactly: `Assisted-by: <model> (Claude Code)`; no `Co-Authored-By`.
- Private-access ratchet (`tests/meta/test_no_private_access.py`): new tests reach no `_private` attribute. `tests/docker/` is a new directory with an implicit budget of 0.
- Docker commands run through the `hpz440` context (the default); never switch context or start Docker Desktop.
- Only `.github/workflows/ci.yml` is an existing file this plan edits (Task 4, last, own commit). Everything else is new.
- No `TBD`/`TODO` in delivered code or docs.

## File map

| Path | Responsibility |
|---|---|
| `docker/machine/launcher.py` (new) | `LauncherError`; `Settings.from_env`; `seed`; `wait_for_port`; `Supervisor`; `main` |
| `docker/machine/mosquitto.conf` (new) | loopback anonymous listener, no persistence |
| `docker/machine/Dockerfile` (new) | `ARG BASE_IMAGE`; installs mosquitto; copies launcher and conf to `/app/machine/` |
| `docker/machine/README.md` (new) | how to run N machines against a remote cloud, read claim codes, what a restart keeps |
| `docker/docker-compose.fleet.yml` (new) | one scalable `machine` service (§5) |
| `tests/docker/__init__.py`, `tests/docker/test_machine_launcher.py` (new) | seeding and supervision unit tests |
| `.github/workflows/ci.yml` | one smoke step in the `image` job (§7) |

## Dependency graph (for parallel dispatch)

```
Task 1 (seed)  ──►  Task 2 (supervise, same file)  ──┐
Task 3 (Dockerfile, conf, compose, README) ──────────┴──►  Task 4 (CI step)
```

Tasks 1 and 3 are independent and run in parallel. Task 2 edits the file Task 1 created. Task 4 needs the image from Task 3 and the finished launcher from Task 2.

---

### Task 1: Launcher settings and seeding

**Files:**
- Create: `docker/machine/launcher.py`
- Create: `tests/docker/__init__.py` (empty)
- Create: `tests/docker/test_machine_launcher.py`

**Interfaces (produces):**

```python
# docker/machine/launcher.py
class LauncherError(Exception):
    """A configuration problem the operator must fix; main() prints it and exits 1."""

@dataclass(frozen=True)
class Settings:
    cloud_url: str
    machine_id: str
    report_interval: int          # 10..3600
    data_dir: Path

    @classmethod
    def from_env(cls, env: Mapping[str, str], *, hostname: str) -> "Settings":
        """§4.1. ICE_CLOUD_URL required (missing/empty -> LauncherError naming it);
        ICE_COLDER_MACHINE_ID default hostname; ICE_COLDER_REPORT_INTERVAL default 60,
        non-integer or outside 10..3600 -> LauncherError; ICE_COLDER_DATA default /app/data."""

DEFAULT_PRODUCTS: tuple[dict, ...]   # the three §4.2 rows: sku, name, slot, kind, button, price

def seed(settings: Settings) -> ConfigModel:
    """§4.2. Creates data_dir if missing. config.json a directory or unparsable ->
    LauncherError. Missing -> ConfigModel() defaults + DEFAULT_PRODUCTS. Missing
    dispensers.toml -> render_example(). Always re-applies machine_id, cloud.enabled=True,
    cloud.url, cloud.report_interval_seconds, mqtt.broker_host='127.0.0.1',
    mqtt.username=None, mqtt.password=None, then save_config(config, data_dir/'config.json').
    Never touches cloud_identity.json or access.json."""

def child_env(settings: Settings, base: Mapping[str, str]) -> dict[str, str]:
    """base plus ICE_COLDER_CONFIG and ICE_COLDER_DISPENSERS pointing into data_dir;
    strips MQTT_BROKER_HOST, MQTT_USERNAME, MQTT_PASSWORD so a host .env can't redirect
    the children away from the loopback broker."""
```

Loading in tests (put this in the test module, not in a shared helper):

```python
LAUNCHER = Path(__file__).resolve().parents[2] / "docker" / "machine" / "launcher.py"
spec = importlib.util.spec_from_file_location("machine_launcher", LAUNCHER)
launcher = importlib.util.module_from_spec(spec); spec.loader.exec_module(launcher)
```

Keep every test on a `tmp_path` data dir with `ICE_COLDER_DATA=str(tmp_path)` in the env mapping passed to `Settings.from_env`; `hostname="unit-host"`.

- [ ] **Step 1: Write the failing tests** (`tests/docker/test_machine_launcher.py`)
  - `test_from_env_requires_cloud_url`: env without `ICE_CLOUD_URL` and with it empty both raise `LauncherError`; `str(exc)` contains `ICE_CLOUD_URL`.
  - `test_from_env_defaults`: only `ICE_CLOUD_URL` set -> `machine_id == "unit-host"`, `report_interval == 60`, `data_dir == Path("/app/data")`.
  - `test_from_env_interval_bounds`: `"9"`, `"3601"`, `"abc"` raise; `"10"` and `"3600"` accepted.
  - `test_seed_fresh_dir_sells`: after `seed`, `config.json` loads as `ConfigModel`; three products with the §4.2 sku/slot/kind/button; `validate_document((tmp/'dispensers.toml').read_text(), config.products).errors == []`; `cloud.enabled is True`, `cloud.url` and `machine_id` from env, `mqtt.broker_host == "127.0.0.1"`.
  - `test_seed_preserves_identity_and_catalog`: write `cloud_identity.json` and `access.json` with arbitrary bytes, `seed` once, add a fourth product via `ice_edge.config.config_store.add_product` and `save_config`, hand-edit `machine_id` to `"edited"` in the JSON, `seed` again: both files byte-identical, four products present, `machine_id` back to env value.
  - `test_seed_leaves_existing_dispensers_alone`: pre-write `dispensers.toml` with `"# custom\n"`, `seed`, content unchanged.
  - `test_seed_rejects_directory_config`: `(tmp/'config.json').mkdir()` -> `LauncherError`.
  - `test_seed_rejects_unparsable_config`: write `"{not json"` -> `LauncherError`.
  - `test_child_env`: result has the two `ICE_COLDER_*` paths and none of the three `MQTT_*` keys even when `base` carries them.
- [ ] **Step 2: Run** `uv run pytest tests/docker -q` — expect import/attribute failures.
- [ ] **Step 3: Implement** `docker/machine/launcher.py` with a module docstring citing the spec, the interfaces above, and a `main()` stub (`raise SystemExit("supervision arrives in Task 2")`) guarded by `if __name__ == "__main__":`.
- [ ] **Step 4: Run** `uv run pytest tests/docker -q` — all pass. Then `uv run pytest tests/meta -q` (private-access, test-isolation, skip-policy meta tests still pass).
- [ ] **Step 5: Lint** `uv run ruff check --fix .` then `uv run ruff format .`.
- [ ] **Step 6: Commit** `feat(fleet): machine launcher settings and config seeding`.

---

### Task 2: Launcher supervision

**Files:**
- Modify: `docker/machine/launcher.py` (append; do not change Task 1's interfaces)
- Modify: `tests/docker/test_machine_launcher.py` (append)

**Interfaces (consumes):** `Settings`, `seed`, `child_env`, `LauncherError` from Task 1.

**Interfaces (produces):**

```python
MOSQUITTO_CONF = "/app/machine/mosquitto.conf"
BROKER = ("127.0.0.1", 1883)
RESTART_DELAY = 5.0
STOP_TIMEOUT = 10.0

@dataclass(frozen=True)
class ChildSpec:
    name: str                 # "mosquitto", "vending", "ice_maker", "mdb", "edge"
    argv: tuple[str, ...]
    fatal: bool = False       # True only for mosquitto (§4.3 step 3)

def app_children(settings: Settings) -> tuple[ChildSpec, ...]:
    """The four §4.3 step-2 children, argv = (sys.executable, "-m", module, *args)."""

async def wait_for_port(host: str, port: int, *, timeout: float, interval: float = 0.25) -> None:
    """TCP-connect poll; LauncherError after timeout."""

class Supervisor:
    def __init__(self, specs: Sequence[ChildSpec], *, env: Mapping[str, str],
                 restart_delay: float = RESTART_DELAY, stop_timeout: float = STOP_TIMEOUT) -> None: ...
    async def run(self) -> int:
        """Start every spec, then loop: a non-fatal child that exits is logged and restarted
        after restart_delay; a fatal child exiting -> stop() and return 1; a stop request
        (request_stop) -> stop() and return 0."""
    def request_stop(self) -> None:       # what the SIGTERM/SIGINT handlers call
    async def stop(self) -> None:         # SIGTERM all, wait stop_timeout, SIGKILL survivors

async def amain(env: Mapping[str, str]) -> int:
    """Settings.from_env (hostname=socket.gethostname()) -> seed -> start mosquitto alone,
    wait_for_port(timeout=10) -> Supervisor over mosquitto + app_children with child_env
    -> install signal handlers -> return run()'s code. LauncherError -> print, return 1."""

def main() -> None: raise SystemExit(asyncio.run(amain(os.environ)))
```

Mosquitto is started by the same `Supervisor` (it's the `fatal=True` spec); the port wait happens between "start mosquitto" and "start the rest", so `Supervisor.run` must accept starting in two phases. Simplest: `Supervisor.start(spec)` is public and `run()` only supervises what has been started plus whatever is passed at construction and not yet started. Pick one design and document it in the class docstring; the tests below only use the public names above.

On Windows there is no `SIGTERM` handler via `loop.add_signal_handler`; guard it with `if sys.platform != "win32"` so the unit tests run on the developer's machine (the container is Linux).

- [ ] **Step 1: Write the failing tests** (append to `tests/docker/test_machine_launcher.py`; all `async` tests use the repo's existing `asyncio_mode`, check `pyproject.toml` `[tool.pytest.ini_options]`):
  - `test_wait_for_port_times_out`: an unbound port on 127.0.0.1 (bind a socket, read its port, close it) with `timeout=0.3` raises `LauncherError`.
  - `test_wait_for_port_succeeds`: start `asyncio.start_server` on an ephemeral port; `wait_for_port` returns.
  - `test_supervisor_restarts_exiting_child`: one spec `argv=(sys.executable, "-c", "import sys; sys.exit(3)")`, `restart_delay=0.05`; run the supervisor as a task, poll a public counter `supervisor.restarts["name"]` until `>= 2` (bounded poll, 5 s cap), then `request_stop()` and assert `await task == 0`. Add `restarts: dict[str, int]` to the public interface.
  - `test_supervisor_fatal_child_returns_1`: one fatal spec that exits 0 immediately plus one non-fatal `sleep` spec (`sys.executable -c "import time; time.sleep(100)"`); `await supervisor.run() == 1` within 5 s and the sleeper is no longer running (`spec` → its process `returncode is not None`; expose `supervisor.processes: dict[str, asyncio.subprocess.Process]`).
  - `test_app_children_argv`: four specs named `vending`, `ice_maker`, `mdb`, `edge`; the three sims carry `--broker 127.0.0.1`; none is fatal; the modules are `ice_sims.vending_machine`, `ice_sims.ice_maker`, `ice_sims.mdb_gateway`, `ice_edge`.
  - `test_amain_missing_url_returns_1` (`capsys`): `await amain({"ICE_COLDER_DATA": str(tmp_path)}) == 1` and stdout contains `ICE_CLOUD_URL`. Nothing is spawned (assert no `config.json` was written).
- [ ] **Step 2: Run** `uv run pytest tests/docker -q` — new tests fail.
- [ ] **Step 3: Implement** the supervision half. Each child's stdout/stderr are inherited (no pipes). Every launcher log line starts with `[launcher]`.
- [ ] **Step 4: Run** `uv run pytest tests/docker tests/meta -q` — pass.
- [ ] **Step 5: Lint** `uv run ruff check --fix .` then `uv run ruff format .`.
- [ ] **Step 6: Commit** `feat(fleet): machine launcher process supervision`.

---

### Task 3: Image, broker config, compose file, README

**Files:**
- Create: `docker/machine/Dockerfile`
- Create: `docker/machine/mosquitto.conf`
- Create: `docker/docker-compose.fleet.yml`
- Create: `docker/machine/README.md`

Independent of Tasks 1–2 (it only names `launcher.py`; the file need not be final).

- [ ] **Step 1: `mosquitto.conf`** — exactly: `listener 1883 127.0.0.1`, `allow_anonymous true`, `persistence false`, `log_dest stdout`. A comment at the top says why it is anonymous (loopback inside one container, §D3).
- [ ] **Step 2: `Dockerfile`** — `ARG BASE_IMAGE=ghcr.io/conradstorz/ice-colder:latest`, `FROM ${BASE_IMAGE}`, `apt-get update`, `apt-get install -y --no-install-recommends mosquitto`, clean apt lists in the same `RUN`, `COPY launcher.py mosquitto.conf /app/machine/`, `CMD ["python", "/app/machine/launcher.py"]`. No `EXPOSE` change needed (inherited 26123). Header comment: build context is `docker/machine/`, so the root `.dockerignore` does not apply (§3).
- [ ] **Step 3: `docker-compose.fleet.yml`** — the §5 block verbatim, with the header comment explaining `ICE_FLEET_BASE_IMAGE`, the three example commands, and that `ICE_COLDER_MACHINE_ID` belongs in an override file for a named machine.
- [ ] **Step 4: `README.md`** — sections: What this is (one paragraph, §1); Run (the three commands, from the repo root); Claiming (grep the logs for the setup code, go to the cloud's `/claim`, §D5); What a restart keeps and what it overwrites (§4.2 step 4–5); Offline test (`stop` one replica); Building against a local base image (`ICE_FLEET_BASE_IMAGE`); Limits (the §1 non-goals, one line each).
- [ ] **Step 5: Verify the compose file parses**: from the repo root, `docker compose -f docker/docker-compose.fleet.yml config` with `ICE_CLOUD_URL=http://example.invalid` in the environment. Expect the rendered service with `scale`-safe settings (no `container_name`) and exit 0. Also `uv run yamllint`-free check is not available; the compose parse is the check.
- [ ] **Step 6: Verify the image builds** (through the default `hpz440` context, which pulls the base image there): `docker build -t ice-colder-machine:dev docker/machine`. Expect success and `docker run --rm ice-colder-machine:dev mosquitto -h` to print mosquitto's usage.
- [ ] **Step 7: Commit** `feat(fleet): simulated machine image, compose file and README`.

---

### Task 4: End-to-end smoke run and CI step

**Files:**
- Modify: `.github/workflows/ci.yml` (the `image` job, after the "Smoke-test the image" step, before the cloud image build)

**Consumes:** the finished `docker/machine/` from Tasks 2 and 3.

- [ ] **Step 1: Local smoke run first** (through `hpz440`): `docker build -t ice-colder-machine:dev docker/machine`; `docker run -d --name machine-smoke -e ICE_CLOUD_URL=http://127.0.0.1:9 ice-colder-machine:dev`; after about 30 s, `docker logs machine-smoke`. Expect these lines (§7): mosquitto `Opening ipv4 listen socket on port 1883`; `[vending] Connected to 127.0.0.1:1883`; `[ice_maker] Connected to 127.0.0.1:1883`; `[mdb] Connected to 127.0.0.1:1883`; `Entering main event loop with web server, MQTT client, and health monitor`; `Dashboard is in setup mode`. Then `docker rm -f machine-smoke`. If any line is missing, that is a Task 1–3 bug: fix it there (own commit) before continuing. Note that the published `latest` base may predate this branch's uplink changes; the smoke lines above do not depend on them.
- [ ] **Step 2: Add the CI step** named `Smoke-test the simulated machine image` as a `run:` block that: builds `docker/machine` with `--build-arg BASE_IMAGE=ice-colder:smoke` as `ice-colder-machine:smoke`; runs it detached with `ICE_CLOUD_URL=http://127.0.0.1:9`; loops up to 60 s (`for i in $(seq 1 60)`, `sleep 1`) until `docker logs` contains all six patterns (`grep -q` each); on success removes the container and exits 0; on timeout prints the full log, removes the container, and exits 1. Keep the step self-contained (no new actions, no new jobs).
- [ ] **Step 3: Verify the YAML**: `uv run python -c "import yaml, sys; yaml.safe_load(open('.github/workflows/ci.yml'))"` (PyYAML is in the dev group; if not, use `docker compose`-style parse is not applicable; fall back to `uv run python -m json.tool`-free visual check and `act`-free review). Also re-run `docker compose -f docker-compose.yml -f docker/docker-compose.fleet.yml config` is not required: the fleet file is standalone.
- [ ] **Step 4: Commit** `ci: smoke-test the simulated machine image` — this is the only commit touching a shared file; keep it to that one file.

---

## Self-review

- §4.1 env table → Task 1 `Settings.from_env`. §4.2 seed steps 1–5 → Task 1 `seed` and its tests (directory/unparsable, defaults + products, example dispensers only when absent, re-apply five fields, identity/access untouched). §4.3 start order, restart, fatal mosquitto, signals → Task 2. §5 compose → Task 3. §6 tests → Tasks 1–2 (`tests/docker/`, file-path import, hostname injected). §7 CI → Task 4. §8 table → covered by the same tasks. §3 README → Task 3.
- Names used across tasks: `Settings`, `seed`, `child_env`, `LauncherError`, `ChildSpec`, `app_children`, `wait_for_port`, `Supervisor` (`run`, `request_stop`, `stop`, `restarts`, `processes`), `amain`, `main` — consistent.
- Deviation from the spec recorded here: `seed` calls `render_example()` in-process instead of shelling out to `python -m ice_edge.vend.dispensers --example`; the output is the same function's, and it avoids a subprocess in the pure seeding function.
