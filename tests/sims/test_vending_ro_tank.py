"""`RoTank` -- the pure RO storage-tank model (synchronous)."""

from ice_sims.vending.channels import SENSOR_PUBLISH_INTERVAL
from ice_sims.vending.ro_tank import RoTank


def _tank(**kw) -> RoTank:
    base = dict(
        capacity_gal=10.0,
        full_gal=9.0,
        empty_gal=1.0,
        fill_gal_per_tick=2.0,
        max_run_ticks=100,
        level_gal=0.0,
    )
    base.update(kw)
    return RoTank(**base)


def test_filling_from_empty_runs_the_pump_then_reports_full():
    tank = _tank()
    assert tank.tick() == {"ro_production": True, "ro_tank_empty": False}
    assert tank.tick() == {}
    assert tank.tick() == {}
    assert tank.tick() == {}
    assert tank.tick() == {"ro_tank_full": True, "ro_production": False}
    assert tank.outputs() == {
        "ro_production": False,
        "ro_tank_full": True,
        "ro_tank_empty": False,
    }


def test_tick_returns_only_changed_keys():
    tank = _tank(level_gal=9.5)
    assert tank.tick() == {}
    assert tank.tick() == {}


def test_draw_past_empty_sets_ro_tank_empty():
    tank = _tank(level_gal=5.0)
    assert tank.draw(3.0) == {}
    assert tank.draw(3.0) == {"ro_tank_empty": True}
    assert tank.level_gal == 0.0


def test_draw_from_full_clears_full():
    tank = _tank(level_gal=9.5)
    tank.tick()
    assert tank.draw(1.0) == {"ro_tank_full": False}


def test_stuck_fill_trips_the_cutoff_and_stays_off():
    tank = _tank(max_run_ticks=3, fill_stuck=True)
    assert tank.tick() == {"ro_production": True}
    assert tank.tick() == {}
    assert tank.tick() == {"ro_production": False}
    assert tank.cutoff_latched
    assert tank.level_gal == 0.0
    assert tank.run_ticks == 0
    for _ in range(5):
        assert tank.tick() == {}
    assert tank.outputs()["ro_production"] is False


def test_draw_does_not_restart_a_latched_pump():
    tank = _tank(max_run_ticks=2, fill_stuck=True, level_gal=4.0)
    tank.tick()
    tank.tick()
    assert tank.cutoff_latched
    tank.draw(1.0)
    assert tank.tick() == {}
    assert tank.cutoff_latched


def test_reset_cutoff_restarts_the_pump():
    tank = _tank(max_run_ticks=2, fill_stuck=True)
    tank.tick()
    tank.tick()
    assert tank.cutoff_latched
    tank.fill_stuck = False
    tank.reset_cutoff()
    assert not tank.cutoff_latched
    assert tank.tick()["ro_production"] is True


def test_run_ticks_reset_when_the_pump_turns_off():
    tank = _tank(max_run_ticks=3, level_gal=8.0)
    tank.tick()  # reaches full on the first tick: off, count stays 0
    assert tank.outputs()["ro_production"] is False
    tank.draw(5.0)
    tank.tick()
    tank.tick()
    assert not tank.cutoff_latched  # two fresh ticks, not four


def test_fill_blocked_keeps_the_tank_empty():
    tank = _tank(fill_blocked=True)
    tank.tick()
    tank.tick()
    assert tank.level_gal == 0.0
    assert tank.outputs()["ro_tank_empty"] is True


def test_default_limit_outlasts_the_edge_ro_overrun_limit():
    edge_limit_seconds = 3600
    default = RoTank.default()
    assert default.max_run_ticks * SENSOR_PUBLISH_INTERVAL > edge_limit_seconds
