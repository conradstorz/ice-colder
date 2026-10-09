# tests/test_dispensers_service.py
"""Tests for `DispenserProfiles` (plan: dispenser profiles, Task 4) -- the
load/validate/save service wrapped around the pure `validate_document`
pipeline (tests/test_dispensers_validation.py).
"""

import hashlib
from pathlib import Path

import pytest

from config.config_model import ConfigModel, PhysicalDetails
from contracts.vending_machine import SubsystemCapabilities
from contracts.common import ChannelDescriptor
from services.dispensers import DispenserProfiles, dispensers_path
from tests.dispenser_fixtures import GOOD, ICE, WATER


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
        contract_version="1.0.0",
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
    with pytest.raises(IsADirectoryError):
        profiles.load()


def test_load_unreadable_file_is_a_file_error(tmp_path, monkeypatch):
    path = tmp_path / "dispensers.toml"
    path.write_text(GOOD, encoding="utf-8")

    def raise_permission_error(self):
        raise PermissionError("Permission denied")

    monkeypatch.setattr(Path, "read_bytes", raise_permission_error)

    profiles = DispenserProfiles(_config(), path=path)
    report = profiles.load()

    assert report.file_error
    assert report.profiles == {}
    assert len(report.errors) == 1
    assert "could not be read" in report.errors[0].message
    assert profiles.digest is None


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


def test_save_before_load_is_refused(tmp_path):
    path = tmp_path / "dispensers.toml"
    path.write_text(GOOD, encoding="utf-8")

    profiles = DispenserProfiles(_config(), path=path)
    report = profiles.save_text(GOOD, expected_digest=None)

    assert not report.ok
    assert len(report.errors) == 1
    assert "never loaded" in report.errors[0].message
    assert path.read_text(encoding="utf-8") == GOOD
    assert not (tmp_path / "dispensers.toml.tmp").exists()


def test_save_none_digest_after_load_is_refused_as_stale(tmp_path):
    path = tmp_path / "dispensers.toml"
    path.write_text(GOOD, encoding="utf-8")

    profiles = DispenserProfiles(_config(), path=path)
    profiles.load()

    report = profiles.save_text(GOOD, expected_digest=None)

    assert not report.ok
    assert len(report.errors) == 1
    assert "changed on disk" in report.errors[0].message
    assert path.read_text(encoding="utf-8") == GOOD


def test_save_refuses_when_file_changed_after_load(tmp_path):
    path = tmp_path / "dispensers.toml"
    bak = tmp_path / "dispensers.toml.bak"
    path.write_text(GOOD, encoding="utf-8")

    profiles = DispenserProfiles(_config(), path=path)
    profiles.load()
    old_digest = profiles.digest

    # The file changes on disk (another editor, a second tab) after load()
    # cached its digest -- a direct write, bypassing the service entirely.
    direct_write = GOOD.replace("run_seconds        = 4.0", "run_seconds        = 9.0")
    path.write_text(direct_write, encoding="utf-8")

    report = profiles.save_text(GOOD, expected_digest=old_digest)

    assert not report.ok
    assert len(report.errors) == 1
    assert "changed on disk" in report.errors[0].message
    assert path.read_text(encoding="utf-8") == direct_write
    assert not bak.exists()
    assert not (tmp_path / "dispensers.toml.tmp").exists()


def test_save_refuses_when_file_appeared_after_missing_load(tmp_path, monkeypatch):
    path = tmp_path / "dispensers.toml"
    monkeypatch.setenv("ICE_COLDER_DISPENSERS", str(path))

    profiles = DispenserProfiles(_config())
    profiles.load()
    assert profiles.digest is None

    # The file appears on disk after the missing-file load().
    path.write_text(GOOD, encoding="utf-8")

    report = profiles.save_text(GOOD, expected_digest=None)

    assert not report.ok
    assert len(report.errors) == 1
    assert "changed on disk" in report.errors[0].message
    assert path.read_text(encoding="utf-8") == GOOD
    assert not (tmp_path / "dispensers.toml.bak").exists()
    assert not (tmp_path / "dispensers.toml.tmp").exists()


def test_save_never_leaves_live_file_absent(tmp_path, monkeypatch):
    path = tmp_path / "dispensers.toml"
    path.write_text(GOOD, encoding="utf-8")

    profiles = DispenserProfiles(_config(), path=path)
    profiles.load()
    digest = profiles.digest

    def raise_on_replace(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr("services.dispensers.os.replace", raise_on_replace)

    new_text = GOOD.replace("run_seconds        = 4.0", "run_seconds        = 5.0")
    with pytest.raises(OSError):
        profiles.save_text(new_text, expected_digest=digest)

    assert path.exists()
    assert path.read_text(encoding="utf-8") == GOOD
    assert not (tmp_path / "dispensers.toml.tmp").exists()


def test_save_cleanup_failure_never_masks_the_original_error(tmp_path, monkeypatch):
    # os.replace fails ("disk full"); the `finally` block's own tmp-file
    # cleanup must not raise a second exception that replaces the first
    # in the traceback seen by the caller -- the original failure is the
    # one that matters and must be what propagates.
    path = tmp_path / "dispensers.toml"
    path.write_text(GOOD, encoding="utf-8")

    profiles = DispenserProfiles(_config(), path=path)
    profiles.load()
    digest = profiles.digest

    def raise_on_replace(src, dst):
        raise OSError("disk full")

    def raise_on_unlink(self, *args, **kwargs):
        raise OSError("cannot delete tmp file")

    monkeypatch.setattr("services.dispensers.os.replace", raise_on_replace)
    monkeypatch.setattr(Path, "unlink", raise_on_unlink)

    new_text = GOOD.replace("run_seconds        = 4.0", "run_seconds        = 5.0")
    with pytest.raises(OSError) as exc_info:
        profiles.save_text(new_text, expected_digest=digest)

    assert "disk full" in str(exc_info.value)
    assert path.read_text(encoding="utf-8") == GOOD


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


def test_revalidate_picks_up_catalog_change(tmp_path):
    """Copilot review (PR #32) finding C1: `revalidate()` re-runs
    validation against the *current* `config.products` -- same shape as
    `set_capabilities`, no disk read -- so a catalog edit after load()
    (e.g. a web route changing a product's kind) is picked up without
    needing to reload dispensers.toml from disk."""
    path = tmp_path / "dispensers.toml"
    path.write_text(GOOD, encoding="utf-8")
    # A local copy, never the shared module-level ICE/WATER objects --
    # mutating those would bleed into every other test that imports them.
    water = WATER.model_copy()
    config = ConfigModel(physical=PhysicalDetails(products=[ICE, water]))
    profiles = DispenserProfiles(config, path=path)
    report = profiles.load()
    assert report.ok

    water.kind = "ice"  # now disagrees with slot 2's water_fill mechanism

    report = profiles.revalidate()

    assert not report.ok
    assert profiles.report is report


def test_revalidate_is_a_noop_before_any_load():
    profiles = DispenserProfiles(_config())
    before = profiles.report
    report = profiles.revalidate()
    assert report is before


def test_load_non_utf8_file_is_a_file_error(tmp_path):
    path = tmp_path / "dispensers.toml"
    bad_bytes = b"schema_version = 1\n\xff\xfe"
    path.write_bytes(bad_bytes)

    profiles = DispenserProfiles(_config(), path=path)
    report = profiles.load()

    assert report.file_error
    assert report.profiles == {}
    assert len(report.errors) == 1
    error = report.errors[0]
    assert error.slot is None
    assert "not valid UTF-8" in error.message
    assert profiles._text is None
    assert profiles.digest == hashlib.sha256(bad_bytes).hexdigest()


def test_save_fsyncs_directory(tmp_path, monkeypatch):
    path = tmp_path / "dispensers.toml"
    path.write_text(GOOD, encoding="utf-8")

    profiles = DispenserProfiles(_config(), path=path)
    profiles.load()
    digest = profiles.digest

    # Monkeypatch fsync_dir to record calls
    fsync_calls = []

    def mock_fsync_dir(directory):
        fsync_calls.append(directory)

    monkeypatch.setattr("services.dispensers.fsync_dir", mock_fsync_dir)

    new_text = GOOD.replace("run_seconds        = 4.0", "run_seconds        = 5.0")
    report = profiles.save_text(new_text, expected_digest=digest)

    assert report.ok
    assert len(fsync_calls) == 1
    assert fsync_calls[0] == path.parent
