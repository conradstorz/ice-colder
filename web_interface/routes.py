from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi import FastAPI

# Mock machine state (replace with real FSM linkage)
from typing import Dict
import random

from services.fsm_control import perform_command

from pathlib import Path
from fastapi.responses import HTMLResponse

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
 
    app.include_router(router)
