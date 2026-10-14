"""The Users levels: /users, /users/{id}, /users/new, /devices,
/users/codes, /users/ownership and their POSTs (task-12 brief).

Replaces the whole of routes/legacy.py's users/devices surface (deleted from
that module in the same commit — task-12 brief resolution 1). The guard
functions (`_guard_owner_target`, `_guard_owner_self_lockout`,
`_guard_owner_device`, `_check_owner_pin`) are moved verbatim from
routes/legacy.py, not reinvented — see each docstring for the Copilot-review
history that put it there. Every part-1 guarantee this module exists to
preserve is listed in task-12-report.md.

Route registration order matters within this router: literal path segments
("/users/new", "/users/codes", "/users/ownership") must be registered before
"/users/{user_id}" for the same HTTP method, or FastAPI/Starlette would match
the dynamic route first and capture e.g. "new" as a user id.
"""

from fastapi import APIRouter

from ice_edge.web.routes.users import accounts, devices, ownership

router = APIRouter()

# Order matters: accounts.router holds /users/{user_id} (GET and POST), which
# would otherwise capture /users/codes, /users/ownership, /users/report,
# /users/transfer and /users/codes/regenerate. It goes last.
router.include_router(ownership.router)
router.include_router(devices.router)
router.include_router(accounts.router)
