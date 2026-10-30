from ice_ui.templating import templates


def test_filters_registered():
    assert "humanize_seconds" in templates.env.filters
    assert "sku_url_segment" in templates.env.filters


def test_money_filter_registered_in_both_environments(tmp_path):
    from ice_ui.templating import make_templates

    for sandboxed in (False, True):
        t = make_templates(tmp_path, sandboxed=sandboxed)
        assert t.env.from_string("{{ 125|money }}").render() == "$1.25"
