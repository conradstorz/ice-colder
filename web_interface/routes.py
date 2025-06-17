from pathlib import Path
from typing import Dict
import random

from uuid import uuid4
from services.config_store import add_product

from fastapi.responses import HTMLResponse
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi import FastAPI

from services.config_store import update_product

from services.fsm_control import perform_command

from config.config_model import ConfigModel, Product

config: ConfigModel = None

def set_config_object(cfg: ConfigModel):
    global config
    config = cfg

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
            if char == b'\n':
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
        inventory_count: int = Form(...)
    ):
        success = add_product(config, sku, name, price, inventory_count)

        return templates.TemplateResponse("partials/inventory_table.html", {
            "request": request,
            "products": config.products
        })
    
    
    @router.post("/inventory/update/{sku}", response_class=HTMLResponse)
    async def update_inventory_item(
        request: Request,
        sku: str,
        name: str = Form(...),
        price: float = Form(...),
        inventory_count: int = Form(...)
    ):
        update_product(config, sku, name, price, inventory_count)

        return templates.TemplateResponse("partials/inventory_table.html", {
            "request": request,
            "products": config.products
        })


    @router.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        return templates.TemplateResponse("dashboard.html", {"request": request})


    @router.get("/status", response_class=HTMLResponse)
    async def status_fragment(request: Request):
        status = get_mock_status()  # Replace this with your real FSM status later
        return templates.TemplateResponse("partials/status_fragment.html", {
            "request": request,
            "status": status
        })


    @router.post("/action/{command}")
    async def control_action(command: str):
        result = perform_command(command)
        return HTMLResponse(f"<p>{result}</p>")
    

    @router.get("/logs", response_class=HTMLResponse)
    async def view_logs(request: Request):
        lines = tail(LOG_PATH, lines=10)
        return templates.TemplateResponse("partials/logs_fragment.html", {
            "request": request,
            "logs": lines
        })


    @router.get("/inventory", response_class=HTMLResponse)
    async def inventory_view(request: Request):
        return templates.TemplateResponse("partials/inventory_table.html", {
            "request": request,
            "products": config.products
        })


    @router.get("/inventory/edit/{sku}", response_class=HTMLResponse)
    async def edit_inventory_item(request: Request, sku: str):
        product = next((p for p in config.products if p.sku == sku), None)
        return templates.TemplateResponse("partials/inventory_edit_form.html", {
            "request": request,
            "product": product
        })


    @router.post("/inventory/update/{sku}", response_class=HTMLResponse)
    async def update_inventory_item(
        request: Request,
        sku: str,
        name: str = Form(...),
        price: float = Form(...),
        inventory_count: int = Form(...)
    ):
        for p in config.products:
            if p.sku == sku:
                p.name = name
                p.price = price
                p.inventory_count = inventory_count
                break

        return templates.TemplateResponse("partials/inventory_table.html", {
            "request": request,
            "products": config.products
        })
    

    @router.get("/inventory/new", response_class=HTMLResponse)
    async def new_product_form(request: Request):
        # Blank form, random temporary SKU
        random_sku = f"SKU-{uuid4().hex[:6].upper()}"
        product = Product(sku=random_sku, name="", price=0.0, inventory_count=0)
        return templates.TemplateResponse("partials/inventory_add_form.html", {
            "request": request,
            "product": product,
            "mode": "new"
        })


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
            return templates.TemplateResponse("partials/inventory_add_form.html", {
                "request": request,
                "product": copied,
                "mode": "copy"
            })


    app.include_router(router)
