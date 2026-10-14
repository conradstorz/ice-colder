"""Resolve which UI renders a machine's pages (cloud spec section 3).

The installed ``ice_ui`` when the reported digest matches; else a cached,
uploaded bundle for that digest (rendered in a sandbox); else the installed
``ice_ui`` flagged as a mismatch.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from pathlib import Path

from fastapi.templating import Jinja2Templates

from ice_contracts.cloud.ui import DIGEST_PATTERN
from ice_ui.bundle import BundleError, extract_bundle, ui_digest
from ice_ui.templating import STATIC_DIR, TEMPLATES_DIR, make_templates

_DIGEST_RE = re.compile(DIGEST_PATTERN)


@dataclass(frozen=True)
class ResolvedUi:
    templates: Jinja2Templates
    static_dir: Path
    mismatch: bool
    digest: str


class UiRegistry:
    def __init__(self, root: Path, *, cloud_templates: Path) -> None:
        self._ui_dir = Path(root) / "ui"
        self._cloud_templates = cloud_templates
        self._lock = threading.Lock()
        self._cache: dict[str, Jinja2Templates] = {}
        self.installed_digest: str = ui_digest()

    def _bundle_dir(self, digest: str) -> Path:
        if not _DIGEST_RE.fullmatch(digest):
            raise BundleError(f"not a sha256 hex digest: {digest!r}")
        return self._ui_dir / digest

    def register(self, digest: str, data: bytes) -> bool:
        """Validate and store an uploaded bundle; ``True`` if newly stored,
        ``False`` if that digest is already present. ``BundleError`` on any
        rejection."""
        dest = self._bundle_dir(digest)
        with self._lock:
            if dest.is_dir():
                return False
            extract_bundle(data, dest, expected_digest=digest)
            return True

    def _templates_for(self, digest: str, factory) -> Jinja2Templates:
        with self._lock:
            if digest not in self._cache:
                self._cache[digest] = factory()
            return self._cache[digest]

    def resolve(self, digest: str) -> ResolvedUi:
        if digest == self.installed_digest:
            return self._installed(mismatch=False)
        cached = self._ui_dir / digest if _DIGEST_RE.fullmatch(digest) else None
        if cached is not None and (cached / "templates").is_dir():
            templates = self._templates_for(
                digest,
                lambda: make_templates(
                    cached / "templates",
                    sandboxed=True,
                    extra_dirs=[self._cloud_templates],
                ),
            )
            return ResolvedUi(templates, cached / "static", False, digest)
        return self._installed(mismatch=True)

    def _installed(self, *, mismatch: bool) -> ResolvedUi:
        templates = self._templates_for(
            self.installed_digest,
            lambda: make_templates(TEMPLATES_DIR, extra_dirs=[self._cloud_templates]),
        )
        return ResolvedUi(templates, STATIC_DIR, mismatch, self.installed_digest)
