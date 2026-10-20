"""Whether the cloud is currently emailing a machine's alerts (spec section 4)."""

from __future__ import annotations

from ice_cloud.settings import CloudSettings
from ice_cloud.store import Machine, Repository


def alerts_handled_for(
    repo: Repository, settings: CloudSettings, machine: Machine
) -> bool:
    """True when the edge may defer its own alert emails to the cloud.

    The machine is claimed, the cloud can send mail, at least one recipient is
    unmuted and unsuspended, and the latest delivery attempt did not fail.
    """
    if machine.owner_id is None or not settings.smtp_configured:
        return False
    people = repo.people_for_alerts(machine.fingerprint)
    if not any(not muted for _, muted in people):
        return False
    return repo.delivery_health(machine.fingerprint)
