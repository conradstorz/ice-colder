from ice_ui.templating import templates


def test_filters_registered():
    assert "humanize_seconds" in templates.env.filters
    assert "sku_url_segment" in templates.env.filters


def test_base_template_loads():
    assert templates.get_template("base.html") is not None
