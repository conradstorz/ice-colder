# tests/test_dispensers_validation.py
"""Tests for the `dispensers.toml` validation pipeline (plan: dispenser
profiles, Task 3). `validate_document` runs tomllib, then per-slot
`SlotProfile` validation, then cross-checks against the product catalog,
channel roles, the dispense time budget, and (optionally) the vending
board's declared capabilities.
"""

from config.config_model import Product
from contracts.vending_machine import SubsystemCapabilities
from contracts.common import ChannelDescriptor
from services.dispensers import (
    Finding,
    ValidationReport,
    _dedupe_union_findings,
    validate_document,
)
from tests.dispenser_fixtures import GOOD, ICE, WATER


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


def test_good_file_is_ok():
    report = validate_document(GOOD, [ICE, WATER])
    assert report.ok
    assert set(report.profiles.keys()) == {1, 2}
    assert len(report.warnings) == 2
    assert len(report.errors) == 0


def test_syntax_error_reports_line():
    lines = GOOD.splitlines()
    lines.insert(5, "bad = = value")
    bad = "\n".join(lines) + "\n"
    report = validate_document(bad, [ICE, WATER])
    assert report.file_error
    assert len(report.findings) == 1
    assert report.findings[0].line == 6
    assert report.findings[0].severity == "error"


def test_bad_slot_key():
    bad = GOOD.replace("[slot.1]", "[slot.01]").replace("[slot.1.", "[slot.01.")
    report = validate_document(bad, [ICE, WATER])
    assert not report.file_error
    file_findings = [f for f in report.findings if f.slot is None]
    assert any("01" in f.message for f in file_findings)
    # slot 2 is still examined despite the bad key for slot 1
    assert 2 in report.profiles


def test_schema_error_names_slot_path_and_value():
    bad = GOOD.replace("max_run_seconds    = 25.0", "max_run_seconds    = 500")
    report = validate_document(bad, [ICE, WATER])
    matches = [
        f for f in report.findings if f.slot == 1 and f.path == "fill.max_run_seconds"
    ]
    assert len(matches) == 1
    finding = matches[0]
    assert "between 1 and 120" in finding.message
    assert "500" in finding.message
    expected_line = (
        GOOD.splitlines().index(
            "max_run_seconds    = 25.0         # 1–120; ICE-301 if the sensor never trips"
        )
        + 1
    )
    assert finding.line == expected_line


def test_unknown_field_suggests():
    bad = GOOD.replace(
        "pulses_per_liter       = 450.0", "pulses_per_litre       = 450.0"
    )
    report = validate_document(bad, [ICE, WATER])
    matches = [f for f in report.findings if f.slot == 2]
    assert any("did you mean pulses_per_liter" in f.message for f in matches)


def test_one_bad_slot_does_not_sink_the_other():
    bad = GOOD.replace("max_run_seconds    = 25.0", "max_run_seconds    = 500")
    report = validate_document(bad, [ICE, WATER])
    assert set(report.profiles.keys()) == {2}


def test_missing_profile_for_catalog_product():
    lines = GOOD.splitlines()
    start = lines.index("[slot.2]")
    bad = "\n".join(lines[:start]) + "\n"
    report = validate_document(bad, [ICE, WATER])
    matches = [f for f in report.findings if f.slot == 2]
    assert any("WATER-1GAL" in f.message for f in matches)
    # The table is entirely absent -- no [slot.2] header exists to point
    # at, so the finding has no line.
    no_table_finding = next(
        f for f in matches if "has no valid [slot.2] table" in f.message
    )
    assert no_table_finding.line is None


def test_invalid_profile_for_catalog_product_has_a_line():
    # The [slot.2] table exists (and so does its header line) but fails
    # schema validation, so it never becomes a candidate profile -- the
    # cross-check's "has no valid [slot.N] table" finding must still
    # resolve a line pointing at that existing header, not fall back to
    # `None` just because this finding's own `path` is empty.
    bad = GOOD.replace('proof                  = "flow_volume"   # or "timed"\n', "")
    report = validate_document(bad, [ICE, WATER])
    matches = [
        f
        for f in report.findings
        if f.slot == 2 and "has no valid [slot.2] table" in f.message
    ]
    assert len(matches) == 1
    finding = matches[0]
    expected_line = bad.splitlines().index("[slot.2]") + 1
    assert finding.line == expected_line


def test_sku_slot_mismatch():
    # slot 1's table now claims WATER-1GAL (the one occurrence of that
    # literal product_sku line, under [slot.1]), whose catalog slot is 2.
    bad = GOOD.replace('product_sku = "ICE-10LB"', 'product_sku = "WATER-1GAL"', 1)
    report = validate_document(bad, [ICE, WATER])
    matches = [f for f in report.findings if f.slot == 1]
    assert any("slot 2" in f.message and "not slot 1" in f.message for f in matches)


def test_kind_mechanism_mismatch():
    other_ice = Product(sku="ICE-10LB", slot=1, kind="other")
    report = validate_document(GOOD, [other_ice, WATER])
    matches = [f for f in report.findings if f.slot == 1]
    assert any(
        "bagged_ice" in f.message and "ICE-10LB" in f.message and "other" in f.message
        for f in matches
    )


def test_duplicate_sku():
    bad = GOOD.replace('product_sku = "WATER-1GAL"', 'product_sku = "ICE-10LB"')
    report = validate_document(bad, [ICE, WATER])
    matches = [f for f in report.findings if f.slot in (1, 2)]
    assert any("ICE-10LB" in f.message for f in matches if f.slot == 1)
    assert any("ICE-10LB" in f.message for f in matches if f.slot == 2)


def test_unknown_sku():
    bad = GOOD.replace('product_sku = "ICE-10LB"', 'product_sku = "ICE-99LB"')
    report = validate_document(bad, [ICE, WATER])
    matches = [f for f in report.findings if f.slot == 1]
    assert any(
        "ICE-99LB" in f.message and "not in the catalog" in f.message for f in matches
    )


def test_channel_used_as_drive_and_sense():
    bad = GOOD.replace('channel      = "bag_fan"', 'channel      = "bag_full_sensor"')
    report = validate_document(bad, [ICE, WATER])
    matches = [f for f in report.findings if f.slot == 1]
    assert any("both a drive and a sensor" in f.message for f in matches)
    assert 1 not in report.profiles


def test_time_budget():
    report = validate_document(GOOD, [ICE, WATER], dispense_timeout_seconds=40)
    matches = [f for f in report.findings if f.slot == 1]
    assert any("exceeds dispense_timeout_seconds" in f.message for f in matches)


def test_capabilities_checked():
    good_caps = _good_capabilities()
    report = validate_document(GOOD, [ICE, WATER], capabilities=good_caps)
    assert len(report.warnings) == 0
    assert report.ok

    missing_caps = _good_capabilities()
    missing_caps.channels = [
        c for c in missing_caps.channels if c.channel_id != "bag_fan"
    ]
    report = validate_document(GOOD, [ICE, WATER], capabilities=missing_caps)
    matches = [f for f in report.findings if f.slot == 1]
    assert any("not declared" in f.message and "bag_fan" in f.message for f in matches)

    wrong_dir_caps = _good_capabilities()
    for c in wrong_dir_caps.channels:
        if c.channel_id == "bag_full_sensor":
            c.direction = "output"
    report = validate_document(GOOD, [ICE, WATER], capabilities=wrong_dir_caps)
    matches = [f for f in report.findings if f.slot == 1]
    assert any("declared as output" in f.message for f in matches)


def test_accessory_range_error_is_humanized():
    bad = GOOD.replace("lead_seconds = 2.0", "lead_seconds = 999.0")
    report = validate_document(bad, [ICE, WATER])
    matches = [
        f
        for f in report.findings
        if f.slot == 1 and f.path == "accessories.bag_fan.lead_seconds"
    ]
    assert len(matches) == 1
    finding = matches[0]
    assert "must be between 0 and 30" in finding.message
    assert "999" in finding.message


def test_accessory_unknown_field_suggests():
    bad = GOOD.replace("lead_seconds = 2.0", "lead_second = 2.0")
    report = validate_document(bad, [ICE, WATER])
    matches = [f for f in report.findings if f.slot == 1]
    assert any("did you mean lead_seconds" in f.message for f in matches)


def test_render_text_format():
    report = validate_document(GOOD, [ICE, WATER])
    text = report.render_text()
    lines = text.splitlines()
    warning_lines = [line for line in lines if "capabilities unknown" in line]
    assert len(warning_lines) == 2
    assert any(
        line.startswith("Slot 1") and "OK" in line and "ICE-10LB" in line
        for line in lines
    )
    assert any(
        line.startswith("Slot 2") and "OK" in line and "WATER-1GAL" in line
        for line in lines
    )
    assert lines[-1] == "0 error(s), 2 warning(s)"


def test_render_text_sorts_file_level_findings_first():
    # Findings deliberately out of order (slot 2, then file-level, then
    # slot 1) -- render_text must sort them file-level first, then by
    # ascending slot, regardless of the order they were appended in.
    report = ValidationReport(
        findings=[
            Finding(
                slot=2, path="", line=None, severity="error", message="slot2 problem"
            ),
            Finding(
                slot=None, path="", line=None, severity="error", message="file problem"
            ),
            Finding(
                slot=1, path="", line=None, severity="error", message="slot1 problem"
            ),
        ]
    )
    text = report.render_text()
    file_idx = text.index("file problem")
    slot1_idx = text.index("slot1 problem")
    slot2_idx = text.index("slot2 problem")
    assert file_idx < slot1_idx < slot2_idx


def test_render_text_invalid_verdict_wording():
    bad = GOOD.replace("max_run_seconds    = 25.0", "max_run_seconds    = 500")
    report = validate_document(bad, [ICE, WATER])
    lines = report.render_text().splitlines()
    assert any(line.startswith("Slot 1") and "INVALID (" in line for line in lines)
    assert not any("error(s)" in line and line.startswith("Slot") for line in lines)


def test_union_field_range_error_is_one_humanized_finding():
    bad = GOOD.replace(
        'stall_current_amps = "unmonitored"   # a number here requires current_channel\n'
        'current_channel    = "unmonitored"',
        'stall_current_amps = 0.01\ncurrent_channel    = "agitator_current_sense"',
    )
    report = validate_document(bad, [ICE, WATER])
    matches = [
        f
        for f in report.findings
        if f.slot == 1 and f.path == "agitate.stall_current_amps"
    ]
    assert len(matches) == 1
    finding = matches[0]
    assert finding.message == "must be between 0.1 and 50 A, got 0.01"
    assert finding.line is not None


def test_dedupe_only_collapses_findings_whose_loc_had_union_noise():
    # Grouping on `(slot, path)` alone -- with no regard for *why* two
    # findings share it -- would wrongly collapse two genuinely different
    # errors that happen to land on the same path by coincidence, not
    # because they're split arms of the same untagged union. A `CurrentSense`
    # both-or-neither violation (a plain `value_error` from a model
    # validator, reported at path "agitate" as a whole) can't itself be
    # reproduced alongside a second, unrelated error at that exact path
    # through real schema validation -- Pydantic's "after" validator never
    # runs once a sibling field has already failed -- so this exercises
    # `_dedupe_union_findings` directly with two hand-built findings that
    # share a path without either coming from a stripped union-noise token.
    both_or_neither = Finding(
        slot=1,
        path="agitate",
        line=10,
        severity="error",
        message=(
            "stall_current_amps and current_channel must both be set or "
            'both be "unmonitored"'
        ),
    )
    other_agitate_error = Finding(
        slot=1,
        path="agitate",
        line=12,
        severity="error",
        message="some other, unrelated agitate-level problem",
    )
    result = _dedupe_union_findings(
        [(both_or_neither, True, False), (other_agitate_error, True, False)]
    )
    assert result == [both_or_neither, other_agitate_error]


def test_dedupe_still_collapses_real_union_noise_split():
    # The actual case this function exists for: one untagged union field
    # failing on two arms at once produces two findings sharing `(slot,
    # path)` because `humanize` stripped the same trailing noise token
    # from each -- those must still collapse to one.
    raw = Finding(
        slot=1,
        path="agitate.stall_current_amps",
        line=5,
        severity="error",
        message="Input should be a valid number",
    )
    humanized = Finding(
        slot=1,
        path="agitate.stall_current_amps",
        line=5,
        severity="error",
        message="must be between 0.1 and 50 A, got 'oops'",
    )
    result = _dedupe_union_findings([(raw, True, True), (humanized, False, True)])
    assert result == [humanized]


def test_missing_mechanism_is_humanized_with_line():
    bad = GOOD.replace('mechanism   = "bagged_ice"\n', "")
    report = validate_document(bad, [ICE, WATER])
    matches = [
        f
        for f in report.findings
        if f.slot == 1 and 'missing required field "mechanism"' in f.message
    ]
    assert len(matches) == 1
    finding = matches[0]
    assert finding.path == ""
    assert "bagged_ice" in finding.message
    assert "water_fill" in finding.message
    expected_line = bad.splitlines().index("[slot.1]") + 1
    assert finding.line == expected_line


def test_missing_proof_is_humanized_with_line():
    bad = GOOD.replace('proof              = "bag_full_sensor"   # or "timed"\n', "")
    report = validate_document(bad, [ICE, WATER])
    matches = [f for f in report.findings if f.slot == 1 and f.path == "fill"]
    assert len(matches) == 1
    finding = matches[0]
    assert 'missing required field "proof"' in finding.message
    assert "bag_full_sensor" in finding.message
    assert "timed" in finding.message
    expected_line = bad.splitlines().index("[slot.1.fill]") + 1
    assert finding.line == expected_line


def test_accessory_named_like_a_union_label_keeps_its_path():
    # An accessory table key is free text and flows through the same
    # loc-noise filter used to strip Pydantic's synthetic untagged-union
    # labels ("constrained-float", ...) -- a key that happens to start
    # with "str"/"int"/"float" (a real-sounding device name: "strobe",
    # "intake_fan") must not be mistaken for one of those labels and
    # silently dropped from the reported path.
    bad = GOOD.replace("[slot.1.accessories.bag_fan]", "[slot.1.accessories.strobe]")
    bad = bad.replace("lead_seconds = 2.0", "lead_seconds = 999.0")
    report = validate_document(bad, [ICE, WATER])
    matches = [
        f
        for f in report.findings
        if f.slot == 1 and f.path == "accessories.strobe.lead_seconds"
    ]
    assert len(matches) == 1
    finding = matches[0]
    assert "must be between 0 and 30" in finding.message
    assert finding.line is not None


def test_accessory_named_like_a_pydantic_label_keeps_its_path():
    # Pydantic's own synthetic union-arm labels include hyphenated and
    # bracketed forms ("constrained-float", "function-before", ...) and
    # even a form with no distinguishing punctuation at all
    # ("is-instance") -- TOML bare keys allow hyphens, so an operator
    # could legitimately name an accessory any of these. None may be
    # mistaken for the real synthetic label and dropped from the
    # reported path: the noise filter must key off *position* (the
    # trailing element Pydantic reserves for a union-arm tag), not just
    # content, since these accessory names are never in that position --
    # each is a dict key followed by its own "lead_seconds" field.
    # Rebuild explicitly rather than chaining fragile string surgery: one
    # accessory per name, each with its own out-of-range lead_seconds.
    header = GOOD[: GOOD.index("[slot.1.accessories.bag_fan]")]
    footer = GOOD[GOOD.index("[slot.2]") :]
    accessories = "".join(
        f"[slot.1.accessories.{name}]\n"
        f'channel      = "bag_fan"\n'
        f'on_during    = ["fill"]\n'
        f"lead_seconds = 999.0\n"
        f"lag_seconds  = 0.5\n\n"
        for name in ("constrained-fan", "function-light", "is-instance")
    )
    bad = header + accessories + footer

    report = validate_document(bad, [ICE, WATER])
    for name in ("constrained-fan", "function-light", "is-instance"):
        path = f"accessories.{name}.lead_seconds"
        matches = [f for f in report.findings if f.slot == 1 and f.path == path]
        assert len(matches) == 1, (name, [f.path for f in report.findings])
        finding = matches[0]
        assert "must be between 0 and 30" in finding.message
        assert finding.line is not None


def test_bad_channel_id_is_humanized():
    bad = GOOD.replace(
        'motor_channel      = "agitator_motor"',
        'motor_channel      = "Agitator Motor!"',
    )
    report = validate_document(bad, [ICE, WATER])
    matches = [
        f for f in report.findings if f.slot == 1 and f.path == "agitate.motor_channel"
    ]
    assert len(matches) == 1
    finding = matches[0]
    assert "lowercase letters, digits and underscores" in finding.message
    assert "1–64 characters" in finding.message
    assert "Agitator Motor!" in finding.message
    assert "pattern" not in finding.message.lower()


def test_slot_key_length_capped():
    bad = GOOD.replace("[slot.1]", "[slot.1000000]").replace(
        "[slot.1.", "[slot.1000000."
    )
    report = validate_document(bad, [ICE, WATER])
    file_findings = [f for f in report.findings if f.slot is None]
    assert any("1000000" in f.message for f in file_findings)


def test_deeply_nested_toml_is_a_file_error():
    bad = "x = " + "[" * 2000 + "]" * 2000 + "\n"
    report = validate_document(bad, [ICE, WATER])
    assert report.file_error
    assert len(report.findings) == 1
    finding = report.findings[0]
    assert finding.severity == "error"
    assert finding.slot is None
    assert "could not be parsed" in finding.message
