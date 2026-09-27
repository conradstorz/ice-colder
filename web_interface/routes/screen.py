"""`/screen` and `/screen/body` — the read-only customer-facing screen.

Moved verbatim from the old web_interface/routes.py (Task 1 executor
resolution 2); no behavior change, no template touched.
"""

import asyncio

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from contracts.vending_machine import EXPECTED_SUBSYSTEMS
from services.access import Permission
from services.health_monitor import HealthMonitor
from web_interface import auth as web_auth
from web_interface import context


def build_router(templates: Jinja2Templates) -> APIRouter:
    router = APIRouter()

    def _screen_context(request: Request) -> dict:
        status = (
            context.vmc_instance.get_status()
            if context.vmc_instance
            else {"state": "unknown", "credit_escrow": 0.0}
        )
        faults = context.vmc_instance.active_faults() if context.vmc_instance else []
        health = (
            context.health_monitor.get_summary()
            if context.health_monitor
            else {"subsystems": {}, "mqtt_connected": False}
        )
        for name in EXPECTED_SUBSYSTEMS:
            health["subsystems"].setdefault(name, HealthMonitor.empty_subsystem_row())
        kinds = {}
        for kind in ("ice", "water"):
            ok, failing = (
                context.availability.sale_available(kind)
                if context.availability
                else (None, [])
            )
            kinds[kind] = {"ok": ok, "failing": failing}
        return context.template_context(
            request,
            status=status,
            faults=faults,
            health=health,
            kinds=kinds,
            payment_enabled=(
                context.availability.payment_enabled if context.availability else None
            ),
            payment_reasons=(
                context.availability.payment_blocking_reasons()
                if context.availability
                else []
            ),
        )

    @router.get(
        "/screen",
        response_class=HTMLResponse,
        dependencies=[Depends(web_auth.require(Permission.view_status))],
    )
    async def screen(request: Request):
        return templates.TemplateResponse(
            "screen.html", context.template_context(request)
        )

    @router.get(
        "/screen/body",
        response_class=HTMLResponse,
        dependencies=[Depends(web_auth.require(Permission.view_status))],
    )
    async def screen_body(request: Request):
        ctx = _screen_context(request)
        if context.event_recorder:
            summary = await asyncio.to_thread(context.event_recorder.get_summary, 24)
            ctx["money_24h"] = summary["money_in"]
            ctx["vends_24h"] = summary["products_out"]
        else:
            ctx["money_24h"] = None
            ctx["vends_24h"] = None
        return templates.TemplateResponse("partials/screen_body.html", ctx)

    return router
