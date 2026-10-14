"""Device shadow: typed stub (cloud spec section 10, item 1)."""

from __future__ import annotations

from typing import Any


class ShadowService:
    def get_desired(self, machine_id: str) -> dict[str, Any]:
        raise NotImplementedError("cloud spec §10")

    def set_desired(self, machine_id: str, desired: dict[str, Any]) -> None:
        raise NotImplementedError("cloud spec §10")

    def get_reported(self, machine_id: str) -> dict[str, Any]:
        raise NotImplementedError("cloud spec §10")
