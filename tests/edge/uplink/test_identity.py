import json
import os
import stat

import pytest

from ice_contracts.cloud import signing
from ice_edge.uplink.identity import ClaimNotice, IdentityError, MachineIdentity


def test_creates_file_once_and_reloads_same_fingerprint(tmp_path):
    path = tmp_path / "data" / "cloud_identity.json"
    first = MachineIdentity.load_or_create(path)
    assert path.exists()
    assert first.path == path
    assert len(first.fingerprint) == 64
    assert first.short_fingerprint == signing.short_fingerprint(first.fingerprint)
    second = MachineIdentity.load_or_create(path)
    assert second.fingerprint == first.fingerprint
    assert second.public_key_b64 == first.public_key_b64
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["version"] == 1
    assert doc["notice"] is None


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
def test_mode_0600_on_posix(tmp_path):
    path = tmp_path / "cloud_identity.json"
    MachineIdentity.load_or_create(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    "text",
    ["not json", "[]", '{"version": 1}', '{"version": 1, "private_key_b64": "!!"}'],
)
def test_corrupt_file_raises_not_regenerates(tmp_path, text):
    path = tmp_path / "cloud_identity.json"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(IdentityError):
        MachineIdentity.load_or_create(path)
    assert path.read_text(encoding="utf-8") == text


def test_sign_verifies_with_public_key(tmp_path):
    import base64

    ident = MachineIdentity.load_or_create(tmp_path / "id.json")
    ts = "2026-10-19T12:00:00Z"
    sig = ident.sign("post", "/api/v1/x", ts, b"{}")
    message = signing.canonical_message("POST", "/api/v1/x", ts, b"{}")
    public = base64.b64decode(ident.public_key_b64)
    assert signing.verify(public, message, sig)
    assert not signing.verify(public, message + b"x", sig)
    assert signing.fingerprint(public) == ident.fingerprint


def test_set_notice_persists_unacked_and_ack_flips(tmp_path):
    path = tmp_path / "id.json"
    ident = MachineIdentity.load_or_create(path)
    assert ident.notice is None
    ident.set_notice("hash1", "2026-10-20T00:00:00Z")
    expected = ClaimNotice("hash1", "2026-10-20T00:00:00Z", False)
    assert ident.notice == expected
    assert MachineIdentity.load_or_create(path).notice == expected
    assert ident.mark_notice_acked(expected) is True
    acked = ClaimNotice("hash1", "2026-10-20T00:00:00Z", True)
    assert ident.notice == acked
    assert MachineIdentity.load_or_create(path).notice == acked


def test_mark_notice_acked_without_notice_is_noop(tmp_path):
    ident = MachineIdentity.load_or_create(tmp_path / "id.json")
    assert ident.mark_notice_acked(ClaimNotice("h", None, False)) is False
    assert ident.notice is None


def test_newer_notice_replaces_older(tmp_path):
    path = tmp_path / "id.json"
    ident = MachineIdentity.load_or_create(path)
    ident.set_notice("old", "2026-10-20T00:00:00Z")
    assert ident.notice is not None
    ident.mark_notice_acked(ident.notice)
    ident.set_notice("new", "2026-10-21T00:00:00Z")
    assert ident.notice == ClaimNotice("new", "2026-10-21T00:00:00Z", False)
    assert MachineIdentity.load_or_create(path).notice == ident.notice


def test_set_notice_none_hash_means_delete_pending(tmp_path):
    path = tmp_path / "id.json"
    ident = MachineIdentity.load_or_create(path)
    ident.set_notice("h", "2026-10-20T00:00:00Z")
    assert ident.notice is not None
    ident.mark_notice_acked(ident.notice)
    ident.set_notice(None, None)
    assert ident.notice == ClaimNotice(None, None, False)
    assert MachineIdentity.load_or_create(path).notice == ClaimNotice(None, None, False)


def test_mark_acked_ignores_stale_notice(tmp_path):
    path = tmp_path / "id.json"
    ident = MachineIdentity.load_or_create(path)
    ident.set_notice("h1", None)
    stale = ident.notice
    assert stale is not None
    ident.set_notice("h2", None)
    assert ident.mark_notice_acked(stale) is False
    assert ident.notice == ClaimNotice("h2", None, False)
    assert MachineIdentity.load_or_create(path).notice == ClaimNotice("h2", None, False)


def test_identity_file_unchanged_by_flags(tmp_path):
    path = tmp_path / "cloud_identity.json"
    identity = MachineIdentity.load_or_create(path)
    before = path.read_text(encoding="utf-8")
    assert identity.alerts_handled is False
    assert identity.last_report_ok_at is None
    from datetime import UTC, datetime

    identity.alerts_handled = True
    identity.last_report_ok_at = datetime(2026, 10, 19, tzinfo=UTC)
    identity.set_notice(None, None)
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert set(doc) == set(json.loads(before))
    assert MachineIdentity.load_or_create(path).alerts_handled is False


@pytest.mark.parametrize(
    "handled, age_seconds, expected",
    [
        (False, 0, False),
        (True, None, False),
        (True, 180, True),
        (True, 181, False),
    ],
)
def test_cloud_handles_alerts_truth_table(tmp_path, handled, age_seconds, expected):
    from datetime import UTC, datetime, timedelta

    from ice_edge.uplink.identity import cloud_handles_alerts

    now = datetime(2026, 10, 19, tzinfo=UTC)
    identity = MachineIdentity.load_or_create(tmp_path / "id.json")
    identity.alerts_handled = handled
    if age_seconds is not None:
        identity.last_report_ok_at = now - timedelta(seconds=age_seconds)
    assert cloud_handles_alerts(identity, 60, now) is expected


def test_cloud_handles_alerts_without_identity():
    from datetime import UTC, datetime

    from ice_edge.uplink.identity import cloud_handles_alerts

    assert cloud_handles_alerts(None, 60, datetime.now(UTC)) is False
