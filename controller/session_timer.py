from collections.abc import Callable

from controller.task_runner import TaskRunner


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
        self._tasks = tasks
        self._seconds = seconds
        self._on_expire = on_expire
        self._task = None

    def arm(self) -> None:
        self.cancel()
        self._task = self._tasks.schedule(
            self._seconds(), self._on_expire, label=self.LABEL
        )

    def cancel(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None

    @property
    def armed(self) -> bool:
        return self._task is not None and not self._task.done()
