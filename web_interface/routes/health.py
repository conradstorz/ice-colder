"""Stub area router for health.

Empty on purpose: Task 1 of the dashboard-v2-shell plan only splits the
existing routes.py into an area-per-module package; the routes that will
live here move over in a later, dedicated plan task, one area at a time.
Registered in web_interface/routes/__init__.py alongside the real routers
so that later task can add routes here without also touching __init__.py.
"""

from fastapi import APIRouter
from fastapi.templating import Jinja2Templates


def build_router(templates: Jinja2Templates) -> APIRouter:
    return APIRouter()
