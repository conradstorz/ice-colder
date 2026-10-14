"""Every class used by the cloud-only templates must exist in the committed
app.css (the portal serves ice_ui's stylesheet; there is no cloud build)."""

from pathlib import Path

import ice_cloud
from tests.ui.test_static_css import (
    APP_CSS_PATH,
    NO_CSS_CLASSES,
    class_is_covered,
    extract_classes,
)

CLOUD_TEMPLATES = Path(ice_cloud.__file__).parent / "templates"


def test_every_cloud_template_class_is_in_app_css():
    css = APP_CSS_PATH.read_text(encoding="utf-8")
    templates = sorted(CLOUD_TEMPLATES.rglob("*.html"))
    assert templates
    missing = {
        path.name: sorted(
            c
            for c in extract_classes(path.read_text(encoding="utf-8"))
            if c not in NO_CSS_CLASSES and not class_is_covered(c, css)
        )
        for path in templates
    }
    assert not {k: v for k, v in missing.items() if v}, missing
