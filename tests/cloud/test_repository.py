from datetime import UTC, datetime, timedelta

from dataclasses import replace
import sqlite3

import pytest

from ice_contracts.cloud.report import FaultView

from ice_cloud.store import (
    AlertEventRow,
    AlertState,
    EmailConflict,
    SqliteRepository,
    sqlite,
)
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
    assert repo.consumed_claim_hashes(FP) == {"h"}
    repo.set_claim_code(FP, "fresh", None)
    assert repo.machine(FP).claim_code_hash == "fresh"
    # A fresh hash does not make a consumed one usable again.
    assert repo.consumed_claim_hashes(FP) == {"h"}
    repo.set_claim_code(FP, "h", None)
    assert repo.machine(FP).claim_code_hash == "fresh"
    repo.set_claim_code(FP, None, None)
    repo.set_claim_code(FP, "h", None)
    assert repo.machine(FP).claim_code_hash is None


def test_unbind_is_the_only_thing_that_forgets_consumed_hashes(repo):
    owner, _ = _accounts(repo)
    repo.register_machine(FP, KEY, "a", T0)
    repo.set_claim_code(FP, "h", None)
    repo.claim_machine(FP, owner.id, T0, expected_hash="h")
    repo.unbind_machine(FP)
    assert repo.consumed_claim_hashes(FP) == set()
    repo.set_claim_code(FP, "h", None)
    assert repo.machine(FP).claim_code_hash == "h"


def test_reclaim_by_the_same_owner_keeps_techs(repo):
    owner, tech = _accounts(repo)
    repo.register_machine(FP, KEY, "a", T0)
    repo.claim_machine(FP, owner.id, T0)
    repo.grant_tech(tech.id, FP, owner.id, T0)
    assert repo.claim_machine(FP, owner.id, T0 + timedelta(hours=1)) is True
    assert repo.access_for(tech.id, FP) == "tech"


# alerts


def _event(kind="raised", at=T0, key="WTR-104:machine", gate="safety", fp=FP):
    return AlertEventRow(
        fingerprint=fp,
        kind=kind,
        at=at,
        key=key,
        code="WTR-104",
        severity="critical",
        gate=gate,
        description="Sump high",
        sku=None,
        product=None,
    )


def _fault(key="ICE-10LB"):
    return FaultView(
        key=key,
        sku="ICE-10LB",
        product="Bag of Ice",
        code="ICE-301",
        severity="lockout",
        scope="product",
        description="Ice bin low",
        since_seconds=412.3,
        gate="fulfillment",
        pending_sale=None,
    )


def _state(fp=FP, keys=("A",), offline_since=None):
    return AlertState(fp, tuple(_fault(k) for k in keys), T0, 60, offline_since)


def _claimed(repo):
    owner, tech = _accounts(repo)
    repo.register_machine(FP, KEY, "a", T0)
    repo.claim_machine(FP, owner.id, T0)
    repo.grant_tech(tech.id, FP, owner.id, T0)
    return owner, tech


def test_alert_state_put_get_replace_delete(repo):
    repo.register_machine(FP, KEY, "a", T0)
    assert repo.alert_state(FP) is None
    repo.put_alert_state(_state(keys=("B", "A")))
    got = repo.alert_state(FP)
    assert got == _state(keys=("B", "A"))
    assert got.faults[0].product == "Bag of Ice" and got.keys == {"A", "B"}
    later = AlertState(FP, (), T0 + timedelta(minutes=1), 120, T0)
    repo.put_alert_state(later)
    assert repo.alert_state(FP) == later
    repo.delete_alert_state(FP)
    assert repo.alert_state(FP) is None


def test_claimed_machines_with_state_only_claimed(repo):
    owner, _ = _accounts(repo)
    repo.register_machine(FP, KEY, "a", T0)
    repo.register_machine(FP_2, KEY, "b", T0)
    repo.claim_machine(FP, owner.id, T0)
    repo.put_alert_state(_state(FP))
    repo.put_alert_state(_state(FP_2))
    found = repo.claimed_machines_with_state()
    assert [(m.fingerprint, s.fingerprint) for m, s in found] == [(FP, FP)]


def test_record_and_list_alert_events_newest_first(repo):
    repo.register_machine(FP, KEY, "a", T0)
    stored = repo.record_alert_events(
        [_event(at=T0), _event(kind="cleared", at=T0 + timedelta(minutes=1))]
    )
    assert [e.id for e in stored] == sorted(e.id for e in stored)
    assert stored[0].kind == "raised" and stored[0].at == T0
    listed = repo.alert_events(FP, limit=10)
    assert listed == list(reversed(stored))
    assert repo.alert_events(FP, limit=1) == [stored[1]]
    assert repo.alert_events(FP_2, limit=10) == []


def test_alert_count_since_ignores_cleared_and_old(repo):
    repo.register_machine(FP, KEY, "a", T0)
    repo.record_alert_events(
        [
            _event(at=T0 - timedelta(hours=30)),
            _event(at=T0 - timedelta(hours=1)),
            _event(kind="offline", at=T0),
            _event(kind="cleared", at=T0),
        ]
    )
    assert repo.alert_count_since(FP, T0 - timedelta(hours=24)) == 2


def test_people_for_alerts_owner_first_then_techs_with_muted(repo):
    owner, tech = _claimed(repo)
    other = repo.upsert_account_from_login("sub-x", "x@example.com", "Xi", T0)
    repo.grant_tech(other.id, FP, owner.id, T0 - timedelta(days=1))
    repo.set_muted(tech.id, FP, True)
    people = repo.people_for_alerts(FP)
    assert [(r.account_id, r.role, m) for r, m in people] == [
        (owner.id, "owner", False),
        (other.id, "tech", False),
        (tech.id, "tech", True),
    ]
    assert people[0][0].email == "owner@example.com"
    assert repo.people_for_alerts(FP_2) == []


def test_people_for_alerts_excludes_suspended(repo):
    owner, tech = _claimed(repo)
    repo.set_suspended(tech.id, T0)
    assert [r.account_id for r, _ in repo.people_for_alerts(FP)] == [owner.id]
    repo.set_suspended(owner.id, T0)
    assert repo.people_for_alerts(FP) == []


def test_set_muted_and_is_muted(repo):
    owner, tech = _claimed(repo)
    assert repo.is_muted(tech.id, FP) is False
    repo.set_muted(tech.id, FP, True)
    assert repo.is_muted(tech.id, FP) is True
    assert repo.is_muted(owner.id, FP) is False
    repo.set_muted(tech.id, FP, False)
    assert repo.is_muted(tech.id, FP) is False


def test_create_delivery_and_mark_increments_attempts(repo):
    owner, _ = _claimed(repo)
    events = repo.record_alert_events([_event(), _event(key="k2")])
    ids = [e.id for e in events]
    d = repo.create_delivery(FP, owner.id, ids, "pending", T0)
    assert d.event_ids == tuple(ids) and d.status == "pending"
    assert d.attempted_at == T0 and d.sent_at is None and d.attempts == 0
    repo.mark_delivery(
        d.id, status="failed", now=T0 + timedelta(minutes=1), error="boom"
    )
    (failed,) = repo.pending_deliveries(before=T0 + timedelta(days=1))
    assert failed.status == "failed" and failed.error == "boom"
    assert failed.attempts == 1 and failed.sent_at is None
    repo.mark_delivery(d.id, status="sent", now=T0 + timedelta(minutes=2))
    assert repo.pending_deliveries(before=T0 + timedelta(days=1)) == []
    assert repo.last_sent_at(FP, owner.id) == T0 + timedelta(minutes=2)


def test_pending_deliveries_before_cutoff(repo):
    owner, _ = _claimed(repo)
    e = repo.record_alert_events([_event()])[0]
    due = repo.create_delivery(FP, owner.id, [e.id], "pending", T0)
    repo.create_delivery(FP, owner.id, [e.id], "pending", T0 + timedelta(minutes=5))
    repo.create_delivery(FP, owner.id, [e.id], "muted", T0)
    repo.create_delivery(FP, owner.id, [e.id], "sent", T0)
    assert [d.id for d in repo.pending_deliveries(before=T0)] == [due.id]
    assert len(repo.pending_deliveries(before=T0 + timedelta(minutes=5))) == 2


def test_pending_deliveries_excludes_exhausted_failed(repo):
    owner, _ = _claimed(repo)
    e = repo.record_alert_events([_event()])[0]
    d = repo.create_delivery(FP, owner.id, [e.id], "pending", T0)
    for n in range(4):
        repo.mark_delivery(d.id, status="failed", now=T0, error="x")
        later = T0 + timedelta(days=1)
        assert [x.attempts for x in repo.pending_deliveries(before=later)] == [n + 1]
    repo.mark_delivery(d.id, status="failed", now=T0, error="x")
    assert repo.pending_deliveries(before=T0 + timedelta(days=1)) == []


def test_failed_delivery_backs_off_exponentially(repo):
    owner, _ = _claimed(repo)
    e = repo.record_alert_events([_event()])[0]
    d = repo.create_delivery(FP, owner.id, [e.id], "pending", T0)
    for attempts, minutes in [(1, 2), (2, 4), (3, 8), (4, 16)]:
        repo.mark_delivery(d.id, status="failed", now=T0, error="x")
        just_before = T0 + timedelta(minutes=minutes) - timedelta(seconds=1)
        assert repo.pending_deliveries(before=just_before) == []
        due = repo.pending_deliveries(before=T0 + timedelta(minutes=minutes))
        assert [x.attempts for x in due] == [attempts]


def test_last_sent_at_and_cleared_since_last_sent(repo):
    owner, tech = _claimed(repo)
    first, cleared_old, cleared_new = repo.record_alert_events(
        [
            _event(at=T0),
            _event(kind="cleared", at=T0 + timedelta(minutes=1)),
            _event(kind="cleared", at=T0 + timedelta(minutes=30)),
        ]
    )
    assert repo.last_sent_at(FP, owner.id) is None
    assert repo.cleared_since_last_sent(FP, owner.id) == [cleared_old, cleared_new]
    d = repo.create_delivery(FP, owner.id, [first.id], "pending", T0)
    repo.mark_delivery(d.id, status="sent", now=T0 + timedelta(minutes=10))
    assert repo.last_sent_at(FP, owner.id) == T0 + timedelta(minutes=10)
    assert repo.cleared_since_last_sent(FP, owner.id) == [cleared_new]
    assert repo.last_sent_at(FP, tech.id) is None


def test_cleared_since_last_sent_bounded_by_access_start(repo):
    owner, tech = _claimed(repo)  # both gain access at T0
    early = repo.record_alert_events(
        [_event(kind="cleared", at=T0 + timedelta(minutes=1))]
    )[0]
    late_tech = repo.upsert_account_from_login("sub-l", "late@example.com", "L", T0)
    repo.grant_tech(late_tech.id, FP, owner.id, T0 + timedelta(minutes=5))
    stranger = repo.upsert_account_from_login("sub-s", "s@example.com", "S", T0)
    after = repo.record_alert_events(
        [_event(kind="cleared", at=T0 + timedelta(minutes=10))]
    )[0]
    assert repo.cleared_since_last_sent(FP, owner.id) == [early, after]
    assert repo.cleared_since_last_sent(FP, late_tech.id) == [after]
    assert repo.cleared_since_last_sent(FP, stranger.id) == []


def test_deliveries_for_events_groups_by_event(repo):
    owner, tech = _claimed(repo)
    a, b, c = repo.record_alert_events([_event(key=k) for k in "abc"])
    d1 = repo.create_delivery(FP, owner.id, [a.id, b.id], "sent", T0)
    d2 = repo.create_delivery(FP, tech.id, [b.id], "muted", T0)
    got = repo.deliveries_for_events([a.id, b.id, c.id])
    assert got == {a.id: [d1], b.id: [d1, d2], c.id: []}
    assert repo.deliveries_for_events([]) == {}


def _seed_alerts(repo, owner, tech):
    repo.put_alert_state(_state())
    repo.set_muted(tech.id, FP, True)
    e = repo.record_alert_events([_event()])[0]
    repo.create_delivery(FP, owner.id, [e.id], "sent", T0)
    return e


def test_claim_change_resets_alert_state_prefs_and_history(repo):
    owner, tech = _claimed(repo)
    e = _seed_alerts(repo, owner, tech)
    newer = repo.upsert_account_from_login("sub-n", "new@example.com", "Nia", T0)
    assert repo.claim_machine(FP, newer.id, T0 + timedelta(hours=1)) is True
    assert repo.alert_state(FP) is None
    assert repo.is_muted(tech.id, FP) is False
    assert repo.alert_events(FP, limit=5) == []
    assert repo.deliveries_for_events([e.id]) == {e.id: []}
    assert repo.last_sent_at(FP, owner.id) is None


def test_reclaim_same_owner_keeps_state(repo):
    owner, tech = _claimed(repo)
    _seed_alerts(repo, owner, tech)
    assert repo.claim_machine(FP, owner.id, T0 + timedelta(hours=1)) is True
    assert repo.alert_state(FP) == _state()
    assert repo.is_muted(tech.id, FP) is True


def test_revoke_deletes_pref(repo):
    owner, tech = _claimed(repo)
    repo.set_muted(tech.id, FP, True)
    repo.set_muted(owner.id, FP, True)
    repo.revoke(tech.id, FP)
    assert repo.is_muted(tech.id, FP) is False
    assert repo.is_muted(owner.id, FP) is True


def test_unbind_deletes_everything_alert(repo):
    owner, tech = _claimed(repo)
    e = _seed_alerts(repo, owner, tech)
    repo.unbind_machine(FP)
    assert repo.alert_state(FP) is None
    assert repo.alert_events(FP, limit=5) == []
    assert repo.deliveries_for_events([e.id]) == {e.id: []}
    assert repo.pending_deliveries(before=T0 + timedelta(days=1)) == []
    assert repo.last_sent_at(FP, owner.id) is None
    assert repo.is_muted(tech.id, FP) is False


def test_migrations_apply_on_existing_db(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    n_old = len(sqlite.MIGRATIONS) - 3
    for script in sqlite.MIGRATIONS[:n_old]:
        old.executescript(script)
    old.execute("INSERT INTO schema_version (version) VALUES (?)", (n_old,))
    old.execute(
        "INSERT INTO machines (fingerprint, public_key, label, registered_at,"
        " last_seen_at) VALUES (?, ?, 'a', ?, ?)",
        (FP, KEY, T0.isoformat(), T0.isoformat()),
    )
    old.commit()
    old.close()
    repo = SqliteRepository(path)
    assert repo.machine(FP).label == "a"
    repo.put_alert_state(_state())
    assert repo.alert_state(FP) == _state()
    assert repo.record_alert_events([_event()])[0].id == 1


def test_alert_events_by_ids_ordered_and_skips_unknown(repo):
    _claimed(repo)
    a, b, c = repo.record_alert_events([_event(key=k) for k in "abc"])
    assert repo.alert_events_by_ids([c.id, a.id, 9999]) == [a, c]
    assert repo.alert_events_by_ids([]) == []


def test_record_alert_pass_commits_events_and_state_together(repo):
    _claimed(repo)
    stored = repo.record_alert_pass([_event()], _state())
    assert len(stored) == 1 and stored[0].id
    assert repo.alert_state(FP) == _state()
    assert repo.alert_events(FP, limit=10) == stored


def test_record_alert_pass_rolls_back_events_when_state_write_fails(repo):
    _claimed(repo)
    bad = _state()
    object.__setattr__(bad, "report_interval_seconds", object())
    with pytest.raises(Exception):
        repo.record_alert_pass([_event()], bad)
    assert repo.alert_events(FP, limit=10) == []
    assert repo.alert_state(FP) is None


def test_mark_offline_if_unchanged(repo):
    _claimed(repo)
    repo.put_alert_state(_state())
    offline = _event(kind="offline", at=T0 + timedelta(hours=1))
    stored = repo.mark_offline_if_unchanged(FP, _state().last_report_at, offline)
    assert stored is not None and stored.kind == "offline"
    assert repo.alert_state(FP).offline_since == offline.at
    assert [e.id for e in repo.alert_events(FP, limit=5)] == [stored.id]
    # already offline: nothing more
    assert repo.mark_offline_if_unchanged(FP, _state().last_report_at, offline) is None
    assert len(repo.alert_events(FP, limit=5)) == 1


def test_mark_offline_if_unchanged_skips_when_report_arrived(repo):
    _claimed(repo)
    repo.put_alert_state(_state())
    newer = replace(_state(), last_report_at=T0 + timedelta(minutes=30))
    repo.put_alert_state(newer)
    offline = _event(kind="offline", at=T0 + timedelta(hours=1))
    assert repo.mark_offline_if_unchanged(FP, _state().last_report_at, offline) is None
    assert repo.alert_state(FP) == newer
    assert repo.alert_events(FP, limit=5) == []


def test_delivery_health_follows_most_recent_attempt(repo):
    owner, tech = _claimed(repo)
    e = repo.record_alert_events([_event()])[0]
    assert repo.delivery_health(FP) is True
    first = repo.create_delivery(FP, owner.id, [e.id], "pending", T0)
    repo.mark_delivery(first.id, status="failed", now=T0 + timedelta(minutes=1))
    assert repo.delivery_health(FP) is False
    repo.create_delivery(FP, tech.id, [e.id], "muted", T0 + timedelta(minutes=2))
    assert repo.delivery_health(FP) is False  # a muted row is not an attempt
    second = repo.create_delivery(FP, owner.id, [e.id], "pending", T0)
    repo.mark_delivery(second.id, status="sent", now=T0 + timedelta(minutes=3))
    assert repo.delivery_health(FP) is True
    repo.mark_delivery(first.id, status="failed", now=T0 + timedelta(minutes=4))
    assert repo.delivery_health(FP) is False
