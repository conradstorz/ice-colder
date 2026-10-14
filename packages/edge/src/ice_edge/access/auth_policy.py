"""PIN policy and loopback detection shared across the access store and the
dashboard's login/setup/enrollment routes."""

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def is_loopback(host: str) -> bool:
    return host in LOOPBACK_HOSTS


MIN_PIN_LENGTH = 4
MAX_PIN_LENGTH = 8


def pin_problem(pin: str) -> str | None:
    """Why this PIN is unacceptable, or None. Never echoes the value."""
    if len(pin) < MIN_PIN_LENGTH:
        return f"PIN must be at least {MIN_PIN_LENGTH} digits"
    if len(pin) > MAX_PIN_LENGTH:
        return f"PIN must be at most {MAX_PIN_LENGTH} digits"
    if not (pin.isdigit() and pin.isascii()):
        return "PIN must be digits only"
    if len(set(pin)) == 1:
        return "PIN must not be the same digit repeated"
    digits = [int(c) for c in pin]
    ascending = all(b - a == 1 for a, b in zip(digits, digits[1:]))
    descending = all(a - b == 1 for a, b in zip(digits, digits[1:]))
    if ascending or descending:
        return "PIN must not be a run of consecutive digits"
    return None
