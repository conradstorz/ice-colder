"""Application factory for the cloud portal stub (dev only, no auth: spec 8)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI

from ice_cloud import ingest, portal
from ice_cloud.settings import CloudSettings
from ice_cloud.store import SnapshotStore
from ice_cloud.ui_registry import UiRegistry

CLOUD_TEMPLATES_DIR = Path(__file__).parent / "templates"


def utc_now() -> datetime:
    return datetime.now(UTC)


def create_app(
    settings: CloudSettings,
    *,
    now: Callable[[], datetime] = utc_now,
    provider: object | None = None,  # Task 11 types this as ice_cloud.auth.OidcProvider
) -> FastAPI:
    app = FastAPI(title="ice-cloud")
    app.state.settings = settings
    app.state.provider = provider
    app.state.store = SnapshotStore(settings.data_dir)
    app.state.registry = UiRegistry(
        settings.data_dir, cloud_templates=CLOUD_TEMPLATES_DIR
    )
    app.state.now = now
    app.include_router(ingest.router)
    app.include_router(portal.router)
    return app
