# ice_sims/vending/climate.py
"""Pure model of the ambient temperature and the autonomous cabinet heater."""

from dataclasses import dataclass, field

from ice_contracts.board.vending_machine import VendingChannel

_HEATER = VendingChannel.heater_power.value


@dataclass
class Climate:
    ambient_c: float
    heater_on_below_c: float = 5.0
    heater_off_above_c: float = 10.0
    heater_on: bool = field(default=False, init=False)

    def outputs(self) -> dict[str, bool]:
        return {_HEATER: self.heater_on}

    def tick(self, drift_c: float) -> dict[str, bool]:
        self.ambient_c += drift_c
        before = self.heater_on
        if self.ambient_c < self.heater_on_below_c:
            self.heater_on = True
        elif self.ambient_c > self.heater_off_above_c:
            self.heater_on = False
        return {_HEATER: self.heater_on} if self.heater_on != before else {}
