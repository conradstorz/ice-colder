# ice_sims/vending/ro_tank.py
"""Pure model of the RO storage tank, its fill pump and its level switches.

No MQTT, no sleeping, no hardware dict: `tick`/`draw` return the output
changes (keyed by `VendingChannel` values) for the simulator to publish.
"""

from dataclasses import dataclass, field

from ice_contracts.board.vending_machine import VendingChannel

_PRODUCTION = VendingChannel.ro_production.value
_FULL = VendingChannel.ro_tank_full.value
_EMPTY = VendingChannel.ro_tank_empty.value


@dataclass
class RoTank:
    capacity_gal: float
    full_gal: float
    empty_gal: float
    fill_gal_per_tick: float
    max_run_ticks: int
    level_gal: float
    fill_blocked: bool = False  # ro_tank_empty injection: no source water
    fill_stuck: bool = False  # ro_fill_stuck injection: pump on, level flat
    cutoff_latched: bool = False  # max-run cutoff tripped; pump held off
    run_ticks: int = field(default=0, init=False)
    _pump_on: bool = field(default=False, init=False, repr=False)
    _published: dict[str, bool] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        self._published = self.outputs()

    @classmethod
    def default(cls) -> "RoTank":
        """Defaults: 400 ticks x 10 s = 4000 s, longer than the edge's
        default 3600 s `ro_max_run_seconds`, so the edge's overrun fault
        fires before the board's cutoff."""
        return cls(
            capacity_gal=100.0,
            full_gal=90.0,
            empty_gal=5.0,
            fill_gal_per_tick=0.5,
            max_run_ticks=400,
            level_gal=80.0,
        )

    def outputs(self) -> dict[str, bool]:
        return {
            _PRODUCTION: self._pump_on,
            _FULL: self.level_gal >= self.full_gal,
            _EMPTY: self.level_gal <= self.empty_gal,
        }

    def _changes(self) -> dict[str, bool]:
        current = self.outputs()
        changed = {k: v for k, v in current.items() if self._published.get(k) != v}
        self._published = current
        return changed

    def tick(self) -> dict[str, bool]:
        if self.level_gal < self.full_gal and not self.cutoff_latched:
            # A blocked feed (no source water) is not a stuck fill: only a pump
            # that runs without the level rising (fill_stuck) counts toward the
            # max-run cutoff.
            if not self.fill_blocked:
                self.run_ticks += 1
            if not (self.fill_blocked or self.fill_stuck):
                self.level_gal = min(
                    self.capacity_gal, self.level_gal + self.fill_gal_per_tick
                )
            if self.run_ticks >= self.max_run_ticks:
                self.cutoff_latched = True
        self._pump_on = self.level_gal < self.full_gal and not self.cutoff_latched
        if not self._pump_on:
            self.run_ticks = 0
        return self._changes()

    def reset_cutoff(self) -> None:
        self.cutoff_latched = False
        self.run_ticks = 0

    def draw(self, gallons: float) -> dict[str, bool]:
        self.level_gal = max(0.0, self.level_gal - gallons)
        return self._changes()
