import asyncio
import secrets as _secrets
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

from config.config_model import ConfigModel, Product
from contracts.vending_machine import EXPECTED_SUBSYSTEMS
from services.config_store import add_product, delete_product, update_product
from services.fsm_control import perform_command
from services.health_monitor import HealthMonitor

config: ConfigModel = None


def set_config_object(cfg: ConfigModel):
    global config
    config = cfg


vmc_instance = None
health_monitor = None


def set_vmc_instance(vmc):
    global vmc_instance
    vmc_instance = vmc


def set_health_monitor(monitor):
    global health_monitor
    health_monitor = monitor


event_recorder = None


def set_event_recorder(recorder):
    global event_recorder
    event_recorder = recorder


inventory_manager = None


def set_inventory_manager(inv):
    global inventory_manager
    inventory_manager = inv


_basic_auth = HTTPBasic()


def require_auth(credentials: HTTPBasicCredentials = Depends(_basic_auth)):
    """HTTP Basic auth for every dashboard route, checked against config.web."""
    if config is None:
        raise HTTPException(status_code=503, detail="Configuration not loaded")
    user_ok = _secrets.compare_digest(credentials.username, config.web.admin_username)
    pass_ok = _secrets.compare_digest(
        credentials.password, config.web.admin_password.get_secret_value()
    )
    if not (user_ok and pass_ok):
        raise HTTPException(
            status_code=401,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )


LOG_PATH = Path("logs/vmc.log")


def tail(file_path: Path, lines: int = 50) -> list[str]:
    if not file_path.exists():
        return ["[Log file not found]"]

    with file_path.open("rb") as f:
        f.seek(0, 2)
        end = f.tell()
        buffer = bytearray()
        count = 0

        for pos in range(end - 1, -1, -1):
            f.seek(pos)
            char = f.read(1)
            if char == b"\n":
                count += 1
                if count >= lines:
                    break
            buffer.extend(char)
        result = buffer[::-1].decode("utf-8", errors="replace")
        return result.strip().splitlines()


def attach_routes(app: FastAPI, templates: Jinja2Templates):
    router = APIRouter(dependencies=[Depends(require_auth)])

    @router.post("/inventory/add", response_class=HTMLResponse)
    async def add_new_product(
        request: Request,
        sku: str = Form(...),
        name: str = Form(...),
        price: float = Form(...),
        slot: str | None = Form(None),
    ):
        parsed_slot = int(slot) if slot not in (None, "") else None
        success = add_product(config, sku, name, price, slot=parsed_slot)
        if success and inventory_manager:
            inventory_manager.add_sku(sku, 0, tracked=False)

        return templates.TemplateResponse(
            "partials/inventory_table.html",
            {"request": request, "products": config.products, "locked": _locked_skus()},
        )

    @router.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        return templates.TemplateResponse("dashboard.html", {"request": request})

    @router.get("/config/machine", response_class=HTMLResponse)
    async def machine_info(request: Request):
        return templates.TemplateResponse(
            "partials/machine_info.html",
            {"request": request, "details": config.physical},
        )

    @router.get("/config/contacts", response_class=HTMLResponse)
    async def contact_info(request: Request):
        return templates.TemplateResponse(
            "partials/contacts.html",
            {"request": request, "people": config.physical.people},
        )

    @router.get("/config/payments", response_class=HTMLResponse)
    async def payment_config(request: Request):
        return templates.TemplateResponse(
            "partials/payments.html", {"request": request, "payment": config.payment}
        )

    @router.get("/config/comms", response_class=HTMLResponse)
    async def comms_config(request: Request):
        return templates.TemplateResponse(
            "partials/comms.html", {"request": request, "comm": config.communication}
        )

    def _locked_skus() -> dict[str, str]:
        if not vmc_instance:
            return {}
        return {
            f["sku"]: f["code"]
            for f in vmc_instance.active_faults()
            if f["scope"] == "product"
        }

    async def _render_status(request: Request):
        if not vmc_instance:
            return HTMLResponse(
                '<div class="bg-red-50 rounded-xl border border-red-200 shadow-sm p-5">'
                '<p class="text-red-600 font-semibold">VMC not initialized</p></div>'
            )

        status = vmc_instance.get_status()
        issues: list[str] = []
        active_faults = vmc_instance.active_faults()
        for f in active_faults:
            target = f["product"] or "machine"
            issues.append(f"{f['code']} {f['description']} ({target})")
            f["since_seconds"] = None

        if event_recorder:
            summary_24h = await asyncio.to_thread(event_recorder.get_summary, 24)
            errors_24h = summary_24h["errors"]
            if errors_24h > 0:
                issues.append(
                    f"{errors_24h} error{'s' if errors_24h != 1 else ''} in last 24h"
                )

        if health_monitor:
            health = health_monitor.get_summary()
            ages = {f["key"]: f["since_seconds"] for f in health["active_faults"]}
            for f in active_faults:
                f["since_seconds"] = ages.get(f["key"])
            stale = [name for name, sub in health["subsystems"].items() if sub["stale"]]
            if stale:
                issues.append(f"Stale subsystems: {', '.join(stale)}")
            out_of_range = [
                loc
                for loc, temp in health["temperatures"].items()
                if not temp["in_range"]
            ]
            if out_of_range:
                issues.append(f"Temp issues: {', '.join(out_of_range)}")

        return templates.TemplateResponse(
            "partials/status_fragment.html",
            {
                "request": request,
                "status": status,
                "is_healthy": len(issues) == 0,
                "issues": issues,
                "active_faults": active_faults,
            },
        )

    @router.get("/status", response_class=HTMLResponse)
    async def status_fragment(request: Request):
        return await _render_status(request)

    @router.post("/faults/{key}/clear", response_class=HTMLResponse)
    async def clear_fault(request: Request, key: str):
        if not vmc_instance or not vmc_instance.clear_fault(key, by="admin"):
            raise HTTPException(
                status_code=404, detail=f"No active fault with key {key}"
            )
        return await _render_status(request)

    @router.post("/action/{command}")
    async def control_action(command: str):
        result = perform_command(command, vmc_instance)
        return HTMLResponse(f"<p>{result}</p>")

    @router.get("/logs", response_class=HTMLResponse)
    async def view_logs(request: Request):
        lines = tail(LOG_PATH, lines=10)
        return templates.TemplateResponse(
            "partials/logs_fragment.html", {"request": request, "logs": lines}
        )

    @router.get("/health", response_class=HTMLResponse)
    async def health_summary(request: Request):
        if not health_monitor:
            return HTMLResponse("<div>Health monitor not initialized</div>")
        health = health_monitor.get_summary()
        for name in EXPECTED_SUBSYSTEMS:
            health["subsystems"].setdefault(name, HealthMonitor.empty_subsystem_row())
        return templates.TemplateResponse(
            "partials/health_fragment.html",
            {"request": request, "health": health},
        )

    @router.get("/activity", response_class=HTMLResponse)
    async def activity_fragment(request: Request, period: int = Query(default=24)):
        if not event_recorder:
            return HTMLResponse(
                '<div class="bg-white rounded-xl border border-gray-200 shadow-sm p-5">'
                '<p class="text-gray-400 text-sm">Activity data not available yet.</p></div>'
            )
        if period not in (24, 168, 720):
            period = 24
        summary = await asyncio.to_thread(event_recorder.get_summary, period)
        average = await asyncio.to_thread(event_recorder.get_historical_average, period)
        return templates.TemplateResponse(
            "partials/activity_fragment.html",
            {
                "request": request,
                "period": period,
                "summary": summary,
                "average": average,
            },
        )

    @router.get("/kpi", response_class=HTMLResponse)
    async def kpi_fragment(request: Request):
        if event_recorder:
            summary = await asyncio.to_thread(event_recorder.get_summary, 24)
            average = await asyncio.to_thread(event_recorder.get_historical_average, 24)
        else:
            summary = None
            average = None
        return templates.TemplateResponse(
            "partials/kpi_fragment.html",
            {
                "request": request,
                "summary": summary,
                "average": average,
            },
        )

    @router.get("/inventory", response_class=HTMLResponse)
    async def inventory_view(request: Request):
        return templates.TemplateResponse(
            "partials/inventory_table.html",
            {"request": request, "products": config.products, "locked": _locked_skus()},
        )

    @router.get("/inventory/edit/{sku}", response_class=HTMLResponse)
    async def edit_inventory_item(request: Request, sku: str):
        product = next((p for p in config.products if p.sku == sku), None)
        return templates.TemplateResponse(
            "partials/inventory_edit_form.html",
            {"request": request, "product": product},
        )

    @router.post("/inventory/update/{sku}", response_class=HTMLResponse)
    async def update_inventory_item(
        request: Request,
        sku: str,
        name: str = Form(...),
        price: float = Form(...),
        slot: int = Form(...),
    ):
        update_product(config, sku, name, price, slot=slot)

        return templates.TemplateResponse(
            "partials/inventory_table.html",
            {"request": request, "products": config.products, "locked": _locked_skus()},
        )

    @router.post("/inventory/delete/{sku}", response_class=HTMLResponse)
    async def delete_inventory_item(request: Request, sku: str):
        success = delete_product(config, sku)
        if success and inventory_manager:
            inventory_manager.remove_sku(sku)
        return templates.TemplateResponse(
            "partials/inventory_table.html",
            {"request": request, "products": config.products, "locked": _locked_skus()},
        )

    @router.get("/inventory/new", response_class=HTMLResponse)
    async def new_product_form(request: Request):
        # Blank form, random temporary SKU
        random_sku = f"SKU-{uuid4().hex[:6].upper()}"
        product = Product(sku=random_sku, name="", price=0.0, inventory_count=0)
        return templates.TemplateResponse(
            "partials/inventory_add_form.html",
            {"request": request, "product": product, "mode": "new"},
        )

    @router.get("/inventory/copy/{sku}", response_class=HTMLResponse)
    async def copy_product_form(request: Request, sku: str):
        base = next((p for p in config.products if p.sku == sku), None)
        if base:
            new_sku = f"SKU-{uuid4().hex[:6].upper()}"
            copied = Product(
                sku=new_sku,
                name=f"{base.name} Copy",
                price=base.price,
                inventory_count=base.inventory_count,
                description=base.description,
                image_url=base.image_url,
                track_inventory=base.track_inventory,
            )
            return templates.TemplateResponse(
                "partials/inventory_add_form.html",
                {"request": request, "product": copied, "mode": "copy"},
            )

    app.include_router(router)
