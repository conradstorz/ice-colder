from collections.abc import Callable

from ice_edge.supervisor.task_runner import TaskRunner, Timer


class SessionTimer:
    """The customer-session inactivity timer: one re-armable delayed callback.

    Knows nothing about the FSM; the delay is read through `seconds` at each
    `arm()` so a test (or operator) overriding it is honoured immediately.
    """

    LABEL = "session_timeout"

    def __init__(
        self,
        tasks: TaskRunner,
        seconds: Callable[[], float],
        on_expire: Callable[[], None],
    ) -> None:
        self._timer = Timer(tasks, self.LABEL)
        self._seconds = seconds
        self._on_expire = on_expire

    def arm(self, delay: float | None = None) -> None:
        self._timer.arm(self._seconds() if delay is None else delay, self._on_expire)

    def cancel(self) -> None:
        self._timer.cancel()

    @property
    def armed(self) -> bool:
        return self._timer.armed


class PaymentRetryTimer(SessionTimer):
    """The single pending `process_payment` re-check: arming replaces any
    earlier one, so a stale chain can never fire after the sale moved on."""

    LABEL = "process_payment"

    def __init__(self, tasks: TaskRunner, on_fire: Callable[[], None]) -> None:
        super().__init__(tasks, lambda: 1.0, on_fire)

    def on_state(self, state: str) -> None:
        """State-change observer: the re-check only lives while interacting."""
        if state != "interacting_with_user":
            self.cancel()
