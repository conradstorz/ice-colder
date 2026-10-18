"""`Climate` -- the pure ambient/heater model (synchronous)."""

from ice_sims.vending.climate import Climate


def test_drift_below_five_turns_the_heater_on():
    climate = Climate(ambient_c=6.0)
    assert climate.tick(-2.0) == {"heater_power": True}


def test_drift_above_ten_turns_it_off():
    climate = Climate(ambient_c=4.0)
    climate.tick(0.0)
    assert climate.tick(7.0) == {"heater_power": False}


def test_between_the_thresholds_nothing_changes():
    climate = Climate(ambient_c=4.0)
    climate.tick(0.0)  # on
    assert climate.tick(2.0) == {}  # 6 C: still on, no change
    assert Climate(ambient_c=7.0).tick(0.5) == {}


def test_ambient_follows_drift_and_outputs_report_the_heater():
    climate = Climate(ambient_c=20.0)
    climate.tick(1.5)
    assert climate.ambient_c == 21.5
    assert climate.outputs() == {"heater_power": False}
