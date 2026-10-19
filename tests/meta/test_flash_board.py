"""Pure logic of scripts/flash_board.py: no subprocess, no serial ports."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.flash_board import (
    EXIT_FAILED,
    EXIT_UNVERIFIED,
    EXIT_USAGE,
    EXIT_VERIFIED,
    PROVISION_KEYS,
    TARGETS,
    Options,
    ProvisionError,
    SerialPort,
    build_parser,
    candidate_boards,
    no_board_exit,
    exit_code,
    expected_version,
    find_port_by_serial,
    load_provisioning,
    mask_secret,
    parse_banner,
    parse_device_list,
    plan_steps,
    provision,
    quote_console_arg,
    set_reply_ok,
    show_reply_ok,
)

REPO = Path(__file__).resolve().parents[2]


def _listing(*entries: dict) -> str:
    return json.dumps(list(entries))


def _opts(**kw) -> Options:
    base = {"target": TARGETS["mdb"]}
    base.update(kw)
    return Options(**base)


def test_empty_or_malformed_listing_is_no_ports() -> None:
    assert parse_device_list("") == []
    assert parse_device_list("[]") == []
    assert parse_device_list("not json") == []
    assert parse_device_list('{"port": "COM3"}') == []


def test_parses_port_fields_and_vid() -> None:
    ports = parse_device_list(
        _listing(
            {
                "port": "COM5",
                "description": "USB-SERIAL CH340",
                "hwid": "USB VID:PID=1A86:7523",
            }
        )
    )
    assert ports == [SerialPort("COM5", "USB-SERIAL CH340", "USB VID:PID=1A86:7523")]
    assert ports[0].vid == "1A86"
    assert ports[0].serial_number is None


def test_vid_is_none_without_usb_id() -> None:
    assert SerialPort("COM1", "Communications Port", "ACPI\\PNP0501\\1").vid is None


def test_candidates_keep_only_known_bridges() -> None:
    ports = parse_device_list(
        _listing(
            {"port": "COM1", "description": "x", "hwid": "ACPI\\PNP0501\\1"},
            {"port": "COM5", "description": "CH340", "hwid": "USB VID:PID=1A86:7523"},
            {
                "port": "/dev/ttyUSB0",
                "description": "CP2102",
                "hwid": "USB VID:PID=10c4:ea60",
            },
            {"port": "COM9", "description": "Uno", "hwid": "USB VID:PID=2341:0043"},
            {"port": "COM7", "description": "S3", "hwid": "USB VID:PID=303A:1001"},
        )
    )
    assert [p.port for p in candidate_boards(ports, TARGETS["vending"])] == [
        "COM5",
        "/dev/ttyUSB0",
    ]
    assert [p.port for p in candidate_boards(ports, TARGETS["mdb"])] == ["COM7"]


def test_mdb_with_both_devkitc_ports_plugged_in_has_one_candidate() -> None:
    ports = parse_device_list(
        _listing(
            {"port": "COM5", "description": "UART", "hwid": "USB VID:PID=10C4:EA60"},
            {"port": "COM7", "description": "S3", "hwid": "USB VID:PID=303A:1001"},
        )
    )
    assert [p.port for p in candidate_boards(ports, TARGETS["mdb"])] == ["COM7"]


def test_entries_without_port_are_dropped() -> None:
    assert parse_device_list(_listing({"hwid": "USB VID:PID=1A86:7523"})) == []


def test_target_table() -> None:
    v, m = TARGETS["vending"], TARGETS["mdb"]
    assert (v.project_dir, v.env, v.prompt) == (
        Path("firmware/vending"),
        "kc868-a8",
        "vend> ",
    )
    assert (m.project_dir, m.env, m.prompt) == (
        Path("firmware/mdb"),
        "esp32-s3-devkitc-1",
        "mdb> ",
    )


def test_serial_number_parsed_from_hwid() -> None:
    p = SerialPort("COM7", "S3", "USB VID:PID=303A:1001 SER=A1:B2:C3 LOCATION=1-4")
    assert p.serial_number == "A1:B2:C3"
    assert p.vid == "303A"


def test_find_port_by_serial_follows_reenumeration() -> None:
    after = SerialPort("COM9", "S3", "USB VID:PID=303A:1001 SER=AAA LOCATION=1")
    other = SerialPort("COM3", "S3", "USB VID:PID=303A:1001 SER=BBB LOCATION=2")
    assert find_port_by_serial([other, after], "AAA") == after
    assert find_port_by_serial([other], "AAA") is None
    assert find_port_by_serial([after], "") is None


@pytest.mark.parametrize(
    "text",
    [
        "firmware 0.1.0",
        "I (321) vending: firmware 0.1.0",
        "I (321) mdb: firmware 0.1.0\r\nvend> ",
    ],
)
def test_parse_banner(text: str) -> None:
    assert parse_banner(text) == "0.1.0"


def test_parse_banner_absent() -> None:
    assert parse_banner("") is None
    assert parse_banner("firmware\n") is None
    assert parse_banner("boot loop") is None


@pytest.mark.parametrize("name", ["vending", "mdb"])
def test_expected_version_reads_header(name: str) -> None:
    assert expected_version(REPO / TARGETS[name].project_dir) == "0.1.0"


def test_expected_version_missing(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        expected_version(tmp_path)


def test_provision_keys() -> None:
    assert PROVISION_KEYS == (
        "wifi_ssid",
        "wifi_pass",
        "mqtt_host",
        "mqtt_port",
        "mqtt_user",
        "mqtt_pass",
        "machine_id",
    )


def test_load_provisioning_ok(tmp_path: Path) -> None:
    f = tmp_path / "p.toml"
    f.write_text('wifi_ssid = "net"\nmqtt_port = "1883"\n', encoding="utf-8")
    assert load_provisioning(f) == {"wifi_ssid": "net", "mqtt_port": "1883"}


def test_load_provisioning_rejects_unknown_key(tmp_path: Path) -> None:
    f = tmp_path / "p.toml"
    f.write_text('polarity = "high"\n', encoding="utf-8")
    with pytest.raises(ProvisionError, match="polarity"):
        load_provisioning(f)


def test_load_provisioning_rejects_non_string(tmp_path: Path) -> None:
    f = tmp_path / "p.toml"
    f.write_text("mqtt_port = 1883\n", encoding="utf-8")
    with pytest.raises(ProvisionError, match="mqtt_port"):
        load_provisioning(f)


def test_load_provisioning_rejects_newline_in_value(tmp_path: Path) -> None:
    f = tmp_path / "p.toml"
    f.write_text('wifi_ssid = "a\\nreboot"\n', encoding="utf-8")
    with pytest.raises(ProvisionError, match="wifi_ssid"):
        load_provisioning(f)


def test_load_provisioning_missing_or_bad_file(tmp_path: Path) -> None:
    with pytest.raises(ProvisionError):
        load_provisioning(tmp_path / "nope.toml")
    bad = tmp_path / "bad.toml"
    bad.write_text("= = =", encoding="utf-8")
    with pytest.raises(ProvisionError):
        load_provisioning(bad)


def test_error_messages_never_contain_secret(tmp_path: Path) -> None:
    f = tmp_path / "p.toml"
    f.write_text("wifi_pass = 12345678\n", encoding="utf-8")
    with pytest.raises(ProvisionError) as e:
        load_provisioning(f)
    assert "12345678" not in str(e.value)


def test_mask_secret() -> None:
    assert mask_secret("wifi_pass", "hunter2") == "***"
    assert mask_secret("mqtt_pass", "hunter2") == "***"
    assert mask_secret("wifi_ssid", "net") == "net"


def test_exit_codes() -> None:
    assert (EXIT_VERIFIED, EXIT_UNVERIFIED, EXIT_FAILED, EXIT_USAGE) == (0, 1, 2, 3)
    assert exit_code(build_ok=True, upload_ok=True, verified=True) == EXIT_VERIFIED
    assert exit_code(build_ok=True, upload_ok=True, verified=False) == EXIT_UNVERIFIED
    assert exit_code(build_ok=False, upload_ok=False, verified=False) == EXIT_FAILED
    assert exit_code(build_ok=True, upload_ok=False, verified=False) == EXIT_FAILED


def test_plan_default() -> None:
    assert plan_steps(_opts()) == ["build", "detect", "upload", "verify"]


def test_plan_no_build() -> None:
    assert plan_steps(_opts(no_build=True)) == ["detect", "upload", "verify"]


def test_plan_verify_only() -> None:
    assert plan_steps(_opts(verify_only=True)) == ["detect", "verify"]


def test_plan_provision() -> None:
    p = Path("mdb.toml")
    assert plan_steps(_opts(provision=p)) == [
        "build",
        "detect",
        "upload",
        "verify",
        "provision",
        "verify",
    ]
    assert plan_steps(_opts(provision=p, verify_only=True)) == [
        "detect",
        "verify",
        "provision",
        "verify",
    ]


def test_plan_monitor_last() -> None:
    assert plan_steps(_opts(monitor=True))[-1] == "monitor"


def test_parser_target_is_positional_choice(capsys) -> None:
    ns = build_parser().parse_args(["mdb", "--no-build"])
    assert ns.target == "mdb" and ns.no_build
    with pytest.raises(SystemExit) as e:
        build_parser().parse_args(["bogus"])
    assert e.value.code == EXIT_USAGE
    capsys.readouterr()


def test_port_busy_detection() -> None:
    from scripts.flash_board import is_port_busy

    assert is_port_busy("could not open port COM5: PermissionError(13)")
    assert not is_port_busy("A fatal error occurred: Failed to connect")


@pytest.mark.parametrize(
    ("value", "quoted"),
    [
        ("net", '"net"'),
        ("my wifi", '"my wifi"'),
        ("", '""'),
        ('say "hi"', '"say \\"hi\\""'),
        ("back\\slash", '"back\\\\slash"'),
        ('\\"', '"\\\\\\""'),
    ],
)
def test_quote_console_arg(value: str, quoted: str) -> None:
    assert quote_console_arg(value) == quoted


def test_set_reply_ok() -> None:
    assert set_reply_ok("set x\r\nsaved; reboot to apply\r\nmdb> ")
    assert not set_reply_ok("rejected: unknown key or bad value\r\nmdb> ")
    assert not set_reply_ok("usage: set <key> <value>\r\nmdb> ")
    assert not set_reply_ok("mdb> ")


def test_show_reply_needs_the_settings_listing() -> None:
    assert show_reply_ok("machine_id = vm-1\r\nmqtt_port = 1883\r\nmdb> ")
    assert not show_reply_ok("\r\nmdb> ")


def test_no_board_exit_is_unverified_when_asked_to_verify_or_provision() -> None:
    assert no_board_exit(_opts()) == EXIT_VERIFIED
    assert no_board_exit(_opts(verify_only=True)) == EXIT_UNVERIFIED
    assert no_board_exit(_opts(provision=Path("p.toml"))) == EXIT_UNVERIFIED


class _FakeSerial:
    """Answers each console line with a canned reply, ending in the prompt."""

    def __init__(self, replies: dict[str, str], prompt: str = "mdb> ") -> None:
        self.replies, self.prompt = replies, prompt
        self.written: list[str] = []
        self._out = b""

    def __enter__(self):
        return self

    def __exit__(self, *a) -> None:
        return None

    def reset_input_buffer(self) -> None:
        self._out = b""

    def write(self, data: bytes) -> None:
        line = data.decode().strip()
        self.written.append(line)
        key = line.split(" ")[1] if line.startswith("set ") else line
        self._out += self.replies.get(key, self.prompt).encode()

    def read(self, n: int) -> bytes:
        chunk, self._out = self._out[:n], self._out[n:]
        return chunk if chunk else b" "


def _provision(monkeypatch, fake: _FakeSerial, values: dict[str, str]) -> bool:
    import scripts.flash_board as fb

    monkeypatch.setattr(fb, "_open_serial", lambda port, timeout: fake)
    monkeypatch.setattr(fb.time, "sleep", lambda s: None)
    return provision("COM1", TARGETS["mdb"], values)


def test_provision_quotes_values_and_accepts_saved(monkeypatch) -> None:
    fake = _FakeSerial({"wifi_ssid": "saved; reboot to apply\r\nmdb> "})
    assert _provision(monkeypatch, fake, {"wifi_ssid": 'my "net"'})
    assert 'set wifi_ssid "my \\"net\\""' in fake.written
    assert fake.written[-1] == "reboot"


def test_provision_fails_on_rejected_reply_and_never_prints_the_value(
    monkeypatch, capsys
) -> None:
    fake = _FakeSerial({"wifi_pass": "rejected: unknown key or bad value\r\nmdb> "})
    assert not _provision(monkeypatch, fake, {"wifi_pass": "hunter2hunter2"})
    out = capsys.readouterr()
    assert "wifi_pass" in out.err
    assert "hunter2" not in out.out + out.err
    assert "reboot" not in fake.written
