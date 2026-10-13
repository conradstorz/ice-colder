"""The dashboard's shared Jinja2Templates instance.

``templates`` is a pure function of the template directory and the filter
set. It has no mutable state and never changes after import, so it is a
module constant, not an injected dependency and not a ``context`` global.
"""

from pathlib import Path

from fastapi.templating import Jinja2Templates

from web_interface.filters import humanize_seconds, sku_url_segment

TEMPLATES_DIR: Path = Path(__file__).parent / "templates"
STATIC_DIR: Path = Path(__file__).parent / "static"

templates = Jinja2Templates(directory=TEMPLATES_DIR)
templates.env.filters["humanize_seconds"] = humanize_seconds
templates.env.filters["sku_url_segment"] = sku_url_segment
