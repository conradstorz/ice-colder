"""UI bundle manifest: the `manifest.json` inside an uploaded UI bundle (cloud spec section 3)."""

from typing import Literal

from pydantic import Field

from ice_contracts.cloud._base import _Model

DIGEST_PATTERN = r"^[0-9a-f]{64}$"


class LevelEntry(_Model):
    title: str
    url: str
    parent_url: str | None


class BundleManifest(_Model):
    format: Literal[1]
    digest: str = Field(pattern=DIGEST_PATTERN)
    ice_ui_version: str
    levels: list[LevelEntry]
