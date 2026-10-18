"""Build the vending ESP32 firmware and program an attached KC868-A8.

Run from the repository root: ``uv run python scripts/flash_vending.py``.

Steps: build ``firmware/vending`` for ``kc868-a8``, list the serial ports PlatformIO can see,
pick the one whose USB id belongs to a known USB-serial bridge, and upload to it. With no board
attached the script prints that and exits 0, leaving the build in place; with several candidate
boards it asks for ``--port``.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PROJECT_DIR = Path("firmware") / "vending"
ENV = "kc868-a8"

# USB vendor ids of the serial bridges found on ESP32 dev boards: QinHeng (CH340/CH9102),
# Silicon Labs (CP210x), FTDI, Espressif (native USB-serial/JTAG).
KNOWN_VIDS = frozenset({"1A86", "10C4", "0403", "303A"})

_VID_RE = re.compile(r"VID(?::PID)?[:_=]([0-9A-Fa-f]{4})")


@dataclass(frozen=True)
class SerialPort:
    port: str
    description: str
    hwid: str

    @property
    def vid(self) -> str | None:
        m = _VID_RE.search(self.hwid)
        return m.group(1).upper() if m else None


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


def run(args: list[str], cwd: Path) -> int:
    print("+", " ".join(args), flush=True)
    return subprocess.call(args, cwd=cwd)


def list_ports(cwd: Path) -> list[SerialPort]:
    out = subprocess.run(
        ["pio", "device", "list", "--json-output"],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    return parse_device_list(out.stdout)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", help="serial port to program (skips auto-detection)")
    ap.add_argument(
        "--no-build", action="store_true", help="upload the existing build only"
    )
    ap.add_argument(
        "--monitor", action="store_true", help="open the serial monitor after upload"
    )
    opts = ap.parse_args(argv)

    root = REPO_ROOT
    project = root / PROJECT_DIR
    if not (project / "platformio.ini").is_file():
        print(f"error: {project} is not a PlatformIO project", file=sys.stderr)
        return 2

    if not opts.no_build:
        rc = run(["pio", "run", "-e", ENV, "-d", str(project)], cwd=root)
        if rc != 0:
            print("build failed", file=sys.stderr)
            return rc

    port = opts.port
    if port is None:
        ports = list_ports(root)
        boards = candidate_boards(ports)
        if not boards:
            seen = ", ".join(p.port for p in ports) or "none"
            print(
                f"no board attached (serial ports seen: {seen}); build is up to date, nothing flashed"
            )
            return 0
        if len(boards) > 1:
            print(
                "several boards attached; pass --port <one of these>:", file=sys.stderr
            )
            for b in boards:
                print(f"  {b.port}  {b.description}  [{b.hwid}]", file=sys.stderr)
            return 1
        port = boards[0].port
        print(f"board on {port}: {boards[0].description} [{boards[0].hwid}]")

    rc = run(
        [
            "pio",
            "run",
            "-e",
            ENV,
            "-d",
            str(project),
            "-t",
            "upload",
            "--upload-port",
            port,
        ],
        cwd=root,
    )
    if rc != 0:
        print("upload failed", file=sys.stderr)
        return rc
    print(f"flashed {ENV} to {port}")

    if opts.monitor:
        return run(
            ["pio", "device", "monitor", "-d", str(project), "-p", port], cwd=root
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
