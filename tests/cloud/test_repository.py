from datetime import UTC, datetime, timedelta

import sqlite3

import pytest

from ice_cloud.store import EmailConflict, SqliteRepository, sqlite
from tests.cloud.reports import FP, FP_2

T0 = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
KEY = "a2V5"


@pytest.fixture
def repo(tmp_path):
    return SqliteRepository(tmp_path / "data" / "cloud.db")


def _accounts(repo):
    owner = repo.upsert_account_from_login("sub-o", "Owner@Example.com", "Olive", T0)
    tech = repo.upsert_account_from_login("sub-t", "tech@example.com", "Tess", T0)
    return owner, tech


def test_upsert_account_creates_then_updates_by_google_sub(repo):
    first = repo.upsert_account_from_login("sub-1", "A@Example.com", "Ann", T0)
    assert first.email == "a@example.com" and first.suspended_at is None
    assert first.created_at == T0
    again = repo.upsert_account_from_login(
        "sub-1", "ann@example.com", "Ann B", T0 + timedelta(days=1)
    )
    assert again.id == first.id and again.name == "Ann B"
    assert again.email == "ann@example.com" and again.created_at == T0
    assert repo.account(first.id) == again
    assert repo.account_by_email("ANN@example.com") == again
    assert repo.account("nope") is None and repo.account_by_email("x@y.z") is None


def test_suspend_and_all_accounts(repo):
    owner, tech = _accounts(repo)
    repo.set_suspended(tech.id, T0)
    assert repo.account(tech.id).suspended_at == T0
    repo.set_suspended(tech.id, None)
    assert repo.account(tech.id).suspended_at is None
    assert [a.id for a in repo.all_accounts()] == [owner.id, tech.id]


def test_register_machine_inserts_then_updates_label_and_seen(repo):
    m = repo.register_machine(FP, KEY, "m-0042", T0)
    assert m.fingerprint == FP and m.public_key_b64 == KEY and m.owner_id is None
    assert m.registered_at == T0 and m.last_seen_at == T0
    assert m.claim_code_hash is None and m.claimed_at is None
    later = T0 + timedelta(hours=1)
    again = repo.register_machine(FP, KEY, "renamed", later)
    assert again.label == "renamed" and again.registered_at == T0
    assert again.last_seen_at == later
    assert repo.machine(FP) == again
    assert repo.machine(FP_2) is None


def test_touch_seen_and_all_machines(repo):
    repo.register_machine(FP_2, KEY, "b", T0)
    repo.register_machine(FP, KEY, "a", T0)
    repo.touch_seen(FP, T0 + timedelta(minutes=5))
    assert repo.machine(FP).last_seen_at == T0 + timedelta(minutes=5)
    assert [m.label for m in repo.all_machines()] == ["a", "b"]


def test_resolve_short_prefix(repo):
    repo.register_machine(FP, KEY, "a", T0)
    assert repo.resolve_short(FP[:16]).fingerprint == FP
    assert repo.resolve_short("0" * 16) is None


def test_resolve_short_ambiguous_is_none(repo):
    repo.register_machine("1234567890abcdef" + "0" * 48, KEY, "a", T0)
    repo.register_machine("1234567890abcdef" + "1" * 48, KEY, "b", T0)
    assert repo.resolve_short("1234567890abcdef") is None


def test_set_claim_code_stores_and_clears(repo):
    repo.register_machine(FP, KEY, "a", T0)
    expiry = T0 + timedelta(days=7)
    repo.set_claim_code(FP, "hash", expiry)
    m = repo.machine(FP)
    assert m.claim_code_hash == "hash" and m.claim_code_expires_at == expiry
    repo.set_claim_code(FP, None, None)
    m = repo.machine(FP)
    assert m.claim_code_hash is None and m.claim_code_expires_at is None


def test_claim_sets_owner_clears_code_and_drops_memberships(repo):
    old, tech = _accounts(repo)
    new = repo.upsert_account_from_login("sub-n", "new@example.com", "Nell", T0)
    repo.register_machine(FP, KEY, "a", T0)
    repo.set_claim_code(FP, "hash", T0 + timedelta(days=7))
    assert repo.claim_machine(FP, old.id, T0) is True
    repo.grant_tech(tech.id, FP, old.id, T0)
    claimed = repo.machine(FP)
    assert claimed.owner_id == old.id and claimed.claimed_at == T0
    assert claimed.claim_code_hash is None and claimed.claim_code_expires_at is None

    later = T0 + timedelta(days=1)
    assert repo.claim_machine(FP, new.id, later) is True
    assert repo.machine(FP).owner_id == new.id
    assert repo.memberships(FP) == []
    assert repo.access_for(tech.id, FP) is None


def test_claim_with_stale_expected_hash_is_false_and_unchanged(repo):
    owner, _ = _accounts(repo)
    repo.register_machine(FP, KEY, "a", T0)
    repo.set_claim_code(FP, "rotated", T0 + timedelta(days=1))
    assert repo.claim_machine(FP, owner.id, T0, expected_hash="old") is False
    m = repo.machine(FP)
    assert m.owner_id is None and m.claim_code_hash == "rotated"
    repo.set_claim_code(FP, None, None)
    assert repo.claim_machine(FP, owner.id, T0, expected_hash="old") is False
    assert repo.machine(FP).owner_id is None


def test_claim_with_expected_hash_matching_succeeds(repo):
    owner, _ = _accounts(repo)
    repo.register_machine(FP, KEY, "a", T0)
    repo.set_claim_code(FP, "h", T0 + timedelta(days=1))
    assert repo.claim_machine(FP, owner.id, T0, expected_hash="h") is True
    m = repo.machine(FP)
    assert m.owner_id == owner.id and m.claim_code_hash is None


def test_claim_with_expected_hash_refuses_expired_code(repo):
    owner, _ = _accounts(repo)
    repo.register_machine(FP, KEY, "a", T0)
    repo.set_claim_code(FP, "h", T0 + timedelta(minutes=1))
    later = T0 + timedelta(minutes=5)
    assert repo.claim_machine(FP, owner.id, later, expected_hash="h") is False
    assert repo.machine(FP).owner_id is None


def test_claim_unknown_machine_is_false(repo):
    owner, _ = _accounts(repo)
    assert repo.claim_machine(FP, owner.id, T0) is False


def test_claim_is_atomic_when_membership_delete_fails(repo, monkeypatch):
    owner, tech = _accounts(repo)
    other = repo.upsert_account_from_login("sub-x", "x@example.com", "Xan", T0)
    repo.register_machine(FP, KEY, "a", T0)
    repo.claim_machine(FP, owner.id, T0)
    repo.grant_tech(tech.id, FP, owner.id, T0)
    repo.set_claim_code(FP, "hash", T0 + timedelta(days=1))
    monkeypatch.setattr(sqlite, "DELETE_MEMBERSHIPS_SQL", "DELETE FROM no_such_table")
    with pytest.raises(sqlite3.OperationalError):
        repo.claim_machine(FP, other.id, T0 + timedelta(hours=1))
    m = repo.machine(FP)
    assert m.owner_id == owner.id and m.claimed_at == T0
    assert m.claim_code_hash == "hash"
    assert repo.access_for(tech.id, FP) == "tech"


def test_unbind_machine_clears_owner_claim_and_memberships(repo):
    owner, tech = _accounts(repo)
    repo.register_machine(FP, KEY, "a", T0)
    repo.claim_machine(FP, owner.id, T0)
    repo.grant_tech(tech.id, FP, owner.id, T0)
    repo.unbind_machine(FP)
    m = repo.machine(FP)
    assert m.owner_id is None and m.claimed_at is None
    assert repo.memberships(FP) == []


def test_access_and_machines_for_account(repo):
    owner, tech = _accounts(repo)
    repo.register_machine(FP, KEY, "a", T0)
    repo.register_machine(FP_2, KEY, "b", T0)
    repo.claim_machine(FP, owner.id, T0)
    repo.claim_machine(FP_2, tech.id, T0)
    repo.grant_tech(tech.id, FP, owner.id, T0)
    assert repo.access_for(owner.id, FP) == "owner"
    assert repo.access_for(tech.id, FP) == "tech"
    assert repo.access_for(tech.id, FP_2) == "owner"
    assert repo.access_for(owner.id, FP_2) is None
    mine = repo.machines_for_account(tech.id)
    assert [(a.machine.fingerprint, a.role) for a in mine] == [
        (FP, "tech"),
        (FP_2, "owner"),
    ]
    assert [a.role for a in repo.machines_for_account(owner.id)] == ["owner"]


def test_grant_is_idempotent_revoke_removes_and_memberships_join_accounts(repo):
    owner, tech = _accounts(repo)
    repo.register_machine(FP, KEY, "a", T0)
    repo.claim_machine(FP, owner.id, T0)
    repo.grant_tech(tech.id, FP, owner.id, T0)
    repo.grant_tech(tech.id, FP, owner.id, T0 + timedelta(days=1))
    [(membership, account)] = repo.memberships(FP)
    assert membership.role == "tech" and membership.granted_by == owner.id
    assert membership.granted_at == T0 and membership.account_id == tech.id
    assert account.email == "tech@example.com"
    repo.revoke(tech.id, FP)
    assert repo.memberships(FP) == []
    assert repo.access_for(tech.id, FP) is None


def test_create_invitation_round_trips_scope_and_defaults_to_seven_days(repo):
    owner, _ = _accounts(repo)
    fleet = repo.create_invitation(owner.id, "Tech@Example.com", "fleet", T0)
    assert fleet.scope == "fleet" and fleet.email == "tech@example.com"
    assert fleet.expires_at == T0 + timedelta(days=7)
    assert fleet.accepted_by is None and len(fleet.token) >= 43
    some = repo.create_invitation(
        owner.id, "t@example.com", (FP, FP_2), T0, ttl=timedelta(hours=1)
    )
    assert some.scope == (FP, FP_2) and some.expires_at == T0 + timedelta(hours=1)
    assert repo.invitation(some.token) == some
    assert repo.invitation("missing") is None


def test_invitations_by_lists_pending_and_filters_by_machine(repo):
    owner, tech = _accounts(repo)
    fleet = repo.create_invitation(owner.id, "a@example.com", "fleet", T0)
    one = repo.create_invitation(owner.id, "b@example.com", (FP,), T0)
    two = repo.create_invitation(owner.id, "c@example.com", (FP_2,), T0)
    assert {i.token for i in repo.invitations_by(owner.id, None, T0)} == {
        fleet.token,
        one.token,
        two.token,
    }
    assert {i.token for i in repo.invitations_by(owner.id, FP, T0)} == {
        fleet.token,
        one.token,
    }
    repo.accept_invitation(one.token, tech.id, T0)
    assert one.token not in {i.token for i in repo.invitations_by(owner.id, None, T0)}
    assert repo.invitations_by(tech.id, None, T0) == []


def test_accept_invitation_grants_listed_machines_once(repo):
    owner, tech = _accounts(repo)
    for fp in (FP, FP_2):
        repo.register_machine(fp, KEY, fp[:2], T0)
        repo.claim_machine(fp, owner.id, T0)
    invite = repo.create_invitation(owner.id, "tech@example.com", (FP,), T0)
    assert repo.accept_invitation(invite.token, tech.id, T0) == [FP]
    accepted = repo.invitation(invite.token)
    assert accepted.accepted_by == tech.id and accepted.accepted_at == T0
    assert repo.access_for(tech.id, FP) == "tech"
    assert repo.access_for(tech.id, FP_2) is None
    assert repo.accept_invitation(invite.token, tech.id, T0) == []


def test_accept_invitation_refuses_expired_and_unknown(repo):
    owner, tech = _accounts(repo)
    repo.register_machine(FP, KEY, "a", T0)
    repo.claim_machine(FP, owner.id, T0)
    invite = repo.create_invitation(owner.id, "t@example.com", "fleet", T0)
    late = T0 + timedelta(days=8)
    assert repo.accept_invitation(invite.token, tech.id, late) == []
    assert repo.access_for(tech.id, FP) is None
    assert repo.invitation(invite.token).accepted_by is None
    assert repo.accept_invitation("missing", tech.id, T0) == []


def test_fleet_scope_expands_only_current_machines(repo):
    owner, tech = _accounts(repo)
    repo.register_machine(FP, KEY, "a", T0)
    repo.claim_machine(FP, owner.id, T0)
    invite = repo.create_invitation(owner.id, "tech@example.com", "fleet", T0)
    repo.register_machine(FP_2, KEY, "b", T0)
    repo.claim_machine(FP_2, owner.id, T0)
    assert repo.accept_invitation(invite.token, tech.id, T0) == [FP, FP_2]
    # A machine claimed after acceptance is not shared.
    third = "ef" * 32
    repo.register_machine(third, KEY, "c", T0)
    repo.claim_machine(third, owner.id, T0)
    assert repo.access_for(tech.id, third) is None


def test_listed_scope_ignores_machines_the_inviter_no_longer_owns(repo):
    owner, tech = _accounts(repo)
    repo.register_machine(FP, KEY, "a", T0)
    repo.claim_machine(FP, owner.id, T0)
    invite = repo.create_invitation(owner.id, "tech@example.com", (FP, FP_2), T0)
    repo.unbind_machine(FP)
    assert repo.accept_invitation(invite.token, tech.id, T0) == []


def test_cancel_invitation_removes_it(repo):
    owner, _ = _accounts(repo)
    invite = repo.create_invitation(owner.id, "t@example.com", "fleet", T0)
    repo.cancel_invitation(invite.token)
    assert repo.invitation(invite.token) is None


def test_migrations_idempotent_on_reopen(tmp_path):
    path = tmp_path / "cloud.db"
    first = SqliteRepository(path)
    account = first.upsert_account_from_login("sub", "a@example.com", "A", T0)
    first.register_machine(FP, KEY, "a", T0)
    second = SqliteRepository(path)
    assert second.account(account.id) == account
    assert second.machine(FP) is not None


def test_foreign_keys_are_enforced(repo):
    with pytest.raises(sqlite3.IntegrityError):
        repo.create_invitation("no-such-account", "t@example.com", "fleet", T0)


def test_login_with_email_owned_by_another_sub_raises_email_conflict(repo):
    first = repo.upsert_account_from_login("sub-1", "a@example.com", "Ann", T0)
    other = repo.upsert_account_from_login("sub-2", "b@example.com", "Bob", T0)
    with pytest.raises(EmailConflict):
        repo.upsert_account_from_login("sub-3", "A@example.com", "Cy", T0)
    with pytest.raises(EmailConflict):
        repo.upsert_account_from_login("sub-2", "a@example.com", "Bob", T0)
    assert repo.account(first.id) == first
    assert repo.account(other.id) == other
    assert len(repo.all_accounts()) == 2


def test_claim_unknown_account_is_false_and_unchanged(repo):
    owner, _ = _accounts(repo)
    repo.register_machine(FP, KEY, "a", T0)
    repo.set_claim_code(FP, "hash", T0 + timedelta(days=1))
    before = repo.machine(FP)
    assert repo.claim_machine(FP, "no-such-account", T0) is False
    assert repo.machine(FP) == before
    assert repo.claim_machine(FP_2, owner.id, T0) is False


def test_grant_tech_unknown_account_or_machine_raises_lookup_error(repo):
    owner, _ = _accounts(repo)
    repo.register_machine(FP, KEY, "a", T0)
    with pytest.raises(LookupError, match="account"):
        repo.grant_tech("no-such-account", FP, owner.id, T0)
    with pytest.raises(LookupError, match="machine"):
        repo.grant_tech(owner.id, FP_2, owner.id, T0)
    assert repo.memberships(FP) == []


def test_invitations_by_excludes_expired_and_accepted(repo):
    owner, tech = _accounts(repo)
    live = repo.create_invitation(owner.id, "a@example.com", "fleet", T0)
    old = repo.create_invitation(
        owner.id, "b@example.com", "fleet", T0, ttl=timedelta(hours=1)
    )
    done = repo.create_invitation(owner.id, "c@example.com", "fleet", T0)
    repo.accept_invitation(done.token, tech.id, T0)
    later = T0 + timedelta(hours=2)
    assert {i.token for i in repo.invitations_by(owner.id, None, later)} == {live.token}
    assert {i.token for i in repo.invitations_by(owner.id, None, T0)} == {
        live.token,
        old.token,
    }
    assert old.expires_at == T0 + timedelta(hours=1)
    assert repo.invitations_by(owner.id, None, old.expires_at) == [live]


def test_set_claim_code_ignores_the_hash_a_claim_consumed(repo):
    owner, _ = _accounts(repo)
    repo.register_machine(FP, KEY, "a", T0)
    repo.set_claim_code(FP, "h", None)
    assert repo.claim_machine(FP, owner.id, T0, expected_hash="h") is True
    repo.set_claim_code(FP, "h", None)  # the edge re-sending the consumed code
    assert repo.machine(FP).claim_code_hash is None
    assert repo.machine(FP).consumed_claim_hash == "h"
    repo.set_claim_code(FP, "fresh", None)
    m = repo.machine(FP)
    assert m.claim_code_hash == "fresh" and m.consumed_claim_hash is None


def test_reclaim_by_the_same_owner_keeps_techs(repo):
    owner, tech = _accounts(repo)
    repo.register_machine(FP, KEY, "a", T0)
    repo.claim_machine(FP, owner.id, T0)
    repo.grant_tech(tech.id, FP, owner.id, T0)
    assert repo.claim_machine(FP, owner.id, T0 + timedelta(hours=1)) is True
    assert repo.access_for(tech.id, FP) == "tech"
