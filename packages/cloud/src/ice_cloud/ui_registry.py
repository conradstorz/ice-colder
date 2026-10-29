"""Resolve which UI renders a machine's pages (cloud spec section 3).

The installed ``ice_ui`` when the reported digest matches; else a cached,
uploaded bundle for that digest, only if the operator listed the digest as
trusted (rendered in a sandbox); else the installed ``ice_ui`` flagged as a
mismatch. An untrusted upload is stored but never rendered or served: its
static files would be same-origin HTML on the cloud's origin.
"""

from __future__ import annotations

import os
import re
import time
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path

from loguru import logger
from fastapi.templating import Jinja2Templates
from jinja2 import Template

from ice_contracts.cloud.ui import DIGEST_PATTERN
from ice_ui.bundle import BundleError, prepare_bundle, ui_digest
from ice_ui.templating import STATIC_DIR, TEMPLATES_DIR, make_templates

# Temp dirs younger than this may belong to an in-flight extraction.
STALE_TEMP_SECONDS = 600

# Each extraction can hold ~64 MiB; on a 512 MB host more than a couple at once
# risks the OOM killer. Renders take ``_lock`` and are never held up by this.
MAX_CONCURRENT_EXTRACTIONS = 2

_DIGEST_RE = re.compile(DIGEST_PATTERN)


class _NoRequestTemplate(Template):
    """A template whose context never carries ``request``.

    Starlette's ``TemplateResponse`` always injects ``request``; an uploaded
    (untrusted) bundle must not reach ``request.app.state``. Dropping it at
    ``new_context`` covers the page itself and every include/extends/import.
    """

    def new_context(self, vars=None, shared=False, locals=None):
        if vars and "request" in vars:
            vars = {k: v for k, v in vars.items() if k != "request"}
        return super().new_context(vars, shared, locals)


def _sandboxed_templates(directory: Path, extra_dirs: list[Path]) -> Jinja2Templates:
    result = make_templates(directory, sandboxed=True, extra_dirs=extra_dirs)
    result.env.template_class = _NoRequestTemplate
    result.env.globals["page_source"] = "remote"
    return result


@dataclass(frozen=True)
class ResolvedUi:
    templates: Jinja2Templates
    static_dir: Path
    mismatch: bool
    digest: str
    sandboxed: bool = False


class UiRegistry:
    def __init__(
        self,
        root: Path,
        *,
        cloud_templates: Path,
        trusted_digests: frozenset[str] = frozenset(),
    ) -> None:
        self._trusted = frozenset(trusted_digests)
        self._ui_dir = Path(root) / "ui"
        self._cloud_templates = cloud_templates
        self._lock = threading.Lock()
        self._extractions = threading.BoundedSemaphore(MAX_CONCURRENT_EXTRACTIONS)
        self._cache: dict[str, Jinja2Templates] = {}
        self.installed_digest: str = ui_digest()
        self._sweep_stale_temp_dirs()

    def _sweep_stale_temp_dirs(self) -> None:
        """Remove ``.``-prefixed extraction leftovers from a crashed upload.

        Assumes one owning process per data dir; dirs touched within
        ``STALE_TEMP_SECONDS`` are skipped so an overlapping process's
        in-flight extraction survives."""
        if not self._ui_dir.is_dir():
            return
        cutoff = time.time() - STALE_TEMP_SECONDS
        stale = [
            p
            for p in self._ui_dir.glob(".*")
            if p.is_dir() and p.stat().st_mtime < cutoff
        ]
        for p in stale:
            shutil.rmtree(p, ignore_errors=True)
        if stale:
            logger.warning("removed {} stale bundle temp dir(s)", len(stale))

    def _bundle_dir(self, digest: str) -> Path:
        if not _DIGEST_RE.fullmatch(digest):
            raise BundleError(f"not a sha256 hex digest: {digest!r}")
        return self._ui_dir / digest

    def has(self, digest: str) -> bool:
        """Whether this bundle is already stored."""
        return (
            _DIGEST_RE.fullmatch(digest) is not None
            and (self._ui_dir / digest).is_dir()
        )

    def register(self, digest: str, data: bytes) -> bool:
        """Validate and store an uploaded bundle; ``True`` if newly stored,
        ``False`` if that digest is already present. ``BundleError`` on any
        rejection."""
        dest = self._bundle_dir(digest)
        if dest.is_dir():
            return False
        # Extraction is slow and untrusted: do it outside the lock that every
        # page render takes, and hold the lock only for the rename.
        with self._extractions:
            tmp, _manifest = prepare_bundle(data, self._ui_dir, expected_digest=digest)
        try:
            with self._lock:
                if dest.is_dir():
                    shutil.rmtree(tmp, ignore_errors=True)
                    return False
                os.replace(tmp, dest)
                return True
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise

    def _templates_for(self, digest: str, factory) -> Jinja2Templates:
        with self._lock:
            if digest not in self._cache:
                self._cache[digest] = factory()
            return self._cache[digest]

    def resolve(self, digest: str) -> ResolvedUi:
        if digest == self.installed_digest:
            return self._installed(mismatch=False)
        cached = self._ui_dir / digest if _DIGEST_RE.fullmatch(digest) else None
        if (
            cached is not None
            and digest in self._trusted
            and (cached / "templates").is_dir()
        ):
            templates = self._templates_for(
                digest,
                lambda: _sandboxed_templates(
                    cached / "templates", [self._cloud_templates]
                ),
            )
            return ResolvedUi(templates, cached / "static", False, digest, True)
        return self._installed(mismatch=True)

    def _installed_factory(self) -> Jinja2Templates:
        result = make_templates(TEMPLATES_DIR, extra_dirs=[self._cloud_templates])
        # Every cloud-rendered page (portal, fleet, admin, claim, login, me,
        # bundles) tells base.html it is the remote side; the edge passes
        # "local" through template_context.
        result.env.globals["page_source"] = "remote"
        return result

    def _installed(self, *, mismatch: bool) -> ResolvedUi:
        templates = self._templates_for(
            self.installed_digest, self._installed_factory
        )
        return ResolvedUi(templates, STATIC_DIR, mismatch, self.installed_digest)
