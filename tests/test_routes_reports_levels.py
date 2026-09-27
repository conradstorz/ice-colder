"""Tests for the four report levels and the email action (Task 10):
GET /reports/period, /reports/product, /reports/product/{sku},
/reports/method, /reports/collections, and POST /reports/email.

Covers: the view_reports permission gate (200 owner/secretary, 403
tech/loader) on all five level routes; breadcrumbs matching the level tree;
the bucket default per range on /reports/period; an invalid `range` falling
back to 30d rather than erroring; a bucket entirely outside the events
retention window rendering "—" (never 0) for its event-derived columns,
proven against a row shown first to exist with the correct revenue; the
product SKU sub-level for a real SKU and a shell 404 for an unknown one;
POST /reports/email against a stubbed mailer (recipient, CSV attachment
filename and parsed-back rows), the fallback to the owner's email when the
current user has none, and the 403 without HX-Request; and the
overflow-x:auto table container.

Fixtures come from tests/conftest.py.
"""

import csv
import io
import re
import time
from urllib.parse import quote

import pytest

from services.access import Role
from services.config_store import add_product
from services.event_recorder import EventRecorder
from web_interface import context as ctx
from web_interface import routes


@pytest.fixture
def recorder(wired, tmp_path):
    """A real EventRecorder wired into routes, torn down after the test --
    same shape as test_routes_reports.py's own `wired_client` fixture."""
    rec = EventRecorder(db_path=str(tmp_path / "events.db"))
    ctx.set_event_recorder(rec)
    try:
        yield rec
    finally:
        ctx.set_event_recorder(None)


def _add_product(cfg, sku="SKU-COLA", name="Cola", price=1.50, slot=1):
    ok = add_product(cfg, sku, name, price, slot=slot, kind="drink")
    assert ok, "test setup: add_product failed"
    return next(p for p in cfg.products if p.sku == sku)


def _breadcrumb(html: str) -> tuple[list[str], str | None]:
    """The breadcrumb <nav>'s hrefs in order, and the leaf's (current page's)
    text -- matches base.html's bar_variant markup exactly."""
    nav_match = re.search(
        r'<nav[^>]*aria-label="Breadcrumb"[^>]*>(.*?)</nav>', html, re.DOTALL
    )
    assert nav_match, "breadcrumb <nav> not found in rendered HTML"
    nav_html = nav_match.group(1)
    hrefs = re.findall(r'href="([^"]+)"', nav_html)
    leaf_match = re.search(r'aria-current="page">([^<]*)</span>', nav_html)
    leaf = leaf_match.group(1).strip() if leaf_match else None
    return hrefs, leaf


def _active_tab(html: str, href_fragment: str) -> bool:
    """True when the anchor whose href contains `href_fragment` carries the
    active-tab styling (border-blue-600) -- mirrors test_routes_reports.py's
    own _active_period helper, generalized to any href substring."""
    pattern = re.escape(href_fragment) + r'[^"]*"\s+class="([^"]*)"'
    match = re.search(pattern, html)
    assert match, f"tab with href containing {href_fragment!r} not found"
    return "border-blue-600" in match.group(1)


LEVEL_URLS = [
    "/reports/period",
    "/reports/product",
    "/reports/method",
    "/reports/collections",
]


class TestPermissionGate:
    """Each of the four range-based levels: 200 for owner/secretary, 403
    for tech/loader -- the same view_reports gate as /reports itself."""

    @pytest.mark.parametrize("url", LEVEL_URLS)
    @pytest.mark.parametrize(
        "role,expected_status",
        [
            (Role.owner, 200),
            (Role.secretary, 200),
            (Role.tech, 403),
            (Role.loader, 403),
        ],
    )
    def test_gate(self, login_as, url, role, expected_status):
        client = login_as(role)
        response = client.get(url)
        assert response.status_code == expected_status

    @pytest.mark.parametrize(
        "role,expected_status",
        [
            (Role.owner, 200),
            (Role.secretary, 200),
            (Role.tech, 403),
            (Role.loader, 403),
        ],
    )
    def test_gate_product_sku(self, login_as, wired, role, expected_status):
        cfg, _vmc, _inv, _store = wired
        product = _add_product(cfg)
        client = login_as(role)
        response = client.get(f"/reports/product/{product.sku}")
        assert response.status_code == expected_status


class TestBreadcrumbs:
    """Breadcrumbs match the level tree (web_interface/levels.py)."""

    def test_period_breadcrumb(self, client):
        resp = client.get("/reports/period")
        assert resp.status_code == 200
        hrefs, leaf = _breadcrumb(resp.text)
        assert hrefs == ["/", "/reports"]
        assert leaf == "By period"

    def test_product_breadcrumb(self, client):
        resp = client.get("/reports/product")
        assert resp.status_code == 200
        hrefs, leaf = _breadcrumb(resp.text)
        assert hrefs == ["/", "/reports"]
        assert leaf == "By product"

    def test_method_breadcrumb(self, client):
        resp = client.get("/reports/method")
        assert resp.status_code == 200
        hrefs, leaf = _breadcrumb(resp.text)
        assert hrefs == ["/", "/reports"]
        assert leaf == "By method"

    def test_collections_breadcrumb(self, client):
        resp = client.get("/reports/collections")
        assert resp.status_code == 200
        hrefs, leaf = _breadcrumb(resp.text)
        assert hrefs == ["/", "/reports"]
        assert leaf == "Cash collections"

    def test_product_sku_breadcrumb_is_a_child_of_by_product(self, client, wired):
        cfg, _vmc, _inv, _store = wired
        product = _add_product(cfg, sku="SKU-CHILD", name="Child Product")
        resp = client.get(f"/reports/product/{product.sku}")
        assert resp.status_code == 200
        hrefs, leaf = _breadcrumb(resp.text)
        assert hrefs == ["/", "/reports", "/reports/product"]
        assert leaf == "Child Product"


class TestBucketDefault:
    """/reports/period's bucket defaults per design §3: day for 7d/30d,
    week for 90d, month for 12m/all -- and an explicit ?bucket= overrides
    the default."""

    @pytest.mark.parametrize(
        "range_key,expected_bucket",
        [
            ("7d", "day"),
            ("30d", "day"),
            ("90d", "week"),
            ("12m", "month"),
            ("all", "month"),
        ],
    )
    def test_default_bucket_per_range(self, client, range_key, expected_bucket):
        resp = client.get(f"/reports/period?range={range_key}")
        assert resp.status_code == 200
        # The range tab for range_key is active...
        assert _active_tab(resp.text, f"/reports/period?range={range_key}")
        # ...and the expected bucket's tab is active, no other bucket's is.
        for bucket in ("day", "week", "month"):
            is_active = _active_tab(
                resp.text, f"/reports/period?range={range_key}&amp;bucket={bucket}"
            )
            assert is_active == (bucket == expected_bucket), (
                f"range={range_key}: expected bucket={expected_bucket} active, "
                f"got bucket={bucket} active={is_active}"
            )

    def test_explicit_bucket_overrides_default(self, client):
        resp = client.get("/reports/period?range=7d&bucket=month")
        assert resp.status_code == 200
        assert _active_tab(resp.text, "/reports/period?range=7d&amp;bucket=month")
        assert not _active_tab(resp.text, "/reports/period?range=7d&amp;bucket=day")


class TestInvalidRangeFallsBack:
    """An unrecognized ?range= falls back to 30d rather than erroring (never
    a 422), on both a range-based level and the email POST."""

    def test_invalid_range_renders_as_30d(self, client):
        resp = client.get("/reports/period?range=not-a-real-range")
        assert resp.status_code == 200
        assert _active_tab(resp.text, "/reports/period?range=30d")
        # 30d's own bucket default (day) is what actually got applied.
        assert _active_tab(resp.text, "/reports/period?range=30d&amp;bucket=day")

    def test_invalid_range_on_product_level(self, client):
        resp = client.get("/reports/product?range=bogus")
        assert resp.status_code == 200
        assert _active_tab(resp.text, "/reports/product?range=30d")


class TestRetentionNoneVersusZero:
    """A bucket entirely outside the 90-day events retention renders
    "—" for failed_vends/refunds/uptime -- never 0, which would
    misreport an old month as fault-free (design §5). Proven against a row
    first shown to exist with the correct revenue, per the task's own
    warning about a hollow "no sales -> row absent" pass.
    """

    def test_old_bucket_shows_dash_not_zero(self, client, recorder):
        # 130 days ago: outside the 90-day retention cutoff, but still
        # inside the 12m/365-day window so its bucket is rendered at all.
        old_ts = time.time() - 130 * 86400
        recorder.record_sale("OLD", "Old Item", 1, 9.00, {"cash": 9.00}, ts=old_ts)

        resp = client.get("/reports/period?range=12m&bucket=month")
        assert resp.status_code == 200
        html = resp.text

        # Positive half FIRST: the row exists and its revenue is correct --
        # proof this isn't merely "no sales -> no row" passing by accident.
        price_idx = html.find("$9.00")
        assert price_idx != -1, "expected a $9.00 revenue cell in the rendered table"
        row_start = html.rfind("<tr>", 0, price_idx)
        row_end = html.find("</tr>", price_idx)
        assert row_start != -1 and row_end != -1
        row_html = html[row_start:row_end]

        # Negative half: failed_vends, refunds and uptime all render the
        # dash, not a literal 0, in this specific row.
        assert row_html.count("—") == 3, (
            f"expected exactly 3 dashes (failed_vends, refunds, uptime) in "
            f"the $9.00 row, found {row_html.count(chr(0x2014))}: {row_html!r}"
        )


class TestProductSku:
    def test_real_sku_shows_its_own_totals(self, client, wired, recorder):
        cfg, _vmc, _inv, _store = wired
        product = _add_product(cfg, sku="SKU-REAL", name="Real Product")
        recorder.record_sale(
            product.sku, product.name, 1, 2.50, {"cash": 2.50}, ts=time.time() - 10
        )

        resp = client.get(f"/reports/product/{product.sku}?range=30d")
        assert resp.status_code == 200
        assert "Real Product" in resp.text
        assert "$2.50" in resp.text

    def test_unknown_sku_is_shell_404(self, client):
        resp = client.get("/reports/product/NO-SUCH-SKU")
        assert resp.status_code == 404
        assert "text/html" in resp.headers["content-type"]
        assert '<header id="bar"' in resp.text
        assert "Not found" in resp.text


class TestSkuUrlSafety:
    """Family C (Copilot review): Product.sku is an arbitrary catalog
    string, but every route that takes one is matched against a single
    URL path, so a raw '?' (starts a query string) or a raw '/' (a
    plain str-converter route 404s on the client's percent-encoded
    '%2F' -- see sku_url_segment's docstring) breaks routing outright.
    Every link built from a SKU (the product table row, the per-SKU
    page's own range/bucket tabs, and Level.child's breadcrumb/Back URL)
    must go through the same web_interface.filters.sku_url_segment
    encoding, and the route must use {sku:path}, end to end: the link
    resolves, the page loads, and the breadcrumb Back still works.
    """

    @pytest.mark.parametrize("sku", ["A/B", "A?B"])
    def test_product_table_row_link_resolves_for_unsafe_sku(
        self, client, wired, recorder, sku
    ):
        cfg, _vmc, _inv, _store = wired
        product = _add_product(cfg, sku=sku, name="Odd SKU Product", slot=7)
        # by_product's table only lists SKUs with sales in the window.
        recorder.record_sale(
            product.sku, product.name, 7, 1.50, {"cash": 1.50}, ts=time.time() - 10
        )

        table_resp = client.get("/reports/product")
        assert table_resp.status_code == 200
        hrefs = re.findall(r'href="(/reports/product/[^"]+)"', table_resp.text)
        matching = [h for h in hrefs if h.startswith("/reports/product/A")]
        assert matching, f"no row link found for sku={sku!r} in {hrefs}"
        href = matching[0]
        # The percent-encoded SKU is present as one opaque unit -- not a
        # raw '?' that would start a query string early.
        assert quote(sku) in href

        sku_resp = client.get(href)
        assert sku_resp.status_code == 200
        assert "Odd SKU Product" in sku_resp.text

    @pytest.mark.parametrize("sku", ["A/B", "A?B"])
    def test_sku_level_tabs_and_breadcrumb_back_resolve_for_unsafe_sku(
        self, client, wired, sku
    ):
        cfg, _vmc, _inv, _store = wired
        _add_product(cfg, sku=sku, name="Odd SKU Product", slot=7)
        encoded = quote(sku)

        resp = client.get(f"/reports/product/{encoded}")
        assert resp.status_code == 200
        html = resp.text

        # Range + bucket tabs on the sku-level page itself.
        tab_hrefs = re.findall(r'href="(/reports/product/[^"]+)"', html)
        assert len(tab_hrefs) >= 8, tab_hrefs  # 5 range + 3 bucket tabs
        for tab_href in tab_hrefs:
            assert quote(sku) in tab_href
            tab_resp = client.get(tab_href)
            assert tab_resp.status_code == 200, f"{tab_href} did not resolve"

        # Breadcrumb: parent is /reports/product (unaffected by the SKU),
        # and the Back button -- level.parent_url in base.html -- resolves.
        hrefs, leaf = _breadcrumb(html)
        assert hrefs == ["/", "/reports", "/reports/product"]
        assert leaf == "Odd SKU Product"
        back_resp = client.get(hrefs[-1])
        assert back_resp.status_code == 200


class TestTableOverflowContainer:
    """Finding 2: the two substring checks this test used to make
    (`"overflow-x-auto" in resp.text` and `"<table" in resp.text`) are
    independent -- a page with overflow-x-auto on an unrelated div and a
    bare table elsewhere would pass. This regex instead requires an
    overflow-x-auto-carrying div's opening tag to be followed by a
    `<table` before that *specific* div closes, so it can only pass when
    the table genuinely nests inside the overflow container.
    """

    # A non-greedy "anything that isn't a </div>" between the container's
    # opening tag and <table -- so a <table> appearing anywhere AFTER the
    # overflow-x-auto div closes (i.e. NOT nested inside it) cannot match,
    # while the real markup (container -> ... -> <table -- possibly with
    # ordinary non-div content, or nested divs whose own closes come
    # before the outer one, in between) still does. Proven to discriminate
    # by test_regression_container_and_table_separated_would_fail below.
    _CONTAINER_THEN_TABLE = re.compile(
        r'<div\b[^>]*class="[^"]*\boverflow-x-auto\b[^"]*"[^>]*>'
        r"(?:(?!</div>).)*?<table\b",
        re.DOTALL,
    )

    @pytest.mark.parametrize("url", LEVEL_URLS)
    def test_table_wrapped_in_overflow_x_auto(self, client, url):
        resp = client.get(url)
        assert resp.status_code == 200
        assert self._CONTAINER_THEN_TABLE.search(resp.text), (
            "expected a <table> to appear before the overflow-x-auto "
            "container's own closing </div>, i.e. genuinely nested inside it"
        )

    def test_regression_container_and_table_separated_would_fail(self):
        """Proof the strengthened assertion above actually discriminates:
        break it by separating the container from the table (an unrelated
        div carries overflow-x-auto; the table lives elsewhere, unwrapped)
        and show the same regex now fails to match -- this is the failure
        Finding 2 says the old two-substring check could never catch.
        """
        broken_html = (
            '<div class="overflow-x-auto">unrelated content</div>'
            "<p>some other markup in between</p>"
            '<table class="w-full text-sm"><tr><td>x</td></tr></table>'
        )
        # The old (weak) assertions would both still pass on this markup...
        assert "overflow-x-auto" in broken_html
        assert "<table" in broken_html
        # ...but the strengthened one correctly rejects it: the table is
        # not nested inside the overflow-x-auto div's own closing </div>.
        assert self._CONTAINER_THEN_TABLE.search(broken_html) is None


class TestEmailReport:
    """POST /reports/email: gated view_reports + require_htmx, sends a
    plain-text body plus a CSV attachment via a stubbed mailer, and reports
    success/failure inline."""

    @pytest.fixture
    def stub_mailer(self, monkeypatch):
        calls = []

        async def fake_send_email(email_config, to, subject, body, attachments=None):
            calls.append(
                {
                    "to": to,
                    "subject": subject,
                    "body": body,
                    "attachments": attachments,
                }
            )
            return True

        monkeypatch.setattr(routes.reports, "send_email", fake_send_email)
        return calls

    def _configure_gateway(self, cfg):
        cfg.communication.email_gateway.smtp_server = "smtp.real.local"

    def test_emails_period_report_with_csv_attachment(
        self, client, wired, recorder, stub_mailer
    ):
        cfg, _vmc, _inv, _store = wired
        self._configure_gateway(cfg)
        recorder.record_sale("A", "Alpha", 1, 9.00, {"cash": 9.00}, ts=time.time() - 10)

        resp = client.post(
            "/reports/email",
            data={"report": "period", "range": "30d", "bucket": "day"},
        )
        assert resp.status_code == 200
        assert "Report emailed to" in resp.text

        assert len(stub_mailer) == 1
        call = stub_mailer[0]
        assert call["to"] == "ada@example.com"  # the wired fixture's owner

        assert call["attachments"] is not None
        assert len(call["attachments"]) == 1
        filename, payload, mime = call["attachments"][0]
        assert filename == f"{cfg.machine_id}-period-30d.csv"
        assert mime == "text/csv"

        # The CSV bytes parse back to the rendered rows.
        reader = csv.DictReader(io.StringIO(payload.decode("utf-8")))
        parsed = list(reader)
        assert len(parsed) >= 1
        assert parsed[-1]["bucket_start"] == "Total"
        assert float(parsed[-1]["revenue"]) == pytest.approx(9.00)
        matching = [r for r in parsed if r["bucket_start"] != "Total"]
        assert any(float(r["revenue"]) == pytest.approx(9.00) for r in matching)

    def test_falls_back_to_owner_email_when_user_has_none(self, wired, stub_mailer):
        from tests.conftest import sign_in

        cfg, _vmc, _inv, store = wired
        self._configure_gateway(cfg)
        cfg.physical.people.machine_owner.email = "owner@example.com"

        no_email_user = store.create_user("No Email Sec", None, Role.secretary, "3691")
        client = sign_in(store, no_email_user)
        try:
            resp = client.post(
                "/reports/email", data={"report": "collections", "range": "30d"}
            )
            assert resp.status_code == 200
        finally:
            client.close()

        assert len(stub_mailer) == 1
        assert stub_mailer[0]["to"] == "owner@example.com"

    def test_email_post_requires_htmx_header(self, client):
        resp = client.post(
            "/reports/email",
            data={"report": "period", "range": "30d", "bucket": "day"},
            headers={"HX-Request": "false"},
        )
        assert resp.status_code == 403

    def test_email_post_succeeds_with_htmx_header_same_url(
        self, client, stub_mailer, wired
    ):
        """The 200 case for this exact URL, in this exact file -- so the
        403-without-HX-Request test above can't be passing because the
        route doesn't exist at all."""
        cfg, _vmc, _inv, _store = wired
        self._configure_gateway(cfg)
        resp = client.post(
            "/reports/email",
            data={"report": "period", "range": "30d", "bucket": "day"},
        )
        assert resp.status_code == 200

    def test_send_failure_reports_inline(self, client, wired, monkeypatch):
        cfg, _vmc, _inv, _store = wired
        self._configure_gateway(cfg)

        async def failing_send_email(*args, **kwargs):
            return False

        monkeypatch.setattr(routes.reports, "send_email", failing_send_email)

        resp = client.post("/reports/email", data={"report": "product", "range": "30d"})
        assert resp.status_code == 200
        assert "Could not send the email" in resp.text

    def test_emails_method_report_with_csv_attachment(
        self, client, wired, recorder, stub_mailer
    ):
        """Finding 3: report=method was entirely untested for the email
        action -- seed two distinct raw method strings, email it, and
        assert on the stubbed mailer's call (filename, CSV parsed back
        against the amounts record_sale actually recorded)."""
        cfg, _vmc, _inv, _store = wired
        self._configure_gateway(cfg)
        recorder.record_sale(
            "A", "Alpha", 1, 1.00, {"cash_coin": 1.00}, ts=time.time() - 20
        )
        recorder.record_sale(
            "B", "Beta", 2, 2.00, {"cash_bill": 2.00}, ts=time.time() - 10
        )

        resp = client.post("/reports/email", data={"report": "method", "range": "30d"})
        assert resp.status_code == 200
        assert "Report emailed to" in resp.text

        assert len(stub_mailer) == 1
        call = stub_mailer[0]
        assert call["to"] == "ada@example.com"

        filename, payload, mime = call["attachments"][0]
        assert filename == f"{cfg.machine_id}-method-30d.csv"
        assert mime == "text/csv"

        reader = csv.DictReader(io.StringIO(payload.decode("utf-8")))
        parsed = {row["method"]: row for row in reader}
        assert set(parsed) == {"cash_coin", "cash_bill"}
        assert float(parsed["cash_coin"]["amount"]) == pytest.approx(1.00)
        assert float(parsed["cash_bill"]["amount"]) == pytest.approx(2.00)

    def test_emails_product_sku_report_with_csv_attachment(
        self, client, wired, recorder, stub_mailer
    ):
        """Finding 3: report=product_sku was entirely untested for the
        email action. Also exercises the ?bucket=/form `bucket` plumbing
        Finding 1 adds to this level -- an explicit bucket posted with the
        form must be the one actually queried and rendered."""
        cfg, _vmc, _inv, _store = wired
        self._configure_gateway(cfg)
        product = _add_product(cfg, sku="SKU-EMAIL", name="Email Product")
        recorder.record_sale(
            product.sku, product.name, 1, 3.25, {"cash": 3.25}, ts=time.time() - 10
        )
        # A different SKU's sale, same window -- must NOT leak into this
        # SKU's total; proves the sku= filter is actually applied here,
        # not merely that the route renders something.
        recorder.record_sale(
            "SKU-OTHER", "Other Product", 2, 99.00, {"cash": 99.00}, ts=time.time() - 5
        )

        resp = client.post(
            "/reports/email",
            data={
                "report": "product_sku",
                "range": "30d",
                "bucket": "day",
                "sku": product.sku,
            },
        )
        assert resp.status_code == 200
        assert "Report emailed to" in resp.text

        assert len(stub_mailer) == 1
        call = stub_mailer[0]
        assert call["to"] == "ada@example.com"

        filename, payload, mime = call["attachments"][0]
        assert filename == f"{cfg.machine_id}-product-{product.sku}-30d.csv"
        assert mime == "text/csv"

        reader = csv.DictReader(io.StringIO(payload.decode("utf-8")))
        parsed = list(reader)
        # Positive half first: the sku's own revenue is present, in a real
        # bucket row (not just the Total row) -- proof this isn't a hollow
        # "no sales seeded" pass.
        matching = [r for r in parsed if r["bucket_start"] != "Total"]
        assert any(float(r["revenue"]) == pytest.approx(3.25) for r in matching)
        assert parsed[-1]["bucket_start"] == "Total"
        assert float(parsed[-1]["revenue"]) == pytest.approx(3.25)

    def test_email_unknown_report_returns_400(self, client, wired):
        """Finding 3: the `Unknown report` 400 branch was entirely
        untested."""
        cfg, _vmc, _inv, _store = wired
        self._configure_gateway(cfg)

        resp = client.post(
            "/reports/email", data={"report": "not-a-real-report", "range": "30d"}
        )
        assert resp.status_code == 400
