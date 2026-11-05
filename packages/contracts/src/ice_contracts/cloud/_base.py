"""Shared base for the cloud wire models: strict (no extra keys) and immutable."""

from pydantic import BaseModel, ConfigDict


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
