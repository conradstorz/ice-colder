"""UI bundle manifest: the `manifest.json` inside an uploaded UI bundle (cloud spec section 3)."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

DIGEST_PATTERN = r"^[0-9a-f]{64}$"


class LevelEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    title: str
    url: str
    parent_url: str | None


class BundleManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    format: Literal[1]
    digest: str = Field(pattern=DIGEST_PATTERN)
    ice_ui_version: str
    levels: list[LevelEntry]
