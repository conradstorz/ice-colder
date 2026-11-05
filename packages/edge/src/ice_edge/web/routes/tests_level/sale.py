from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from ice_edge.access.access import Permission
from ice_edge.web import auth as web_auth
from ice_edge.web import context
from ice_ui.levels import LEVEL_TESTS_SALE
from ice_edge.web.routes.tests_level.commands import products_by_slot
from ice_edge.web.routes.tests_level.lease import _refusal, _require_lease
from ice_ui.templating import templates


router = APIRouter()


@router.get(
    "/tests/sale",
    response_class=HTMLResponse,
    dependencies=[Depends(web_auth.require(Permission.run_tests))],
)
async def tests_sale_picker(request: Request):
    """The simulated-sale SKU picker (Task 13b). Deliberately takes no
    lease -- same rule as `tests_level`/`tests_subsystem` above: a
    POST /tests/sale run is what acquires it, not viewing this page.

    Registered BEFORE /tests/{subsystem} below, same reason /tests/log
    is: "sale" would otherwise be swallowed as a (nonexistent)
    subsystem name by that single-segment route.
    """
    return templates.TemplateResponse(
        request,
        "tests_sale.html",
        context.template_context(
            request, level=LEVEL_TESTS_SALE, products=products_by_slot()
        ),
    )


@router.post(
    "/tests/sale",
    response_class=HTMLResponse,
    dependencies=[
        Depends(web_auth.require(Permission.run_tests)),
        Depends(context.require_htmx),
    ],
)
async def tests_sale_run(request: Request, sku: str = Form(...)):
    """Run one simulated sale (TestSaleRunner.run_test_sale) for the
    SKU the picker's form submitted.

    SKU-with-slash: the SKU arrives as a POST form field
    (application/x-www-form-urlencoded), never a URL segment -- GET and
    POST /tests/sale are the only two routes this picker defines (no
    `{sku}` route segment exists anywhere under it), so there is no
    second SKU-in-a-URL escaping mechanism to invent here:
    `sku_url_segment`/`{sku:path}` exist specifically because a raw `/`
    in a URL PATH segment 404s (see packages/ui/src/ice_ui/filters.py's
    docstring); a form body has no such restriction, and FastAPI's
    `Form(...)` decodes it back to the exact original string,
    including any `/`. `tests_sale.html`'s `<option value="{{
    product.sku }}">` is likewise not a URL -- it is an HTML attribute
    value, which Jinja2's autoescaping (the default here) already
    makes safe on its own for any catalog string, `/` included.
    """
    principal = web_auth.current_principal(request)
    products = context.config.products if context.config else []
    product = next((p for p in products if p.sku == sku), None)
    if product is None:
        raise HTTPException(status_code=404, detail=f"No such product: {sku}")

    refused = _require_lease(request, principal)
    if refused is not None:
        return refused

    machine = context.machine_instance
    try:
        result = await machine.test_sales.run_test_sale(
            sku, user_id=principal.user.id, user_name=principal.user.name
        )
    except (ValueError, RuntimeError) as exc:
        # ValueError: the catalog moved under us between the lookup
        # above and this call (vmc.products, not context.config.products
        # -- see run_test_sale's own find_product). RuntimeError:
        # run_test_sale's own "could not select" guard (locked out,
        # sold out) or "no lease held" (a race against the check above).
        # Either way this is a refusal, not a 500.
        return _refusal(request, str(exc))

    return templates.TemplateResponse(
        request,
        "partials/test_sale_result.html",
        context.template_context(request, result=result),
    )
