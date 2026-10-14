"""Signed remote commands: typed stub (cloud spec section 10, item 2)."""

from __future__ import annotations

from typing import Any


class CommandService:
    def send(
        self,
        machine_id: str,
        command: str,
        *,
        actor: str,
        params: dict[str, Any] | None = None,
    ) -> None:
        raise NotImplementedError("cloud spec §10")
