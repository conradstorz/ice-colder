"""Dashboard admin-password policy shared by first-run setup and startup checks."""

import secrets

WEAK_PASSWORDS = {"changeme", "admin", "password", "ice-colder"}
MIN_PASSWORD_LENGTH = 12
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def password_problem(password: str) -> str | None:
    """Why this admin password must not face a network, or None if acceptable."""
    if not password:
        return "admin password is empty"
    if password.lower() in WEAK_PASSWORDS:
        # Never echo the value: this message is logged by the startup check.
        return "admin password is a well-known default"
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"admin password is shorter than {MIN_PASSWORD_LENGTH} characters"
    return None


def generate_admin_password() -> str:
    """A random URL-safe password for first-run configs (20 characters)."""
    return secrets.token_urlsafe(15)


def is_loopback(host: str) -> bool:
    return host in LOOPBACK_HOSTS
