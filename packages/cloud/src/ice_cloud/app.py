"""Application factory for the cloud portal stub (dev only, no auth: spec 8)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import httpx
from fastapi import FastAPI

from ice_cloud.body_limit import BodyLimitMiddleware
from ice_cloud import admin, auth, ingest, machine_auth, people, portal
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
    provider: auth.OidcProvider | None = None,
) -> FastAPI:
    http: httpx.AsyncClient | None = None

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            if http is not None:
                await http.aclose()

    app = FastAPI(title="ice-cloud", lifespan=lifespan)
    app.state.settings = settings
    if provider is None and settings.google_configured:
        http = httpx.AsyncClient(timeout=10.0)
        provider = auth.GoogleProvider(
            settings.google_client_id,
            settings.google_client_secret,
            (settings.public_url or "").rstrip("/") + "/auth/google/callback",
            http=http,
        )
    app.state.provider = provider
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    app.state.store = SnapshotStore(settings.data_dir)
    app.state.repo = SqliteRepository(settings.data_dir / "cloud.db")
    app.state.registry = UiRegistry(
        settings.data_dir, cloud_templates=CLOUD_TEMPLATES_DIR
    )
    app.state.now = now
    app.add_middleware(BodyLimitMiddleware)
    machine_auth.install(app)
    auth.install(app)
    app.include_router(ingest.router)
    app.include_router(auth.router)
    app.include_router(admin.router)
    app.include_router(people.router)
    app.include_router(portal.router)
    return app
