from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import routes

# No CORS middleware: the dashboard is same-origin (HTMX partials from this app).
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

# NOTE: /static is served WITHOUT auth (Starlette mounts can't take dependencies).
# Keep only non-sensitive assets (css/js/images) here; everything else goes
# through the permission-gated router in routes.py (see require() decorator).
app.mount("/static", StaticFiles(directory="web_interface/static"), name="static")

routes.attach_routes(app)
