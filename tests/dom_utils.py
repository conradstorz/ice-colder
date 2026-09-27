"""A tiny, real HTML parser for tests that need to assert on element
*structure* (tag names, attributes, nesting) rather than grep a substring
out of a response body. Stdlib-only (html.parser) — no new dependency.

Substring/regex assertions on rendered HTML ("assert 'hx-get=\"/pill\"' in
resp.text") can't tell an element apart from its neighbours and can't see
whether it's nested inside another one; they also can't distinguish "this
attribute is present with this exact value" from "this attribute name
happens to appear somewhere in the response". Use this instead whenever a
test cares about which element something is on, how many of it there are,
or what's inside what.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser

# Tags that never get a matching end tag, so the open/close stack tracker
# below must not push them.
_VOID_TAGS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)


@dataclass
class Element:
    tag: str
    attrs: dict[str, str | None] = field(default_factory=dict)
    ancestors: tuple[str, ...] = ()

    def is_within(self, tag: str) -> bool:
        """True if an ancestor (not self) has this tag name."""
        return tag in self.ancestors


class _DomCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.stack: list[str] = []
        self.elements: list[Element] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.elements.append(Element(tag, dict(attrs), tuple(self.stack)))
        if tag not in _VOID_TAGS:
            self.stack.append(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.elements.append(Element(tag, dict(attrs), tuple(self.stack)))

    def handle_endtag(self, tag: str) -> None:
        if tag not in self.stack:
            return
        while self.stack and self.stack[-1] != tag:
            self.stack.pop()
        if self.stack:
            self.stack.pop()


def parse_elements(html: str) -> list[Element]:
    """Every start tag in *html*, in document order, each carrying its own
    attributes and the tag names of every ancestor it was nested inside."""
    collector = _DomCollector()
    collector.feed(html)
    return collector.elements


def find_by_id(elements: list[Element], element_id: str) -> list[Element]:
    return [e for e in elements if e.attrs.get("id") == element_id]
