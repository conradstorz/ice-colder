"""The pure fleet view model."""

from __future__ import annotations

from datetime import timedelta

from ice_cloud import fleet_view, portal
from ice_cloud.fleet_view import (
    ATTENTION_LIMIT,
    MachineStatus,
    build_fleet_view,
    classify,
    filter_rows,
    percentages,
)
from ice_cloud.store.repository import Account, Machine, Membership
from ice_contracts.cloud.report import FaultView
from ice_ui.bundle import ui_digest
from tests.cloud.reports import _FAULT, NOW, make_report

DIGEST = ui_digest()


def _machine(fp, label, owner_id=None, **kw):
    base = dict(
        fingerprint=fp,
        public_key_b64="k",
        label=label,
        owner_id=owner_id,
        registered_at=NOW - timedelta(days=2),
        last_seen_at=NOW,
        claim_code_hash=None,
        claim_code_expires_at=None,
        claimed_at=None,
    )
    base.update(kw)
    return Machine(**base)


def _account(aid, email="a@x.io", suspended=False):
    return Account(
        id=aid,
        google_sub=aid,
        email=email,
        name=aid,
        created_at=NOW,
        suspended_at=NOW if suspended else None,
    )


def _report(age=0, **kw):
    kw.setdefault("digest", DIGEST)
    return make_report(sent_at=NOW - timedelta(seconds=age), **kw)


def _safety_report(**kw):
    """make_report's `faults` is a bool, so swap in a safety-gate fault afterwards."""
    base = _report(**kw)
    fault = FaultView.model_validate(
        {**_FAULT, "gate": "safety", "pending_sale": None, "code": "WTR-103"}
    )
    return base.model_copy(update={"faults": [fault]})


def _view(machines, reports, accounts=(), memberships=(), alerts=None):
    return build_fleet_view(
        machines=machines,
        accounts=list(accounts),
        memberships=list(memberships),
        reports=reports,
        alert_counts=alerts or {},
        installed_digest=DIGEST,
        now=NOW,
    )


def test_classify_boundary_exactly_three_intervals_is_online():
    assert classify(_report(age=180, interval=60), NOW) is MachineStatus.online


def test_classify_one_second_past_is_silent():
    assert classify(_report(age=181, interval=60), NOW) is MachineStatus.silent


def test_classify_none_is_never_reported():
    assert classify(None, NOW) is MachineStatus.never_reported


def _small_fleet():
    ms = [
        _machine("u1" * 32, "unclaimed-online"),
        _machine("s1" * 32, "claimed-silent", owner_id="o1"),
        _machine("n1" * 32, "claimed-never", owner_id="o1"),
        _machine("f1" * 32, "claimed-faulted", owner_id="o2"),
    ]
    reports = {
        "u1" * 32: _report(faults=False),
        "s1" * 32: _report(age=1000, faults=False),
        "f1" * 32: _report(),
    }
    return ms, reports, [_account("o1", "o1@x.io"), _account("o2", "o2@x.io", True)]


def test_headlines_for_small_fleet():
    ms, reports, accts = _small_fleet()
    ms.append(_machine("u2" * 32, "unclaimed-extra", owner_id=None))
    v = _view(ms, reports, accts)
    machines, owners, unclaimed, silent = v.headlines
    assert (machines.label, machines.value, machines.sub) == (
        "Machines",
        5,
        "3 claimed · 2 unclaimed",
    )
    assert machines.url == "/admin/machines"
    assert (owners.value, owners.sub) == (2, "2 accounts · 1 suspended")
    assert (unclaimed.label, unclaimed.value, unclaimed.tone) == (
        "Unclaimed calling in",
        1,
        "bad",
    )
    assert unclaimed.sub == "1 unclaimed silent or never reported"
    assert unclaimed.url == "/admin/machines?claimed=no&status=online"
    assert (silent.value, silent.tone, silent.sub) == (1, "bad", "2 never reported")
    assert silent.url == "/admin/machines?status=silent"


def test_secondary_revenue_excludes_silent():
    ms, reports, accts = _small_fleet()
    v = _view(ms, reports, accts, alerts={"s1" * 32: 2, "f1" * 32: 1})
    sec = {h.label: h for h in v.secondary}
    assert list(sec) == [
        "Faulted",
        "Payment blocked",
        "Alerts (24 h)",
        "Revenue (24 h)",
        "Uptime (24 h)",
        "Software",
    ]
    assert sec["Revenue (24 h)"].value == "$84.00"
    assert sec["Revenue (24 h)"].sub == "28 products out · 2 vends failed"
    assert sec["Uptime (24 h)"].value == "99.3%"
    assert sec["Uptime (24 h)"].sub == "across 2 online machines"
    assert sec["Faulted"].value == 1
    assert sec["Faulted"].tone == "warn"
    assert sec["Alerts (24 h)"].value == 3
    assert sec["Software"].value == 1
    assert sec["Software"].sub == "0 with a different UI"


def test_secondary_empty_fleet_uptime_dash_and_safety_tone():
    assert _view([], {}).secondary[4].value == "—"
    m = _machine("a1" * 32, "x")
    v = _view([m], {m.fingerprint: _safety_report()})
    faulted = v.secondary[0]
    assert (faulted.tone, faulted.sub) == ("bad", "of which 1 safety")


def test_status_mix_percentages_sum_to_100_and_zero_for_empty():
    ms, reports, accts = _small_fleet()
    mix = _view(ms, reports, accts).mix
    assert (mix.online_ok, mix.online_faulted, mix.silent, mix.never_reported) == (
        1,
        1,
        1,
        1,
    )
    assert (
        mix.online_ok_pct
        + mix.online_faulted_pct
        + mix.silent_pct
        + (mix.never_reported_pct)
        == 100
    )
    empty = _view([], {}).mix
    assert empty.online_ok_pct == empty.silent_pct == 0


def test_percentages_largest_remainder():
    p = percentages([1, 1, 1])
    assert sum(p) == 100
    assert sorted(p) == [33, 33, 34]
    p = percentages([2, 1, 0, 0])
    assert p == [67, 33, 0, 0]
    assert percentages([0, 0]) == [0, 0]


def test_attention_order_and_uniqueness():
    ms = [
        _machine("a1" * 32, "safe", owner_id="o1"),
        _machine("b1" * 32, "silent-unclaimed"),
        _machine("c1" * 32, "paydown", owner_id="o1"),
        _machine("d1" * 32, "unclaimed-online"),
        _machine("e1" * 32, "fine", owner_id="o1"),
    ]
    reports = {
        "a1" * 32: _safety_report(),
        "b1" * 32: _report(age=3 * 3600 + 12 * 60, faults=False),
        "c1" * 32: _report(
            faults=False,
            health={
                **_report(faults=False).health.model_dump(mode="json"),
                "payment_enabled": False,
                "payment_reasons": ["door open"],
            },
        ),
        "d1" * 32: _report(faults=False),
        "e1" * 32: _report(faults=False),
    }
    att = _view(ms, reports, [_account("o1")]).attention
    assert [a.row.label for a in att] == [
        "safe",
        "silent-unclaimed",
        "paydown",
        "unclaimed-online",
    ]
    assert att[0].reason == "WTR-103 — Ice bin low"
    assert att[1].reason == "silent for 3h 12m, expected every 60s"
    assert att[2].reason == "door open"
    assert att[3].reason == "unclaimed, calling in since 2026-10-11 14:05 UTC"
    assert [a.tone for a in att] == ["bad", "bad", "warn", "warn"]


def test_attention_silent_longest_first():
    ms = [
        _machine("a1" * 32, "short"),
        _machine("b1" * 32, "long"),
        _machine("c1" * 32, "mid"),
    ]
    reports = {
        "a1" * 32: _report(age=500, faults=False),
        "b1" * 32: _report(age=9000, faults=False),
        "c1" * 32: _report(age=2000, faults=False),
    }
    att = _view(ms, reports).attention
    assert [a.row.label for a in att] == ["long", "mid", "short"]


def test_top_fault_prefers_safety_gate_over_earlier_code():
    base = _report()
    early = FaultView.model_validate(
        {**_FAULT, "gate": "fulfillment", "pending_sale": None, "code": "AAA-100"}
    )
    safety = FaultView.model_validate(
        {**_FAULT, "gate": "safety", "pending_sale": None, "code": "WTR-103"}
    )
    report = base.model_copy(update={"faults": [early, safety]})
    m = _machine("a1" * 32, "a")
    row = _view([m], {m.fingerprint: report}).rows[0]
    assert row.top_fault.code == "WTR-103"
    assert row.has_safety_fault is True


def test_report_without_kpi_summary_contributes_nothing():
    base = _report(faults=False)
    report = base.model_copy(
        update={"kpi": base.kpi.model_copy(update={"summary": None})}
    )
    m = _machine("a1" * 32, "a")
    sec = {h.label: h for h in _view([m], {m.fingerprint: report}).secondary}
    assert sec["Revenue (24 h)"].value == "$0.00"
    assert sec["Revenue (24 h)"].sub == "0 products out · 0 vends failed"
    assert sec["Uptime (24 h)"].value == "—"


def test_owner_without_account_falls_back_to_owner_id():
    m = _machine("a1" * 32, "a", owner_id="ghost")
    v = _view([m], {m.fingerprint: _report(faults=False)})
    assert v.rows[0].owner_email == "ghost"
    assert v.headlines[1].value == 1
    (owner,) = v.owners
    assert (owner.account_id, owner.email, owner.name) == ("ghost", "ghost", "")
    assert owner.suspended is False
    assert owner.machines == 1


def test_attention_capped_at_12():
    ms = [_machine(f"{i:02x}" * 32, f"m{i:02d}") for i in range(20)]
    reports = {m.fingerprint: _report(faults=False) for m in ms}
    assert len(_view(ms, reports).attention) == ATTENTION_LIMIT == 12


def test_owner_rows_count_distinct_techs():
    ms = [
        _machine("a1" * 32, "a", owner_id="o1"),
        _machine("b1" * 32, "b", owner_id="o1"),
        _machine("c1" * 32, "c", owner_id="o2"),
    ]
    reports = {"a1" * 32: _report(), "b1" * 32: _report(age=999, faults=False)}
    mem = [
        Membership("t1", "a1" * 32, "tech", "o1", NOW),
        Membership("t1", "b1" * 32, "tech", "o1", NOW),
        Membership("t2", "b1" * 32, "tech", "o1", NOW),
    ]
    accts = [_account("o1", "z@x.io"), _account("o2", "y@x.io")]
    v = _view(ms, reports, accts, mem, alerts={"a1" * 32: 2, "b1" * 32: 1})
    first = v.owners[0]
    assert (first.email, first.machines, first.online, first.silent) == (
        "z@x.io",
        2,
        1,
        1,
    )
    assert (first.faulted, first.techs, first.alerts_24h) == (1, 2, 3)
    assert first.url == "/admin/machines?owner=o1"
    assert (v.owners[1].email, v.owners[1].never_reported) == ("y@x.io", 1)


def test_filter_rows_each_filter_and_conjunction():
    ms, reports, accts = _small_fleet()
    rows = _view(ms, reports, accts).rows

    def labels(**kw):
        return sorted(r.label for r in filter_rows(rows, **kw))

    assert labels(status=MachineStatus.silent) == ["claimed-silent"]
    assert labels(claimed=False) == ["unclaimed-online"]
    assert labels(claimed=True) == [
        "claimed-faulted",
        "claimed-never",
        "claimed-silent",
    ]
    assert labels(faulted=True) == ["claimed-faulted"]
    assert labels(owner="o1") == ["claimed-never", "claimed-silent"]
    assert labels(payment="blocked") == []
    assert labels(claimed=True, status=MachineStatus.online) == ["claimed-faulted"]
    assert labels(owner="o1", faulted=True) == []


def _blocked_health(report):
    return report.health.model_copy(update={"payment_enabled": False})


def test_faulted_and_payment_filters_match_online_only():
    ms = [_machine("a1" * 32, "stale"), _machine("b1" * 32, "live")]
    stale = _report(age=1000)
    stale = stale.model_copy(update={"health": _blocked_health(stale)})
    live = _report(faults=False)
    live = live.model_copy(update={"health": _blocked_health(live)})
    rows = _view(ms, {"a1" * 32: stale, "b1" * 32: live}).rows
    assert stale.faults
    assert [r.label for r in filter_rows(rows, faulted=True)] == []
    assert [r.label for r in filter_rows(rows, payment="blocked")] == ["live"]


def test_rows_sorted_silent_first():
    ms, reports, accts = _small_fleet()
    rows = _view(ms, reports, accts).rows
    assert [r.label for r in rows] == [
        "claimed-silent",
        "claimed-never",
        "claimed-faulted",
        "unclaimed-online",
    ]
    never = rows[1]
    assert (never.age_seconds, never.age_text) == (None, "never")


def test_row_fields():
    m = _machine("a1" * 32, "a", owner_id="o1", claim_code_hash="h")
    other = _machine("b1" * 32, "b", claim_code_hash="h", claim_code_expires_at=NOW)
    r = _view(
        [m, other],
        {"a1" * 32: _report(age=30, digest="0" * 64)},
        [_account("o1", "o@x.io")],
    ).rows
    a = next(x for x in r if x.label == "a")
    assert a.owner_email == "o@x.io"
    assert a.claim_open is True
    assert a.ui_mismatch is True
    assert a.age_text == "30s"
    assert a.top_fault.code == "ICE-301"
    assert next(x for x in r if x.label == "b").claim_open is False


def test_portal_is_stale_uses_fleet_view():
    assert portal.STALE_FACTOR is fleet_view.STALE_FACTOR
    assert fleet_view.is_stale(_report(age=181), NOW) is True
    assert fleet_view.is_stale(_report(age=180), NOW) is False


def test_future_report_clamps_age_to_zero():
    m = _machine("c1" * 32, "c")
    row = _view([m], {"c1" * 32: _report(age=-30)}).rows[0]
    assert row.status is MachineStatus.online
    assert (row.age_seconds, row.age_text) == (0.0, "0s")


def test_ui_mismatch_count_on_view():
    m = _machine("a1" * 32, "a")
    v = _view([m], {"a1" * 32: _report(digest="0" * 64)})
    assert v.ui_mismatch_count == 1
