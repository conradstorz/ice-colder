"""Tests for the pure Level data structure in web_interface/levels.py.

Level is a frozen dataclass describing one node of the dashboard v2 navigation
tree: a title, a URL, and a reference to its parent Level (or None for the
root). These tests discover the module's LEVEL_* constants by reflection so
that adding a new level to the tree automatically joins the assertions.
"""

import dataclasses
import inspect

import pytest

from web_interface import levels
from web_interface.levels import Level


def _level_constants():
    """Every module-level attribute named LEVEL_* whose value is a Level."""
    return {
        name: value
        for name, value in inspect.getmembers(levels)
        if name.startswith("LEVEL_") and isinstance(value, Level)
    }


def test_discovers_all_31_levels():
    """29 (spec §2) + LEVEL_TESTS_LOG (Task 13a, /tests/log) +
    LEVEL_TESTS_SALE (Task 13b, /tests/sale's SKU picker) -- the Tests
    level's two static sub-levels besides the parameterized per-subsystem
    page (built at request time via Level.child, not a module constant).
    """
    constants = _level_constants()
    assert len(constants) == 31


def test_home_has_one_crumb_and_root_parent_url():
    home = levels.LEVEL_HOME
    assert home.crumbs == [(home.title, home.url)]
    assert home.parent_url == "/"
    assert home.parent is None
    assert home.url == "/"
    assert home.title == "Home"


def test_second_level_constant_has_two_crumbs_ending_in_itself():
    health = levels.LEVEL_HEALTH
    assert len(health.crumbs) == 2
    assert health.crumbs[0] == (levels.LEVEL_HOME.title, levels.LEVEL_HOME.url)
    assert health.crumbs[-1] == (health.title, health.url)


def test_third_level_constant_has_three_crumbs_ending_in_itself():
    subsystems = levels.LEVEL_HEALTH_SUBSYSTEMS
    assert len(subsystems.crumbs) == 3
    assert subsystems.crumbs[0] == (levels.LEVEL_HOME.title, levels.LEVEL_HOME.url)
    assert subsystems.crumbs[1] == (levels.LEVEL_HEALTH.title, levels.LEVEL_HEALTH.url)
    assert subsystems.crumbs[-1] == (subsystems.title, subsystems.url)


def test_parent_url_matches_parent_url_for_every_constant():
    for name, level in _level_constants().items():
        if level.parent is None:
            assert level.parent_url == "/", (
                f"{name} has no parent but parent_url != '/'"
            )
        else:
            assert level.parent_url == level.parent.url, (
                f"{name}.parent_url does not match its parent's url"
            )


def test_child_produces_parent_crumbs_plus_its_own_and_is_a_new_object():
    parent = levels.LEVEL_PRODUCTS
    child = Level.child(parent, "SKU-123", "/products/SKU-123")

    assert child is not parent
    assert child.parent is parent
    assert child.crumbs == parent.crumbs + [(child.title, child.url)]
    assert child.parent_url == parent.url


def test_child_of_child_inherits_whole_chain():
    product = Level.child(levels.LEVEL_PRODUCTS, "SKU-123", "/products/SKU-123")
    catalog = Level.child(product, "Catalog", "/products/SKU-123/catalog")

    assert len(catalog.crumbs) == 4
    assert catalog.crumbs == [
        (levels.LEVEL_HOME.title, levels.LEVEL_HOME.url),
        (levels.LEVEL_PRODUCTS.title, levels.LEVEL_PRODUCTS.url),
        (product.title, product.url),
        (catalog.title, catalog.url),
    ]
    assert catalog.parent_url == product.url


def test_every_url_in_the_tree_is_unique():
    constants = _level_constants()
    urls = [level.url for level in constants.values()]
    assert len(urls) == len(set(urls))


def test_every_url_in_the_tree_begins_with_slash():
    for name, level in _level_constants().items():
        assert level.url.startswith("/"), f"{name}.url does not start with '/'"


def test_level_is_a_frozen_dataclass():
    home = levels.LEVEL_HOME
    with pytest.raises(dataclasses.FrozenInstanceError):
        home.title = "Changed"


class TestNewReportLevels:
    """The five report/settings levels pre-created for wave-3 tasks."""

    def test_reports_sub_levels_are_children_of_reports_with_right_titles_and_urls(
        self,
    ):
        cases = [
            (levels.LEVEL_REPORTS_PERIOD, "By period", "/reports/period"),
            (levels.LEVEL_REPORTS_PRODUCT, "By product", "/reports/product"),
            (levels.LEVEL_REPORTS_METHOD, "By method", "/reports/method"),
            (
                levels.LEVEL_REPORTS_COLLECTIONS,
                "Cash collections",
                "/reports/collections",
            ),
        ]
        for level, title, url in cases:
            assert level.title == title
            assert level.url == url
            assert level.parent is levels.LEVEL_REPORTS
            assert len(level.crumbs) == 3
            assert level.crumbs[0] == (
                levels.LEVEL_HOME.title,
                levels.LEVEL_HOME.url,
            )
            assert level.crumbs[1] == (
                levels.LEVEL_REPORTS.title,
                levels.LEVEL_REPORTS.url,
            )
            assert level.crumbs[-1] == (level.title, level.url)

    def test_settings_reports_is_a_child_of_settings(self):
        level = levels.LEVEL_SETTINGS_REPORTS
        assert level.title == "Reports"
        assert level.url == "/settings/reports"
        assert level.parent is levels.LEVEL_SETTINGS
        assert len(level.crumbs) == 3
        assert level.crumbs[0] == (levels.LEVEL_HOME.title, levels.LEVEL_HOME.url)
        assert level.crumbs[1] == (
            levels.LEVEL_SETTINGS.title,
            levels.LEVEL_SETTINGS.url,
        )
        assert level.crumbs[-1] == (level.title, level.url)
