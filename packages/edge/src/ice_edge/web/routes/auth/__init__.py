"""Login, enrollment, logout, setup and setup-code routes.

Everything that used to live on routes.py's `public` router (no session
dependency — these routes exist precisely to establish a session) moved
here verbatim, per Task 1 executor resolution 2. Handlers dereference
`context.xxx` at request time rather than binding module globals at import
(see ice_edge/web/context.py's module docstring).
"""

from fastapi import APIRouter

from ice_edge.web.routes.auth import login, setup

public = APIRouter()

# No overlapping paths between the two sub-routers; original file order kept.
public.include_router(login.router)
public.include_router(setup.router)
