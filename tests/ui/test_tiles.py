from ice_ui.tiles import tile


def test_tile_returns_the_five_consumed_keys():
    assert tile("Logs", "/health/logs", "health", "Last 50 lines") == {
        "title": "Logs",
        "url": "/health/logs",
        "icon": "health",
        "context": "Last 50 lines",
        "enabled": True,
    }


def test_tile_enabled_false_is_kept_not_dropped():
    t = tile("Users", "/users", "users", "3 known", enabled=False)
    assert t["enabled"] is False
    assert set(t) == {"title", "url", "icon", "context", "enabled"}
