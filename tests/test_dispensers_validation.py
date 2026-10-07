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
from services.dispensers import Finding, ValidationReport, validate_document
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
