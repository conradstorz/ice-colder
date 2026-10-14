"""Application factory for the cloud portal stub (dev only, no auth: spec 8)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI

from ice_cloud import ingest, portal
from ice_cloud.store import SnapshotStore
from ice_cloud.ui_registry import UiRegistry

CLOUD_TEMPLATES_DIR = Path(__file__).parent / "templates"


def utc_now() -> datetime:
    return datetime.now(UTC)


def create_app(*, data_dir: Path, now: Callable[[], datetime] = utc_now) -> FastAPI:
    app = FastAPI(title="ice-cloud (dev only, no auth)")
    app.state.store = SnapshotStore(data_dir)
    app.state.registry = UiRegistry(data_dir, cloud_templates=CLOUD_TEMPLATES_DIR)
    app.state.now = now
    app.include_router(ingest.router)
    app.include_router(portal.router)
    return app
