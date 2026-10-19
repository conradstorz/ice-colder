"""Application factory for the cloud portal stub (dev only, no auth: spec 8)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI

from ice_cloud import ingest, machine_auth, portal
from ice_cloud.settings import CloudSettings
from ice_cloud.store import SnapshotStore, SqliteRepository
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
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    app.state.store = SnapshotStore(settings.data_dir)
    app.state.repo = SqliteRepository(settings.data_dir / "cloud.db")
    app.state.registry = UiRegistry(
        settings.data_dir, cloud_templates=CLOUD_TEMPLATES_DIR
    )
    app.state.now = now
    machine_auth.install(app)
    app.include_router(ingest.router)
    app.include_router(portal.router)
    return app
