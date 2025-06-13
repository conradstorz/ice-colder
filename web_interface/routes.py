from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi import FastAPI

# Mock machine state (replace with real FSM linkage)
from typing import Dict
import random

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

    @router.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        return templates.TemplateResponse("dashboard.html", {"request": request})

    @router.get("/status")
    async def status():
        return JSONResponse(get_mock_status())

    @router.post("/action/{command}")
    async def control_action(command: str):
        # TODO: integrate with FSM control interface
        return JSONResponse({"result": f"Executed {command}"})

    app.include_router(router)
