"""Class-coverage test for the compiled, committed dashboard v2 stylesheet.

Offline 7-inch tablets have no CDN, so every Tailwind utility class a
template references must already be baked into the committed
`web_interface/static/app.css` by the build
(`web_interface/tailwind.config.js` + `web_interface/tailwind.input.css`,
compiled with the standalone Tailwind CLI — see that config file's header
comment for the exact command). A class missing from `app.css` is a build
defect, not a test defect: per the task-3 brief's resolution 6, fix the
build (Tailwind's content scan, or a `safelist` entry for a class assembled
at render time), never widen the skip rule below until the class
disappears from the check.
"""

from __future__ import annotations

import re
from pathlib import Path

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "web_interface" / "templates"
APP_CSS_PATH = (
    Path(__file__).resolve().parent.parent / "web_interface" / "static" / "app.css"
)

_CLASS_ATTR_RE = re.compile(r'class="([^"]*)"')

# A whole Jinja expression (`{{ ... }}`) or statement (`{% ... %}`) inside a
# class="..." attribute is dynamic — its value cannot be read statically off
# the page, so it is dropped wholesale before splitting on whitespace, e.g.:
#   - class="{{ 'a' if x else 'b' }}"           -> "" (nothing to check)
#   - class="p-2 {{ extra }}"                   -> "p-2 " -> {"p-2"}
#   - class="px-4 ... {% if p == period %}border-b-2 border-blue-600
#            text-blue-600 {% else %}text-gray-500 ... {% endif %}"
#     (partials/activity_fragment.html's tab styling) -> the three `{% %}`
#     tags are dropped and the literal class text either side of them
#     — `border-b-2`, `border-blue-600`, `text-gray-500`, etc. — is kept
#     and checked normally.
# Without this step, naively splitting on whitespace instead leaves bare
# Jinja keywords and dotted attribute lookups behind as fake "tokens" (`if`,
# `else`, `endif`, `==`, `s.uptime_pct`, `<`) that are not CSS classes and
# were never going to appear in app.css — checking them would make the test
# fail on templates that have no real coverage gap. Dropping whole
# `{{ }}` / `{% %}` blocks is not a widening of what gets skipped in order
# to hide a missing class (task-3 brief resolution 6): every literal class
# name in every template, including every branch of every conditional
# class, still gets extracted and checked below.
_JINJA_BLOCK_RE = re.compile(r"\{\{.*?\}\}|\{%.*?%\}", re.DOTALL)

# Safety net, not the primary mechanism: if any brace or quote character
# survives the block-stripping above (a malformed or unanticipated Jinja
# construct), drop that token rather than let a syntax fragment masquerade
# as a class. This should never fire against today's templates.
_SKIP_CHARS = ("{", "}", '"', "'")


def _is_plausible_class(token: str) -> bool:
    return bool(token) and not any(ch in token for ch in _SKIP_CHARS)


def extract_classes(html: str) -> set[str]:
    """Return every plausible literal class token from `class="..."` attributes."""
    tokens: set[str] = set()
    for match in _CLASS_ATTR_RE.finditer(html):
        literal_text = _JINJA_BLOCK_RE.sub(" ", match.group(1))
        for token in literal_text.split():
            if _is_plausible_class(token):
                tokens.add(token)
    return tokens


def escape_for_css_selector(class_name: str) -> str:
    """Escape a class name the way Tailwind escapes it in a generated selector.

    Tailwind backslash-escapes every character in a class name that is not
    a plain ASCII letter, digit, underscore or hyphen when turning it into
    a CSS selector, e.g. `lg:grid-cols-4` -> `.lg\\:grid-cols-4`,
    `w-1/2` -> `.w-1\\/2`, `px-2.5` -> `.px-2\\.5`.
    """
    return "".join(
        ch if (ch.isalnum() or ch in "_-") else "\\" + ch for ch in class_name
    )


def class_is_covered(class_name: str, css_text: str) -> bool:
    """True if `class_name`'s escaped selector form is a substring of `css_text`."""
    return escape_for_css_selector(class_name) in css_text


def all_template_classes() -> dict[str, set[str]]:
    """Map each template path (relative to the templates dir) to its class tokens."""
    return {
        str(path.relative_to(TEMPLATES_DIR)): extract_classes(
            path.read_text(encoding="utf-8")
        )
        for path in sorted(TEMPLATES_DIR.rglob("*.html"))
    }


def test_every_template_class_is_in_app_css():
    assert APP_CSS_PATH.exists(), (
        f"{APP_CSS_PATH} is missing; run the Tailwind build "
        "(see web_interface/tailwind.config.js's header comment)"
    )
    css_text = APP_CSS_PATH.read_text(encoding="utf-8")
    assert css_text.strip(), f"{APP_CSS_PATH} is empty; run the Tailwind build"

    per_template = all_template_classes()
    assert per_template, f"no *.html templates found under {TEMPLATES_DIR}"

    all_classes: set[str] = set()
    for classes in per_template.values():
        all_classes |= classes
    assert all_classes, "no plausible class tokens were extracted from any template"

    missing_by_template: dict[str, list[str]] = {}
    for template, classes in per_template.items():
        template_missing = sorted(
            c for c in classes if not class_is_covered(c, css_text)
        )
        if template_missing:
            missing_by_template[template] = template_missing

    assert not missing_by_template, (
        "classes used in templates but absent from web_interface/static/app.css "
        "(rebuild it, or add a safelist entry in web_interface/tailwind.config.js "
        f"for a class assembled at render time): {missing_by_template}"
    )


def test_checker_reports_a_fabricated_class_as_missing():
    """Guard against a hollow test: a class that does not exist anywhere in
    Tailwind must be reported missing, so an empty or stale app.css can
    never pass the coverage test silently."""
    css_text = APP_CSS_PATH.read_text(encoding="utf-8")
    fake_class = "not-a-real-tailwind-class-xyz"
    assert not class_is_covered(fake_class, css_text)


def test_extractor_skips_only_jinja_fragments():
    """Guard on the extractor itself: literal classes survive, Jinja
    fragments (braces/quotes) are skipped, exactly per the module docstring
    rule above — nothing else."""
    html = (
        '<div class="p-2 {{ extra }}">'
        "<span class=\"{{ 'a' if x else 'b' }}\">"
        '<i class="rounded-xl border shadow-sm">'
    )
    classes = extract_classes(html)
    assert classes == {"p-2", "rounded-xl", "border", "shadow-sm"}


def test_app_css_carries_touch_feedback_rules():
    """The touch-feedback rules live in @layer base of tailwind.input.css
    (spec 2026-09-30 §4.1). They are element selectors, not classes, so the
    coverage test above cannot see them; this catches a source edit that
    was never rebuilt into the committed app.css."""
    css = APP_CSS_PATH.read_text(encoding="utf-8")
    for needle in (
        ":active",
        ".htmx-request",
        ":focus-visible",
        "accent-color:",
        "-webkit-tap-highlight-color:transparent",
        "touch-action:manipulation",
        "@keyframes spin",
        "@media (hover:hover)",
    ):
        assert needle in css, f"{needle!r} missing from app.css — rebuild it"
