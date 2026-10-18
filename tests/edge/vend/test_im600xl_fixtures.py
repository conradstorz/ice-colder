"""The IM600XL sample, edge fixture and simulator-local fixture agree."""

import json

from ice_common.config_model import ConfigModel
from ice_contracts.board.dispenser_schema import ReleaseByCatch, WaterFillByVolume
from ice_edge.vend.dispensers import validate_document
from tests.dispenser_fixtures import IM600XL_GOOD, IM600XL_PRODUCTS
from tests.paths import REPO_ROOT
from tests.sims.vending_profiles import IM600XL_PROFILES

EXAMPLE_PATH = REPO_ROOT / "dispensers.example.toml"
CONFIG_EXAMPLE_PATH = REPO_ROOT / "config.example.json"


def _example_report():
    raw = json.loads(CONFIG_EXAMPLE_PATH.read_text(encoding="utf-8"))
    config = ConfigModel.model_validate(raw)
    return validate_document(EXAMPLE_PATH.read_text(encoding="utf-8"), config.products)


def test_example_describes_the_im600xl():
    report = _example_report()
    assert report.ok, report.render_text()
    slot0 = report.profiles[0]
    assert isinstance(slot0.release, ReleaseByCatch)
    assert slot0.release.catch_channel == "trap_door_catch"
    assert set(slot0.accessories) == {"bag_fan"}
    for slot in (1, 2):
        fill = report.profiles[slot].fill
        assert isinstance(fill, WaterFillByVolume)
        assert fill.valve_channel == "water_valve_solenoid"
        assert fill.flow_sensor_channel == "fill_pulses"


def test_im600xl_good_validates_clean():
    report = validate_document(IM600XL_GOOD, IM600XL_PRODUCTS)
    assert report.ok, report.render_text()
    assert report.errors == []


def test_both_fixture_homes_describe_the_same_profiles():
    report = validate_document(IM600XL_GOOD, IM600XL_PRODUCTS)
    assert report.profiles == IM600XL_PROFILES
