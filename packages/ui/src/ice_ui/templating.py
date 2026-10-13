"""The shared Jinja2Templates factory and the dashboard's default instance.

``make_templates`` is a pure function of the template directories and the
filter set, so the machine and the cloud portal render the same UI from one
definition. ``templates`` is the machine's default instance.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from fastapi.templating import Jinja2Templates
from jinja2 import ChoiceLoader, Environment, FileSystemLoader
from jinja2.sandbox import SandboxedEnvironment

from ice_ui.filters import humanize_seconds, sku_url_segment

TEMPLATES_DIR: Path = Path(__file__).parent / "templates"
STATIC_DIR: Path = Path(__file__).parent / "static"


def make_templates(
    directory: Path,
    *,
    sandboxed: bool = False,
    extra_dirs: Sequence[Path] = (),
) -> Jinja2Templates:
    """Build a Jinja2Templates with the UI filters registered.

    Templates are looked up in ``extra_dirs`` first, then ``directory``.
    ``sandboxed=True`` renders in a ``SandboxedEnvironment`` (for templates
    that are not wholly trusted).
    """
    loader = ChoiceLoader([FileSystemLoader(d) for d in (*extra_dirs, directory)])
    env_class = SandboxedEnvironment if sandboxed else Environment
    result = Jinja2Templates(env=env_class(loader=loader, autoescape=True))
    result.env.filters["humanize_seconds"] = humanize_seconds
    result.env.filters["sku_url_segment"] = sku_url_segment
    return result


templates = make_templates(TEMPLATES_DIR)
