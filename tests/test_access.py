"""Tests for services/access.py."""

import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from services.access import (
    BACKOFF_CAP_SECONDS,
    ROLE_PERMISSIONS,
    AccessError,
    AccessStore,
    Backoff,
    Device,
    OwnerExistsError,
    Permission,
    Role,
    generate_code,
    generate_token,
    hash_pin,
    hash_secret,
    token_fingerprint,
    verify_pin,
    verify_secret,
)


class FakeClock:
    """Monotonic clock under test control."""

    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeRequest:
    """Minimal stand-in for a starlette Request for client_ip()/is_https()."""

    class _Client:
        def __init__(self, host):
            self.host = host

    class _Url:
        def __init__(self, scheme):
            self.scheme = scheme

    def __init__(self, peer="10.0.0.5", headers=None, scheme="http"):
        self.client = self._Client(peer)
        self.headers = headers or {}
        self.url = self._Url(scheme)


class TestBackoffDelays:
    def test_first_attempt_is_allowed(self):
        b = Backoff(clock=FakeClock())
        assert b.check("pin", "u1", "dev1") is None

    def test_delays_double_from_one_second(self):
        clock = FakeClock()
        b = Backoff(clock=clock)
        for want in (1.0, 2.0, 4.0, 8.0, 16.0):
            b.record_failure("pin", "u1", "dev1", trusted=True)
            assert b.check("pin", "u1", "dev1", trusted=True) == pytest.approx(want)
            clock.advance(want)
            assert b.check("pin", "u1", "dev1", trusted=True) is None

    def test_delay_caps_at_one_hour(self):
        b = Backoff(clock=FakeClock())
        for _ in range(30):
            b.record_failure("pin", "u1", "dev1", trusted=True)
        assert b.check("pin", "u1", "dev1", trusted=True) == pytest.approx(
            BACKOFF_CAP_SECONDS
        )

    def test_success_resets_the_counter(self):
        b = Backoff(clock=FakeClock())
        for _ in range(5):
            b.record_failure("pin", "u1", "dev1", trusted=True)
        b.record_success("pin", "u1", "dev1", trusted=True)
        assert b.check("pin", "u1", "dev1", trusted=True) is None

    def test_clients_are_independent(self):
        b = Backoff(clock=FakeClock())
        for _ in range(4):
            b.record_failure("pin", "u1", "dev1", trusted=True)
        assert b.check("pin", "u1", "dev1", trusted=True) is not None
        assert b.check("pin", "u1", "dev2", trusted=True) is None

    def test_kinds_are_independent(self):
        b = Backoff(clock=FakeClock())
        for _ in range(4):
            b.record_failure("pin", "u1", "dev1", trusted=True)
        assert b.check("otp", "u1", "dev1", trusted=True) is None

    def test_entries_idle_a_day_are_pruned(self):
        clock = FakeClock()
        b = Backoff(clock=clock)
        for _ in range(4):
            b.record_failure("pin", "u1", "dev1", trusted=True)
        clock.advance(86401)
        assert b.check("pin", "u1", "dev1", trusted=True) is None
        assert b._failures == {}


class TestPerUserBudget:
    def test_untrusted_failures_slow_every_untrusted_client(self):
        clock = FakeClock()
        b = Backoff(clock=clock)
        # 20 failures across 20 distinct untrusted clients: each client's own
        # counter is 1 (a 1 s delay, long expired) but the budget has tripped.
        for i in range(20):
            if i:
                clock.advance(2)
            b.record_failure("pin", "u1", f"ip{i}")
        assert b.check("pin", "u1", "fresh-ip") == pytest.approx(1.0)

    def test_budget_delay_grows_and_caps(self):
        b = Backoff(clock=FakeClock())
        for i in range(30):
            b.record_failure("pin", "u1", f"ip{i}")
        # 30 failures -> 11 over the threshold -> 2 ** 10 == 1024 s.
        assert b.check("pin", "u1", "fresh-ip") == pytest.approx(1024.0)
        for i in range(30, 45):
            b.record_failure("pin", "u1", f"ip{i}")
        assert b.check("pin", "u1", "fresh-ip") == pytest.approx(BACKOFF_CAP_SECONDS)

    def test_budget_failures_age_out_of_the_hour(self):
        clock = FakeClock()
        b = Backoff(clock=clock)
        for i in range(20):
            b.record_failure("pin", "u1", f"ip{i}")
        clock.advance(3601)
        assert b.check("pin", "u1", "fresh-ip") is None

    def test_trusted_client_never_reads_the_budget(self):
        b = Backoff(clock=FakeClock())
        for i in range(25):
            b.record_failure("pin", "u1", f"ip{i}")
        # The legitimate tablet, where u1 is trusted, is unaffected.
        assert b.check("pin", "u1", "tablet", trusted=True) is None

    def test_trusted_failures_do_not_raise_the_budget(self):
        clock = FakeClock()
        b = Backoff(clock=clock)
        for _ in range(25):
            b.record_failure("pin", "u1", "tablet", trusted=True)
            b.record_success("pin", "u1", "tablet", trusted=True)
        assert b.check("pin", "u1", "fresh-ip") is None


class TestBackoffClientIp:
    def test_forwarded_header_ignored_from_untrusted_peer(self):
        b = Backoff(clock=FakeClock())
        req = FakeRequest("10.0.0.5", {"x-forwarded-for": "1.2.3.4"})
        assert b.client_ip(req) == "10.0.0.5"

    def test_rightmost_hop_used_from_trusted_proxy(self):
        b = Backoff(clock=FakeClock(), trusted_proxies=["10.0.0.0/24"])
        req = FakeRequest("10.0.0.5", {"x-forwarded-for": "1.2.3.4, 9.9.9.9"})
        assert b.client_ip(req) == "9.9.9.9"

    def test_trusted_proxy_without_header_falls_back_to_peer(self):
        b = Backoff(clock=FakeClock(), trusted_proxies=["10.0.0.0/24"])
        assert b.client_ip(FakeRequest("10.0.0.5")) == "10.0.0.5"

    def test_invalid_cidr_is_ignored_not_fatal(self):
        b = Backoff(clock=FakeClock(), trusted_proxies=["not-a-cidr", "10.0.0.0/24"])
        req = FakeRequest("10.0.0.5", {"x-forwarded-for": "9.9.9.9"})
        assert b.client_ip(req) == "9.9.9.9"


class TestBackoffIsHttps:
    def test_direct_https_scheme(self):
        b = Backoff(clock=FakeClock())
        assert b.is_https(FakeRequest(scheme="https")) is True

    def test_forwarded_proto_only_from_trusted_proxy(self):
        b = Backoff(clock=FakeClock(), trusted_proxies=["10.0.0.0/24"])
        req = FakeRequest("10.0.0.5", {"x-forwarded-proto": "https"})
        assert b.is_https(req) is True

    def test_forwarded_proto_from_untrusted_peer_is_ignored(self):
        b = Backoff(clock=FakeClock())
        req = FakeRequest("8.8.8.8", {"x-forwarded-proto": "https"})
        assert b.is_https(req) is False


class TestHashing:
    def test_pin_round_trip(self):
        h, s = hash_pin("1379")
        assert verify_pin("1379", h, s)
        assert not verify_pin("1380", h, s)

    def test_same_pin_two_users_different_hashes(self):
        h1, s1 = hash_pin("1379")
        h2, s2 = hash_pin("1379")
        assert s1 != s2
        assert h1 != h2

    def test_pin_hash_is_hex_and_not_the_pin(self):
        h, s = hash_pin("1379")
        bytes.fromhex(h)
        bytes.fromhex(s)
        assert "1379" not in h

    def test_secret_round_trip(self):
        stored = hash_secret("12345678")
        assert stored.startswith("scrypt$")
        assert verify_secret("12345678", stored)
        assert not verify_secret("12345679", stored)

    def test_verify_secret_rejects_garbage_without_raising(self):
        assert verify_secret("12345678", "") is False
        assert verify_secret("12345678", "nonsense") is False
        assert verify_secret("12345678", "scrypt$zz$zz") is False


class TestCodeGeneration:
    def test_generate_code_length_and_digits(self):
        for _ in range(50):
            code = generate_code(8)
            assert len(code) == 8
            assert code.isdigit()

    def test_generate_code_six_digits(self):
        assert len(generate_code(6)) == 6

    def test_tokens_are_unique_and_long(self):
        tokens = {generate_token() for _ in range(100)}
        assert len(tokens) == 100
        assert all(len(t) >= 40 for t in tokens)

    def test_token_fingerprint_is_stable_sha256(self):
        assert token_fingerprint("abc") == token_fingerprint("abc")
        assert len(token_fingerprint("abc")) == 64
        assert token_fingerprint("abc") != token_fingerprint("abd")


class TestPermissionTable:
    def test_every_role_has_an_entry(self):
        assert set(ROLE_PERMISSIONS) == set(Role)

    def test_everyone_sees_status_and_edits_placement(self):
        for role in Role:
            assert Permission.view_status in ROLE_PERMISSIONS[role]
            assert Permission.edit_placement in ROLE_PERMISSIONS[role]

    def test_owner_has_every_permission(self):
        assert ROLE_PERMISSIONS[Role.owner] == frozenset(Permission)

    def test_secretary_matrix(self):
        assert ROLE_PERMISSIONS[Role.secretary] == frozenset(
            {
                Permission.view_status,
                Permission.edit_catalog,
                Permission.edit_placement,
                Permission.view_reports,
                Permission.edit_contacts,
                Permission.manage_users,
            }
        )

    def test_tech_matrix(self):
        assert ROLE_PERMISSIONS[Role.tech] == frozenset(
            {
                Permission.view_status,
                Permission.clear_faults,
                Permission.view_logs,
                Permission.machine_controls,
                Permission.run_tests,
                Permission.edit_placement,
            }
        )

    def test_loader_matrix(self):
        assert ROLE_PERMISSIONS[Role.loader] == frozenset(
            {Permission.view_status, Permission.edit_placement}
        )

    def test_only_owner_manages_ownership_or_secrets(self):
        for role in (Role.secretary, Role.tech, Role.loader):
            assert Permission.manage_ownership not in ROLE_PERMISSIONS[role]
            assert Permission.edit_secrets not in ROLE_PERMISSIONS[role]


class FakeWallClock:
    def __init__(self, start: datetime | None = None):
        self.now = start or datetime(2026, 9, 25, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now += timedelta(**kwargs)


@pytest.fixture
def store(tmp_path):
    return AccessStore(
        path=tmp_path / "access.json", clock=FakeClock(), wall_clock=FakeWallClock()
    )


class TestStorePersistence:
    def test_missing_file_is_empty_not_corrupt(self, store):
        assert store.users == {}
        assert store.devices == {}
        assert store.corrupt is False
        assert store.owner() is None

    def test_created_user_survives_a_reload(self, tmp_path):
        path = tmp_path / "access.json"
        s1 = AccessStore(path=path, clock=FakeClock(), wall_clock=FakeWallClock())
        user = s1.create_user("Ada", "ada@example.com", Role.owner, "1379")
        s2 = AccessStore(path=path, clock=FakeClock(), wall_clock=FakeWallClock())
        assert s2.get_user(user.id).name == "Ada"
        assert s2.get_user(user.id).role is Role.owner
        assert s2.verify_user_pin(user.id, "1379")

    def test_pin_is_never_written_in_clear(self, tmp_path):
        path = tmp_path / "access.json"
        s = AccessStore(path=path, clock=FakeClock(), wall_clock=FakeWallClock())
        s.create_user("Ada", None, Role.owner, "1379")
        assert "1379" not in path.read_text(encoding="utf-8")

    def test_invalid_json_marks_the_store_corrupt(self, tmp_path):
        path = tmp_path / "access.json"
        path.write_text("{not json", encoding="utf-8")
        s = AccessStore(path=path, clock=FakeClock(), wall_clock=FakeWallClock())
        assert s.corrupt is True
        assert s.users == {}

    def test_a_corrupt_store_refuses_to_write(self, tmp_path):
        path = tmp_path / "access.json"
        path.write_text("{not json", encoding="utf-8")
        s = AccessStore(path=path, clock=FakeClock(), wall_clock=FakeWallClock())
        with pytest.raises(AccessError):
            s.create_user("Ada", None, Role.owner, "1379")
        assert path.read_text(encoding="utf-8") == "{not json"

    def test_timestamps_are_utc_iso8601(self, tmp_path):
        path = tmp_path / "access.json"
        s = AccessStore(path=path, clock=FakeClock(), wall_clock=FakeWallClock())
        s.create_user("Ada", None, Role.owner, "1379")
        raw = json.loads(path.read_text(encoding="utf-8"))
        created = list(raw["users"].values())[0]["created_at"]
        assert created == "2026-09-25T12:00:00+00:00"

    @pytest.mark.skipif(os.name != "posix", reason="POSIX file modes only")
    def test_file_is_created_0600(self, tmp_path):
        path = tmp_path / "access.json"
        s = AccessStore(path=path, clock=FakeClock(), wall_clock=FakeWallClock())
        s.create_user("Ada", None, Role.owner, "1379")
        assert (path.stat().st_mode & 0o777) == 0o600

    @pytest.mark.skipif(os.name != "posix", reason="POSIX file modes only")
    def test_loose_permissions_are_tightened_with_a_warning(self, tmp_path, caplog):
        path = tmp_path / "access.json"
        path.write_text('{"users": {}, "devices": {}}', encoding="utf-8")
        path.chmod(0o644)
        AccessStore(path=path, clock=FakeClock(), wall_clock=FakeWallClock())
        assert (path.stat().st_mode & 0o777) == 0o600
        assert "0600" in caplog.text

    @pytest.mark.skipif(os.name != "posix", reason="POSIX file modes only")
    def test_no_temp_file_is_left_behind(self, tmp_path):
        path = tmp_path / "access.json"
        s = AccessStore(path=path, clock=FakeClock(), wall_clock=FakeWallClock())
        s.create_user("Ada", None, Role.owner, "1379")
        assert list(tmp_path.glob("*.tmp")) == []

    def test_replace_failure_during_save_leaves_no_tmp_file(self, store, monkeypatch):
        """os.replace can fail (permission error, locked destination — routine
        on Windows); the temp file must still be cleaned up and the error
        must still propagate to the caller rather than being swallowed."""
        store.create_user("Ada", None, Role.owner, "1379")

        def boom(*args, **kwargs):
            raise OSError("simulated os.replace failure")

        monkeypatch.setattr("services.access.os.replace", boom)
        with pytest.raises(OSError):
            store.create_user("Bob", None, Role.tech, "2468")
        assert list(store.path.parent.glob("*.tmp")) == []


class TestUsers:
    def test_second_owner_is_rejected(self, store):
        store.create_user("Ada", None, Role.owner, "1379")
        with pytest.raises(OwnerExistsError):
            store.create_user("Bob", None, Role.owner, "2468")

    def test_second_owner_rejection_does_not_persist_the_user(self, store):
        store.create_user("Ada", None, Role.owner, "1379")
        with pytest.raises(OwnerExistsError):
            store.create_user("Bob", None, Role.owner, "2468")
        assert [u.name for u in store.users.values()] == ["Ada"]

    def test_other_roles_may_repeat(self, store):
        store.create_user("T1", None, Role.tech, "1379")
        store.create_user("T2", None, Role.tech, "2468")
        assert len(store.users) == 2

    def test_promoting_a_second_user_to_owner_is_rejected(self, store):
        store.create_user("Ada", None, Role.owner, "1379")
        bob = store.create_user("Bob", None, Role.tech, "2468")
        with pytest.raises(OwnerExistsError):
            store.update_user(bob.id, role=Role.owner)

    def test_disabled_user_fails_pin_verification(self, store):
        u = store.create_user("Ada", None, Role.owner, "1379")
        store.set_user_disabled(u.id, True)
        assert store.verify_user_pin(u.id, "1379") is False

    def test_unknown_user_fails_pin_verification(self, store):
        assert store.verify_user_pin("nope", "1379") is False

    def test_enabled_users_excludes_disabled(self, store):
        a = store.create_user("Ada", None, Role.owner, "1379")
        b = store.create_user("Bob", None, Role.tech, "2468")
        store.set_user_disabled(b.id, True)
        assert [u.id for u in store.enabled_users()] == [a.id]

    def test_reset_pin_changes_the_hash_and_untrusts_every_device(self, store):
        u = store.create_user("Ada", None, Role.owner, "1379")
        dev, _ = store.create_device("Tablet", shared=True)
        store.trust_device(dev.id, u.id)
        store.set_user_pin(u.id, "2468")
        assert store.verify_user_pin(u.id, "2468")
        assert store.verify_user_pin(u.id, "1379") is False
        assert store.devices[dev.id].trusted_user_ids == []

    def test_deleting_a_user_removes_them_from_every_device(self, store):
        u = store.create_user("Ada", None, Role.owner, "1379")
        d1, _ = store.create_device("Tablet", shared=True)
        d2, _ = store.create_device("Phone", shared=False)
        store.trust_device(d1.id, u.id)
        store.trust_device(d2.id, u.id)
        store.delete_user(u.id)
        assert store.get_user(u.id) is None
        assert store.devices[d1.id].trusted_user_ids == []
        assert store.devices[d2.id].trusted_user_ids == []

    def test_trusting_an_unknown_user_is_refused(self, store):
        dev, _ = store.create_device("Tablet", shared=True)
        with pytest.raises(AccessError):
            store.trust_device(dev.id, "nobody")


class TestDevices:
    def test_token_resolves_to_its_device_and_is_not_stored_raw(self, tmp_path):
        path = tmp_path / "access.json"
        s = AccessStore(path=path, clock=FakeClock(), wall_clock=FakeWallClock())
        dev, token = s.create_device("Tablet", shared=True)
        assert s.device_for_token(token).id == dev.id
        assert token not in path.read_text(encoding="utf-8")

    def test_unknown_or_missing_token_resolves_to_none(self, store):
        store.create_device("Tablet", shared=True)
        assert store.device_for_token("bogus") is None
        assert store.device_for_token(None) is None

    def test_forget_and_shared_toggle(self, store):
        dev, _ = store.create_device("Tablet", shared=True)
        store.set_device_shared(dev.id, False)
        assert store.devices[dev.id].shared is False
        store.forget_device(dev.id)
        assert dev.id not in store.devices

    def test_stale_unenrolled_devices_are_pruned_after_a_day(self, tmp_path):
        wall = FakeWallClock()
        s = AccessStore(
            path=tmp_path / "access.json", clock=FakeClock(), wall_clock=wall
        )
        user = s.create_user("Ada", None, Role.owner, "1379")
        kept, _ = s.create_device("Tablet", shared=True)
        s.trust_device(kept.id, user.id)
        abandoned, _ = s.create_device("Drive-by", shared=False)
        wall.advance(hours=25)
        assert s.prune_devices() == 1
        assert kept.id in s.devices
        assert abandoned.id not in s.devices

    def test_fresh_unenrolled_devices_survive(self, tmp_path):
        wall = FakeWallClock()
        s = AccessStore(
            path=tmp_path / "access.json", clock=FakeClock(), wall_clock=wall
        )
        fresh, _ = s.create_device("Drive-by", shared=False)
        wall.advance(hours=23)
        assert s.prune_devices() == 0
        assert fresh.id in s.devices

    def test_naive_timestamp_is_skipped_but_sweep_still_prunes_others(self, tmp_path):
        """A malformed (timezone-naive) created_at must not raise TypeError and
        abort the whole sweep; the bad record is left in place while a
        separate, genuinely stale device is still pruned in the same call."""
        wall = FakeWallClock()
        s = AccessStore(
            path=tmp_path / "access.json", clock=FakeClock(), wall_clock=wall
        )
        naive = Device(
            id="naive-device",
            token_hash="deadbeef",
            label="Hand-edited",
            created_at="2020-01-01T00:00:00",  # no tzinfo
            last_seen_at="2020-01-01T00:00:00",
        )
        s.devices[naive.id] = naive
        stale, _ = s.create_device("Drive-by", shared=False)
        wall.advance(hours=25)
        assert s.prune_devices() == 1
        assert naive.id in s.devices
        assert stale.id not in s.devices


class TestSessions:
    def test_session_resolves_to_its_user_and_device(self, store):
        u = store.create_user("Ada", None, Role.owner, "1379")
        d, _ = store.create_device("Phone", shared=False)
        sid = store.create_session(u.id, d.id)
        session = store.resolve_session(sid)
        assert session.user_id == u.id
        assert session.device_id == d.id

    def test_unknown_session_resolves_to_none(self, store):
        assert store.resolve_session("nope") is None
        assert store.resolve_session(None) is None

    def test_shared_device_idles_out_after_five_minutes(self, tmp_path):
        clock = FakeClock()
        s = AccessStore(
            path=tmp_path / "access.json", clock=clock, wall_clock=FakeWallClock()
        )
        u = s.create_user("Ada", None, Role.owner, "1379")
        d, _ = s.create_device("Tablet", shared=True)
        sid = s.create_session(u.id, d.id)
        clock.advance(299)
        assert s.resolve_session(sid) is not None
        clock.advance(301)
        assert s.resolve_session(sid) is None

    def test_personal_device_idles_out_after_eight_hours(self, tmp_path):
        clock = FakeClock()
        s = AccessStore(
            path=tmp_path / "access.json", clock=clock, wall_clock=FakeWallClock()
        )
        u = s.create_user("Ada", None, Role.owner, "1379")
        d, _ = s.create_device("Phone", shared=False)
        sid = s.create_session(u.id, d.id)
        clock.advance(28799)
        assert s.resolve_session(sid) is not None
        clock.advance(28801)
        assert s.resolve_session(sid) is None

    def test_activity_refreshes_the_idle_clock(self, tmp_path):
        clock = FakeClock()
        s = AccessStore(
            path=tmp_path / "access.json", clock=clock, wall_clock=FakeWallClock()
        )
        u = s.create_user("Ada", None, Role.owner, "1379")
        d, _ = s.create_device("Tablet", shared=True)
        sid = s.create_session(u.id, d.id)
        for _ in range(10):
            clock.advance(200)
            assert s.resolve_session(sid) is not None

    def test_absolute_cap_ends_a_busy_session_at_a_day(self, tmp_path):
        clock = FakeClock()
        s = AccessStore(
            path=tmp_path / "access.json", clock=clock, wall_clock=FakeWallClock()
        )
        u = s.create_user("Ada", None, Role.owner, "1379")
        d, _ = s.create_device("Phone", shared=False)
        sid = s.create_session(u.id, d.id)
        for _ in range(500):
            clock.advance(200)
            s.resolve_session(sid)
        assert s.resolve_session(sid) is None

    def test_session_dies_with_its_user(self, store):
        u = store.create_user("Ada", None, Role.owner, "1379")
        d, _ = store.create_device("Phone", shared=False)
        sid = store.create_session(u.id, d.id)
        store.delete_user(u.id)
        assert store.resolve_session(sid) is None

    def test_session_dies_with_its_device(self, store):
        u = store.create_user("Ada", None, Role.owner, "1379")
        d, _ = store.create_device("Phone", shared=False)
        sid = store.create_session(u.id, d.id)
        store.forget_device(d.id)
        assert store.resolve_session(sid) is None

    def test_end_session_and_end_all(self, store):
        u = store.create_user("Ada", None, Role.owner, "1379")
        d, _ = store.create_device("Phone", shared=False)
        s1 = store.create_session(u.id, d.id)
        s2 = store.create_session(u.id, d.id)
        store.end_session(s1)
        assert store.resolve_session(s1) is None
        assert store.resolve_session(s2) is not None
        store.end_all_sessions()
        assert store.resolve_session(s2) is None


class TestOtps:
    def test_otp_round_trip(self, store):
        u = store.create_user("Ada", None, Role.owner, "1379")
        d, _ = store.create_device("Phone", shared=False)
        code = store.issue_otp(u.id, d.id)
        assert len(code) == 6 and code.isdigit()
        assert store.verify_otp(u.id, d.id, code) is True

    def test_otp_is_single_use(self, store):
        u = store.create_user("Ada", None, Role.owner, "1379")
        d, _ = store.create_device("Phone", shared=False)
        code = store.issue_otp(u.id, d.id)
        assert store.verify_otp(u.id, d.id, code) is True
        assert store.verify_otp(u.id, d.id, code) is False

    def test_otp_expires_after_ten_minutes(self, tmp_path):
        clock = FakeClock()
        s = AccessStore(
            path=tmp_path / "access.json", clock=clock, wall_clock=FakeWallClock()
        )
        u = s.create_user("Ada", None, Role.owner, "1379")
        d, _ = s.create_device("Phone", shared=False)
        code = s.issue_otp(u.id, d.id)
        clock.advance(601)
        assert s.verify_otp(u.id, d.id, code) is False

    def test_resend_replaces_the_pending_otp(self, store):
        u = store.create_user("Ada", None, Role.owner, "1379")
        d, _ = store.create_device("Phone", shared=False)
        first = store.issue_otp(u.id, d.id)
        second = store.issue_otp(u.id, d.id)
        assert store.verify_otp(u.id, d.id, first) is False
        assert store.verify_otp(u.id, d.id, second) is True

    def test_otp_is_bound_to_its_device(self, store):
        u = store.create_user("Ada", None, Role.owner, "1379")
        d1, _ = store.create_device("Phone", shared=False)
        d2, _ = store.create_device("Laptop", shared=False)
        code = store.issue_otp(u.id, d1.id)
        assert store.verify_otp(u.id, d2.id, code) is False


class TestEnrollTokens:
    def test_enroll_token_round_trip(self, store):
        u = store.create_user("Ada", None, Role.owner, "1379")
        token = store.issue_enroll_token(u.id, "ip-1")
        assert store.resolve_enroll_token(token, "ip-1") == u.id

    def test_enroll_token_is_bound_to_its_client(self, store):
        u = store.create_user("Ada", None, Role.owner, "1379")
        token = store.issue_enroll_token(u.id, "ip-1")
        assert store.resolve_enroll_token(token, "ip-2") is None

    def test_enroll_token_expires_after_ten_minutes(self, tmp_path):
        clock = FakeClock()
        s = AccessStore(
            path=tmp_path / "access.json", clock=clock, wall_clock=FakeWallClock()
        )
        u = s.create_user("Ada", None, Role.owner, "1379")
        token = s.issue_enroll_token(u.id, "ip-1")
        clock.advance(601)
        assert s.resolve_enroll_token(token, "ip-1") is None

    def test_cleared_enroll_token_stops_resolving(self, store):
        u = store.create_user("Ada", None, Role.owner, "1379")
        token = store.issue_enroll_token(u.id, "ip-1")
        store.clear_enroll_token(token)
        assert store.resolve_enroll_token(token, "ip-1") is None
