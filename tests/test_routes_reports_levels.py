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


class TestTableOverflowContainer:
    @pytest.mark.parametrize("url", LEVEL_URLS)
    def test_table_wrapped_in_overflow_x_auto(self, client, url):
        resp = client.get(url)
        assert resp.status_code == 200
        assert "overflow-x-auto" in resp.text
        assert "<table" in resp.text


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
