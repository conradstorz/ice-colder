"""Build, flash, verify and provision an ESP32 board (``vending`` or ``mdb``).

Run from the repository root::

    uv run python scripts/flash_board.py mdb     [--port COMx] [--no-build] [--monitor]
                                                 [--provision mdb.toml] [--verify-only]
    uv run python scripts/flash_board.py vending [--port COMx] [--no-build] [--monitor]
                                                 [--provision vending.toml] [--verify-only]

Exit codes: 0 verified, 1 flashed but unverified, 2 build or upload failed, 3 usage.
A provisioning file is TOML with string values for wifi_ssid, wifi_pass, mqtt_host, mqtt_port,
mqtt_user, mqtt_pass and machine_id; secrets are never printed.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

EXIT_VERIFIED = 0
EXIT_UNVERIFIED = 1
EXIT_FAILED = 2
EXIT_USAGE = 3

# USB vendor ids of the serial bridges found on ESP32 dev boards: QinHeng (CH340/CH9102),
# Silicon Labs (CP210x), FTDI, Espressif (native USB-serial/JTAG).
KNOWN_VIDS = frozenset({"1A86", "10C4", "0403", "303A"})

PROVISION_KEYS = (
    "wifi_ssid",
    "wifi_pass",
    "mqtt_host",
    "mqtt_port",
    "mqtt_user",
    "mqtt_pass",
    "machine_id",
)
SECRET_KEYS = frozenset({"wifi_pass", "mqtt_pass"})

BAUD = 115200

_VID_RE = re.compile(r"VID(?::PID)?[:_=]([0-9A-Fa-f]{4})")
_SER_RE = re.compile(r"SER=(\S+)")
_BANNER_RE = re.compile(r"firmware[ \t]+(\S+)")
_VERSION_RE = re.compile(r"kFirmwareVersion\s*=\s*\"([^\"]+)\"")
_BUSY_RE = re.compile(
    r"could not open port|PermissionError|Access is denied|Device or resource busy|"
    r"port is busy",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Target:
    name: str
    project_dir: Path
    env: str
    prompt: str


TARGETS: dict[str, Target] = {
    "vending": Target("vending", Path("firmware/vending"), "kc868-a8", "vend> "),
    "mdb": Target("mdb", Path("firmware/mdb"), "esp32-s3-devkitc-1", "mdb> "),
}


@dataclass(frozen=True)
class Options:
    target: Target
    port: str | None = None
    no_build: bool = False
    monitor: bool = False
    provision: Path | None = None
    verify_only: bool = False


class ProvisionError(ValueError):
    """The provisioning file is unusable. Messages never contain values."""


@dataclass(frozen=True)
class SerialPort:
    port: str
    description: str
    hwid: str

    @property
    def vid(self) -> str | None:
        m = _VID_RE.search(self.hwid)
        return m.group(1).upper() if m else None

    @property
    def serial_number(self) -> str | None:
        m = _SER_RE.search(self.hwid)
        return m.group(1) if m else None


# ---------------------------------------------------------------- pure logic


def parse_device_list(text: str) -> list[SerialPort]:
    """Parse ``pio device list --json-output``; a blank or malformed listing is an empty list."""
    try:
        raw = json.loads(text or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(raw, list):
        return []
    return [
        SerialPort(
            port=str(d.get("port", "")),
            description=str(d.get("description", "")),
            hwid=str(d.get("hwid", "")),
        )
        for d in raw
        if isinstance(d, dict) and d.get("port")
    ]


def candidate_boards(ports: list[SerialPort]) -> list[SerialPort]:
    """The ports whose USB vendor id is a known ESP32 serial bridge."""
    return [p for p in ports if p.vid in KNOWN_VIDS]


def find_port_by_serial(ports: list[SerialPort], serial: str) -> SerialPort | None:
    """The port carrying USB serial number ``serial`` (an S3 may re-enumerate elsewhere)."""
    if not serial:
        return None
    for p in ports:
        if p.serial_number == serial:
            return p
    return None


def parse_banner(text: str) -> str | None:
    """The version from a ``firmware <version>`` boot line, else None."""
    m = _BANNER_RE.search(text)
    return m.group(1) if m else None


def expected_version(project_dir: Path) -> str:
    """``kFirmwareVersion`` from the project's ``lib/*/version.h``."""
    for header in sorted(project_dir.glob("lib/*/version.h")):
        m = _VERSION_RE.search(header.read_text(encoding="utf-8"))
        if m:
            return m.group(1)
    raise FileNotFoundError(f"no kFirmwareVersion in {project_dir}/lib/*/version.h")


def load_provisioning(path: Path) -> dict[str, str]:
    """Read a provisioning TOML: only the allowed keys, string values without line breaks."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as e:
        raise ProvisionError(
            f"cannot read provisioning file {path}: {e.strerror}"
        ) from e
    except tomllib.TOMLDecodeError as e:
        raise ProvisionError(f"provisioning file {path} is not valid TOML") from e
    out: dict[str, str] = {}
    for key, value in data.items():
        if key not in PROVISION_KEYS:
            raise ProvisionError(
                f"unknown provisioning key {key!r}; allowed: {', '.join(PROVISION_KEYS)}"
            )
        if not isinstance(value, str):
            raise ProvisionError(f"provisioning key {key!r} must be a string")
        if "\r" in value or "\n" in value:
            raise ProvisionError(
                f"provisioning key {key!r} must not contain a line break"
            )
        out[key] = value
    return out


def quote_console_arg(value: str) -> str:
    """Quote ``value`` for the board's ``esp_console_split_argv``: double quotes around it, with
    backslash and double quote escaped by a backslash (so spaces and empty values survive)."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def set_reply_ok(reply: str) -> bool:
    """A ``set`` reply that confirms the value was stored (``saved; reboot to apply``)."""
    return "saved" in reply and "rejected" not in reply and "usage" not in reply


def show_reply_ok(reply: str) -> bool:
    """A ``show`` reply that really is the settings listing, not just a bare prompt."""
    return "machine_id" in reply


def no_board_exit(opts: Options) -> int:
    """Exit code when no board is attached: a plain flash has nothing to do (0), but a request to
    verify or provision a board that is not there did not succeed (1)."""
    return (
        EXIT_UNVERIFIED
        if opts.verify_only or opts.provision is not None
        else EXIT_VERIFIED
    )


def mask_secret(key: str, value: str) -> str:
    return "***" if key in SECRET_KEYS else value


def exit_code(*, build_ok: bool, upload_ok: bool, verified: bool) -> int:
    if not build_ok or not upload_ok:
        return EXIT_FAILED
    return EXIT_VERIFIED if verified else EXIT_UNVERIFIED


def plan_steps(opts: Options) -> list[str]:
    steps: list[str] = []
    if not opts.verify_only:
        if not opts.no_build:
            steps.append("build")
        steps += ["detect", "upload", "verify"]
    else:
        steps += ["detect", "verify"]
    if opts.provision is not None:
        steps += ["provision", "verify"]
    if opts.monitor:
        steps.append("monitor")
    return steps


def is_port_busy(output: str) -> bool:
    return bool(_BUSY_RE.search(output))


class _ArgParser(argparse.ArgumentParser):
    def error(self, message: str):  # type: ignore[override]
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: error: {message}\n")


def build_parser() -> argparse.ArgumentParser:
    ap = _ArgParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("target", choices=sorted(TARGETS), help="which board to flash")
    ap.add_argument("--port", help="serial port to program (skips auto-detection)")
    ap.add_argument("--no-build", action="store_true", help="upload the existing build")
    ap.add_argument(
        "--monitor", action="store_true", help="open the serial monitor last"
    )
    ap.add_argument(
        "--provision", type=Path, metavar="FILE.toml", help="provisioning file"
    )
    ap.add_argument(
        "--verify-only", action="store_true", help="skip build and upload; verify only"
    )
    return ap


# ------------------------------------------------------------- side effects


def run(args: list[str], cwd: Path) -> tuple[int, str]:
    """Run a command, echoing its output; return (exit code, captured output)."""
    print("+", " ".join(args), flush=True)
    proc = subprocess.Popen(
        args,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
    )
    lines: list[str] = []
    assert proc.stdout is not None
    for line in proc.stdout:
        print(line, end="", flush=True)
        lines.append(line)
    return proc.wait(), "".join(lines)


def list_ports(cwd: Path = REPO_ROOT) -> list[SerialPort]:
    out = subprocess.run(
        ["pio", "device", "list", "--json-output"],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    return parse_device_list(out.stdout)


def _pio(target: Target, *extra: str) -> list[str]:
    return [
        "pio",
        "run",
        "-e",
        target.env,
        "-d",
        str(REPO_ROOT / target.project_dir),
        *extra,
    ]


def upload_with_retry(
    target: Target, port: SerialPort, attempts: int = 2, pause_s: float = 2.0
) -> tuple[bool, SerialPort]:
    """Upload, retrying after a pause and re-finding the board by USB serial number."""
    current = port
    for attempt in range(1, attempts + 1):
        rc, out = run(
            _pio(target, "-t", "upload", "--upload-port", current.port), REPO_ROOT
        )
        if rc == 0:
            return True, current
        if is_port_busy(out):
            print(
                f"{current.port} is busy (is a serial monitor open?); close it and retry",
                file=sys.stderr,
            )
            return False, current
        if attempt == attempts:
            break
        print(
            f"upload attempt {attempt} failed; retrying in {pause_s:g} s",
            file=sys.stderr,
        )
        time.sleep(pause_s)
        if port.serial_number:
            moved = find_port_by_serial(list_ports(), port.serial_number)
            if moved is not None:
                if moved.port != current.port:
                    print(f"board re-enumerated on {moved.port}")
                current = moved
    return False, current


def _open_serial(port: str, timeout: float):
    try:
        import serial  # type: ignore[import-not-found]
    except ImportError:
        print(
            "pyserial is not installed in this environment; cannot talk to the console "
            "(try: uv run --with pyserial python scripts/flash_board.py ...)",
            file=sys.stderr,
        )
        return None
    try:
        return serial.Serial(port, BAUD, timeout=timeout)
    except Exception as e:  # noqa: BLE001 - report, never raise into the exit-code logic
        print(f"cannot open {port}: {e}", file=sys.stderr)
        return None


def _read_until(ser, needle: str, timeout_s: float) -> str:
    deadline = time.monotonic() + timeout_s
    buf = ""
    while time.monotonic() < deadline and needle not in buf:
        buf += ser.read(256).decode("utf-8", errors="replace")
    return buf


def verify(port: str, target: Target, timeout_s: float = 15.0) -> bool:
    """Reset the board, wait for its boot banner, run ``show``, compare the version."""
    try:
        want = expected_version(REPO_ROOT / target.project_dir)
    except FileNotFoundError as e:
        print(f"cannot determine expected version: {e}", file=sys.stderr)
        return False
    ser = _open_serial(port, 0.25)
    if ser is None:
        return False
    with ser:
        ser.dtr = False
        ser.rts = True  # pulse the reset line; harmless where it is not wired
        time.sleep(0.1)
        ser.rts = False
        text = _read_until(ser, "firmware ", timeout_s)
        text += _read_until(ser, "\n", 1.0)
        got = parse_banner(text)
        if got is None:
            print(f"no boot banner from {port} within {timeout_s:g} s", file=sys.stderr)
            return False
        if got != want:
            print(
                f"version mismatch: board reports {got}, build is {want}",
                file=sys.stderr,
            )
            return False
        ser.reset_input_buffer()  # boot chatter must not pass for the answer
        ser.write(b"\r\nshow\r\n")
        shown = _read_until(ser, target.prompt, 5.0)
        if target.prompt not in shown or not show_reply_ok(shown):
            print(
                f"console on {port} did not answer `show` with its settings",
                file=sys.stderr,
            )
            return False
    print(f"verified {target.name} {got} on {port}")
    return True


def provision(port: str, target: Target, values: dict[str, str]) -> bool:
    ser = _open_serial(port, 0.25)
    if ser is None:
        return False
    with ser:
        ser.write(b"\r\n")
        _read_until(ser, target.prompt, 3.0)
        for key, value in values.items():
            print(f"set {key} {mask_secret(key, value)}")
            ser.reset_input_buffer()
            ser.write(f"set {key} {quote_console_arg(value)}\r\n".encode())
            reply = _read_until(ser, target.prompt, 5.0)
            if target.prompt not in reply:
                print(f"no prompt after setting {key}", file=sys.stderr)
                return False
            if not set_reply_ok(reply):
                print(f"the board did not accept {key}", file=sys.stderr)
                return False
        print("reboot")
        ser.write(b"reboot\r\n")
    time.sleep(1.0)
    return True


def _select_port(opts: Options) -> tuple[SerialPort | None, int]:
    """The board to use, or (None, exit code): 0 when none is attached, 3 when ambiguous."""
    ports = list_ports()
    if opts.port is not None:
        for p in ports:
            if p.port == opts.port:
                return p, EXIT_VERIFIED
        return SerialPort(opts.port, "", ""), EXIT_VERIFIED
    boards = candidate_boards(ports)
    if not boards:
        seen = ", ".join(p.port for p in ports) or "none"
        print(f"no board attached (serial ports seen: {seen}); nothing flashed")
        return None, EXIT_VERIFIED
    if len(boards) > 1:
        print("several boards attached; pass --port <one of these>:", file=sys.stderr)
        for b in boards:
            print(f"  {b.port}  {b.description}  [{b.hwid}]", file=sys.stderr)
        return None, EXIT_USAGE
    print(f"board on {boards[0].port}: {boards[0].description} [{boards[0].hwid}]")
    return boards[0], EXIT_VERIFIED


def _refind(port: SerialPort) -> SerialPort:
    """After a reset, follow an S3 that came back on another port."""
    if not port.serial_number:
        return port
    for _ in range(5):
        moved = find_port_by_serial(list_ports(), port.serial_number)
        if moved is not None:
            return moved
        time.sleep(1.0)
    return port


def main(argv: list[str] | None = None) -> int:
    ns = build_parser().parse_args(argv)
    opts = Options(
        target=TARGETS[ns.target],
        port=ns.port,
        no_build=ns.no_build,
        monitor=ns.monitor,
        provision=ns.provision,
        verify_only=ns.verify_only,
    )
    target = opts.target
    if not (REPO_ROOT / target.project_dir / "platformio.ini").is_file():
        print(
            f"error: {target.project_dir} is not a PlatformIO project", file=sys.stderr
        )
        return EXIT_USAGE
    values: dict[str, str] = {}
    if opts.provision is not None:
        try:
            values = load_provisioning(opts.provision)
        except ProvisionError as e:
            print(f"error: {e}", file=sys.stderr)
            return EXIT_USAGE

    build_ok = upload_ok = True
    verified = False
    port: SerialPort | None = None
    for step in plan_steps(opts):
        if step == "build":
            rc, _ = run(_pio(target), REPO_ROOT)
            if rc != 0:
                print("build failed", file=sys.stderr)
                return exit_code(build_ok=False, upload_ok=False, verified=False)
        elif step == "detect":
            port, rc = _select_port(opts)
            if port is None:
                return no_board_exit(opts) if rc == EXIT_VERIFIED else rc
        elif step == "upload":
            assert port is not None
            upload_ok, port = upload_with_retry(target, port)
            if not upload_ok:
                print("upload failed", file=sys.stderr)
                return exit_code(build_ok=build_ok, upload_ok=False, verified=False)
            print(f"flashed {target.env} to {port.port}")
            port = _refind(port)
        elif step == "verify":
            assert port is not None
            verified = verify(port.port, target)
        elif step == "provision":
            assert port is not None
            if not provision(port.port, target, values):
                return EXIT_UNVERIFIED
            port = _refind(port)
        elif step == "monitor":
            assert port is not None
            run(["pio", "device", "monitor", "-p", port.port], REPO_ROOT)
    return exit_code(build_ok=build_ok, upload_ok=upload_ok, verified=verified)


if __name__ == "__main__":
    sys.exit(main())
