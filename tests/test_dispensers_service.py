# tests/test_dispensers_service.py
"""Tests for `DispenserProfiles` (plan: dispenser profiles, Task 4) -- the
load/validate/save service wrapped around the pure `validate_document`
pipeline (tests/test_dispensers_validation.py).
"""

import hashlib
from pathlib import Path

from config.config_model import ConfigModel, PhysicalDetails
from contracts.vending_machine import SubsystemCapabilities
from contracts.common import ChannelDescriptor
from services.dispensers import DispenserProfiles, dispensers_path
from tests.test_dispensers_validation import GOOD, ICE, WATER


def _config() -> ConfigModel:
    return ConfigModel(physical=PhysicalDetails(products=[ICE, WATER]))


def _good_capabilities() -> SubsystemCapabilities:
    def ch(channel_id: str, direction: str) -> ChannelDescriptor:
        return ChannelDescriptor(
            channel_id=channel_id,
            kind="binary",
            interval_seconds=1.0,
            direction=direction,
        )

    return SubsystemCapabilities(
        subsystem="vending",
        firmware="x",
        contract_version="0.8.0",
        channels=[
            ch("agitator_motor", "output"),
            ch("auger_motor", "output"),
            ch("bag_drop_solenoid", "output"),
            ch("bag_fan", "output"),
            ch("vending_now_light", "output"),
            ch("water_valve_solenoid", "output"),
            ch("bag_full_sensor", "input"),
            ch("door_sensor", "input"),
            ch("water_flow_sensor", "input"),
        ],
    )


def test_path_from_env_read_at_call_time(monkeypatch, tmp_path):
    custom = tmp_path / "custom-dispensers.toml"
    monkeypatch.setenv("ICE_COLDER_DISPENSERS", str(custom))
    assert dispensers_path() == custom

    monkeypatch.delenv("ICE_COLDER_DISPENSERS")
    assert dispensers_path() == Path("dispensers.toml")


def test_load_missing_file_is_a_warning_with_no_profiles(monkeypatch, tmp_path):
    path = tmp_path / "dispensers.toml"
    monkeypatch.setenv("ICE_COLDER_DISPENSERS", str(path))

    profiles = DispenserProfiles(_config())
    report = profiles.load()

    assert report.file_error
    assert report.profiles == {}
    assert len(report.warnings) == 1
    assert report.warnings[0].slot is None
    assert report.warnings[0].path == ""
    assert str(path) in report.warnings[0].message
    assert profiles.digest is None
    assert profiles.report is report


def test_load_directory_raises(monkeypatch, tmp_path):
    path = tmp_path / "dispensers.toml"
    path.mkdir()
    monkeypatch.setenv("ICE_COLDER_DISPENSERS", str(path))

    profiles = DispenserProfiles(_config())
    try:
        profiles.load()
    except IsADirectoryError:
        pass
    else:
        raise AssertionError("expected IsADirectoryError")


def test_load_good_file_populates_profiles_and_digest(tmp_path):
    path = tmp_path / "dispensers.toml"
    path.write_bytes(GOOD.encode("utf-8"))

    profiles = DispenserProfiles(_config(), path=path)
    report = profiles.load()

    assert report.ok
    assert set(report.profiles.keys()) == {1, 2}
    assert profiles.digest == hashlib.sha256(GOOD.encode("utf-8")).hexdigest()
    assert profiles.profile_for_slot(1) is not None
    assert profiles.profile_for_slot(1).product_sku == "ICE-10LB"
    assert profiles.profile_for_slot(99) is None


def test_validate_text_writes_nothing(tmp_path):
    path = tmp_path / "dispensers.toml"
    path.write_text(GOOD, encoding="utf-8")
    before_mtime = path.stat().st_mtime_ns
    before_bytes = path.read_bytes()

    profiles = DispenserProfiles(_config(), path=path)
    profiles.load()

    report = profiles.validate_text(GOOD.replace("ICE-10LB", "ICE-99LB"))
    assert not report.ok

    after_mtime = path.stat().st_mtime_ns
    after_bytes = path.read_bytes()
    assert before_mtime == after_mtime
    assert before_bytes == after_bytes
    # validate_text must not touch self.report either.
    assert profiles.report.ok


def test_save_refuses_errors_and_leaves_file_and_bak_untouched(tmp_path):
    path = tmp_path / "dispensers.toml"
    bak = tmp_path / "dispensers.toml.bak"
    path.write_text(GOOD, encoding="utf-8")
    bak.write_text("sentinel-bak-contents", encoding="utf-8")

    profiles = DispenserProfiles(_config(), path=path)
    profiles.load()
    digest = profiles.digest

    bad_text = GOOD.replace("ICE-10LB", "ICE-99LB")
    report = profiles.save_text(bad_text, expected_digest=digest)

    assert not report.ok
    assert path.read_text(encoding="utf-8") == GOOD
    assert bak.read_text(encoding="utf-8") == "sentinel-bak-contents"
    assert profiles.digest == digest
    assert not (tmp_path / "dispensers.toml.tmp").exists()


def test_save_refuses_stale_digest(tmp_path):
    path = tmp_path / "dispensers.toml"
    path.write_text(GOOD, encoding="utf-8")

    profiles = DispenserProfiles(_config(), path=path)
    profiles.load()

    report = profiles.save_text(GOOD, expected_digest="stale-digest")

    assert not report.ok
    assert len(report.errors) == 1
    assert "changed on disk" in report.errors[0].message
    assert path.read_text(encoding="utf-8") == GOOD
    assert not (tmp_path / "dispensers.toml.tmp").exists()
    assert not (tmp_path / "dispensers.toml.bak").exists()


def test_save_writes_atomically_and_rotates_bak(tmp_path):
    path = tmp_path / "dispensers.toml"
    path.write_bytes(GOOD.encode("utf-8"))

    profiles = DispenserProfiles(_config(), path=path)
    profiles.load()
    digest = profiles.digest

    new_text = GOOD.replace("run_seconds        = 4.0", "run_seconds        = 5.0")
    report = profiles.save_text(new_text, expected_digest=digest)

    assert report.ok
    assert path.read_bytes() == new_text.encode("utf-8")
    assert (tmp_path / "dispensers.toml.bak").read_bytes() == GOOD.encode("utf-8")
    assert not (tmp_path / "dispensers.toml.tmp").exists()
    assert profiles.digest == hashlib.sha256(new_text.encode("utf-8")).hexdigest()
    assert profiles.profile_for_slot(1).agitate.run_seconds == 5.0


def test_set_capabilities_clears_warnings(tmp_path):
    path = tmp_path / "dispensers.toml"
    path.write_text(GOOD, encoding="utf-8")

    profiles = DispenserProfiles(_config(), path=path)
    load_report = profiles.load()
    assert len(load_report.warnings) == 2  # unknown-capabilities, one per slot

    report = profiles.set_capabilities(_good_capabilities())

    assert report.ok
    assert len(report.warnings) == 0
    assert profiles.capabilities is not None
    # The file on disk and self.report reflect the new, warning-free report.
    assert profiles.report is report
