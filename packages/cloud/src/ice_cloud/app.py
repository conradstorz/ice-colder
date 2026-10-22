"""Application factory for the cloud portal (identity and tenancy spec)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from fastapi import FastAPI
from loguru import logger

from ice_cloud.alerts.dispatcher import AlertDispatcher
from ice_cloud.alerts.mail import Mailer, make_mailer
from ice_cloud.body_limit import BodyLimitMiddleware
from ice_cloud import admin, auth, claim, ingest, machine_auth, people, portal
from ice_cloud.alerts import pages
from ice_cloud.alerts.sweep import OfflineSweep
from ice_common.backoff import Backoff
from ice_contracts.cloud import signing
from ice_cloud.machine_auth import SeenSignatures
from ice_cloud.settings import CloudSettings
from ice_cloud.store import SnapshotStore, SqliteRepository, delete_snapshots
from ice_cloud.ui_registry import UiRegistry

CLOUD_TEMPLATES_DIR = Path(__file__).parent / "templates"


def utc_now() -> datetime:
    return datetime.now(UTC)


def create_app(
    settings: CloudSettings,
    *,
    now: Callable[[], datetime] = utc_now,
    provider: auth.OidcProvider | None = None,
    mailer: Mailer | None = None,
    start_sweep: bool = True,
) -> FastAPI:
    http: httpx.AsyncClient | None = None

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        sweep_task = None
        if start_sweep:
            sweep_task = asyncio.create_task(app.state.sweep.run())
        try:
            yield
        finally:
            await app.state.alerts.drain()
            if sweep_task is not None:
                sweep_task.cancel()
                await asyncio.gather(sweep_task, return_exceptions=True)
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
    app.state.mailer = mailer if mailer is not None else make_mailer(settings)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    app.state.store = SnapshotStore(settings.data_dir)
    app.state.repo = SqliteRepository(settings.data_dir / "cloud.db")
    app.state.registry = UiRegistry(
        settings.data_dir,
        cloud_templates=CLOUD_TEMPLATES_DIR,
        trusted_digests=settings.trusted_ui_digests,
    )
    app.state.now = now
    if not settings.smtp_configured:
        logger.warning("SMTP is not configured; alerts are log-only")
    app.state.alerts = AlertDispatcher(
        app.state.repo,
        app.state.mailer,
        now=now,
        public_url=settings.public_url,
        from_addr=settings.smtp_from or "ice-colder@localhost",
    )

    async def purge_unclaimed() -> None:
        cutoff = now() - timedelta(days=settings.unclaimed_ttl_days)
        purged = await asyncio.to_thread(
            app.state.repo.purge_unclaimed_machines, cutoff
        )
        await delete_snapshots(app.state.store, purged)
        if purged:
            logger.info("Purged {} never-claimed machines", len(purged))

    async def housekeeping() -> None:
        """The sweep's one maintenance hook: unclaimed purge, then retention."""
        try:
            await purge_unclaimed()
        except Exception:
            logger.exception("Unclaimed machine purge failed")
        cutoff = now() - timedelta(days=settings.alert_retention_days)
        removed = await asyncio.to_thread(app.state.repo.prune_alert_history, cutoff)
        if removed:
            logger.info("Pruned {} aged alert rows", removed)

    app.state.sweep = OfflineSweep(
        app.state.repo, app.state.alerts, now=now, purge=housekeeping
    )
    # A signature stays acceptable for +/- the skew, so remember it for twice that.
    app.state.seen_signatures = SeenSignatures(2 * signing.MAX_CLOCK_SKEW_SECONDS)
    app.state.register_lock = asyncio.Lock()
    app.state.new_registrations = ingest.SlidingWindow(
        ingest.MAX_NEW_REGISTRATIONS_PER_HOUR, ingest.REGISTRATION_WINDOW_SECONDS
    )
    app.state.bundle_bodies = machine_auth.InflightSlots(
        machine_auth.MAX_INFLIGHT_BUNDLE_BODIES
    )
    app.state.backoff = Backoff(clock=lambda: app.state.now().timestamp())
    app.add_middleware(BodyLimitMiddleware)
    machine_auth.install(app)
    auth.install(app)
    app.include_router(ingest.router)
    app.include_router(auth.router)
    app.include_router(admin.router)
    app.include_router(people.router)
    app.include_router(claim.router)
    app.include_router(pages.router)
    app.include_router(portal.router)
    return app
