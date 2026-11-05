"""Unit tests for the shared route helpers in ice_edge/web/context.py."""

import pytest
from fastapi import HTTPException

from ice_common.config_model import ConfigModel
from ice_edge.web import context


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, True), ("true", True), ("", True), ("anything", True), ("false", False)],
)
def test_confirming_flag_is_false_only_for_the_literal_false(value, expected):
    assert context.confirming_flag(value) is expected


def test_confirming_flag_is_case_sensitive():
    assert context.confirming_flag("FALSE") is True


def test_hx_redirect_is_an_empty_200_with_the_header():
    resp = context.hx_redirect("/somewhere")
    assert resp.status_code == 200
    assert resp.body == b""
    assert resp.headers["hx-redirect"] == "/somewhere"


def test_find_product_and_product_or_404(monkeypatch):
    cfg = ConfigModel()
    monkeypatch.setattr(context, "config", cfg)
    assert context.find_product("nope") is None
    with pytest.raises(HTTPException) as exc:
        context.product_or_404("nope")
    assert exc.value.status_code == 404
    assert exc.value.detail == "No such product: nope"
