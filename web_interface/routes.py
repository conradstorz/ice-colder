import random
from pathlib import Path
from typing import Dict
from uuid import uuid4

from fastapi import APIRouter, FastAPI, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from config.config_model import ConfigModel, Product
from services.config_store import add_product, update_product
from services.fsm_control import perform_command

config: ConfigModel = None


def set_config_object(cfg: ConfigModel):
    global config
    config = cfg


vmc_instance = None


def set_vmc_instance(vmc):
    global vmc_instance
    vmc_instance = vmc


LOG_PATH = Path("LOGS/vmc.log")


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


status_data = {
    "state": "IDLE",
    "uptime": 0,
    "errors": [],
}


def get_mock_status() -> Dict:
    status_data["uptime"] += 1
    status_data["state"] = random.choice(["IDLE", "READY", "VENDING", "ERROR"])
    return status_data


def attach_routes(app: FastAPI, templates: Jinja2Templates):
    router = APIRouter()

    @router.post("/inventory/add", response_class=HTMLResponse)
    async def add_new_product(
        request: Request,
        sku: str = Form(...),
        name: str = Form(...),
        price: float = Form(...),
        inventory_count: int = Form(...),
    ):
        add_product(config, sku, name, price, inventory_count)

        return templates.TemplateResponse(
            "partials/inventory_table.html", {"request": request, "products": config.products}
        )

    @router.post("/inventory/update/{sku}", response_class=HTMLResponse)
    async def update_inventory_item(
        request: Request, sku: str, name: str = Form(...), price: float = Form(...), inventory_count: int = Form(...)
    ):
        update_product(config, sku, name, price, inventory_count)

        return templates.TemplateResponse(
            "partials/inventory_table.html", {"request": request, "products": config.products}
        )

    @router.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        return templates.TemplateResponse("dashboard.html", {"request": request})

    @router.get("/config/machine", response_class=HTMLResponse)
    async def machine_info(request: Request):
        return templates.TemplateResponse(
            "partials/machine_info.html", {"request": request, "details": config.physical}
        )

    @router.get("/config/inventory", response_class=HTMLResponse)
    async def inventory_panel(request: Request):
        return templates.TemplateResponse(
            "partials/inventory_table.html", {"request": request, "products": config.products}
        )

    @router.get("/config/contacts", response_class=HTMLResponse)
    async def contact_info(request: Request):
        return templates.TemplateResponse(
            "partials/contacts.html", {"request": request, "people": config.physical.people}
        )

    @router.get("/config/payments", response_class=HTMLResponse)
    async def payment_config(request: Request):
        return templates.TemplateResponse("partials/payments.html", {"request": request, "payment": config.payment})

    @router.get("/config/comms", response_class=HTMLResponse)
    async def comms_config(request: Request):
        return templates.TemplateResponse("partials/comms.html", {"request": request, "comm": config.communication})

    @router.get("/status", response_class=HTMLResponse)
    async def status_fragment(request: Request):
        if not vmc_instance:
            return HTMLResponse("<div>🚨 VMC not initialized</div>")

        return templates.TemplateResponse(
            "partials/status_fragment.html", {"request": request, "status": vmc_instance.get_status()}
        )

    @router.post("/action/{command}")
    async def control_action(command: str):
        result = perform_command(command)
        return HTMLResponse(f"<p>{result}</p>")

    @router.get("/logs", response_class=HTMLResponse)
    async def view_logs(request: Request):
        lines = tail(LOG_PATH, lines=10)
        return templates.TemplateResponse("partials/logs_fragment.html", {"request": request, "logs": lines})

    @router.get("/inventory/edit/{sku}", response_class=HTMLResponse)
    async def edit_inventory_item(request: Request, sku: str):
        product = next((p for p in config.products if p.sku == sku), None)
        return templates.TemplateResponse("partials/inventory_edit_form.html", {"request": request, "product": product})

    @router.get("/inventory/new", response_class=HTMLResponse)
    async def new_product_form(request: Request):
        # Blank form, random temporary SKU
        random_sku = f"SKU-{uuid4().hex[:6].upper()}"
        product = Product(sku=random_sku, name="", price=0.0, inventory_count=0)
        return templates.TemplateResponse(
            "partials/inventory_add_form.html", {"request": request, "product": product, "mode": "new"}
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
                "partials/inventory_add_form.html", {"request": request, "product": copied, "mode": "copy"}
            )

    app.include_router(router)
