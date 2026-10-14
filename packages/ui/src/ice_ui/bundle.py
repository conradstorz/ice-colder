"""UI digest and UI bundle (cloud spec section 3).

The digest identifies one exact UI: every file under ``templates/`` and
``static/`` plus the levels manifest (breadcrumbs and Back links come from
Python, not from template files). A machine reports its digest; the cloud can
render with its own ``ice_ui`` when they match, or with an uploaded bundle of
the machine's own UI when they do not.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import shutil
import tarfile
import tempfile
from functools import cache
from importlib.metadata import version
from pathlib import Path, PurePosixPath

from ice_contracts.cloud.ui import BundleManifest, LevelEntry
from ice_ui import levels as _levels_module

ICE_UI_VERSION: str = version("ice-ui")

INSTALLED_ROOT: Path = Path(__file__).parent
MANIFEST_NAME = "manifest.json"
BUNDLE_ROOTS = ("templates", "static")
MAX_UNCOMPRESSED_BYTES = 20 * 1024 * 1024


class BundleError(ValueError):
    """An uploaded bundle was rejected; the message says why."""


def levels_manifest() -> list[LevelEntry]:
    """Every module-level ``Level`` in ``ice_ui.levels``, sorted by url."""
    entries = [
        LevelEntry(
            title=lv.title,
            url=lv.url,
            parent_url=lv.parent.url if lv.parent is not None else None,
        )
        for lv in vars(_levels_module).values()
        if isinstance(lv, _levels_module.Level)
    ]
    return sorted(entries, key=lambda e: e.url)


def _bundle_files(root: Path) -> list[tuple[str, Path]]:
    """``(posix relpath, path)`` for every file under templates/ and static/,
    sorted by relpath."""
    found: list[tuple[str, Path]] = []
    for top in BUNDLE_ROOTS:
        base = root / top
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if path.is_file():
                found.append((path.relative_to(root).as_posix(), path))
    return sorted(found)


def compute_digest(root: Path, levels: list[LevelEntry]) -> str:
    """sha256 over the sorted (posix relpath, NUL, file bytes, NUL) of every
    file under ``root/templates`` and ``root/static``, followed by the
    canonical JSON of *levels*. Bytes are hashed as found (no newline
    normalisation)."""
    h = hashlib.sha256()
    for rel, path in _bundle_files(root):
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(path.read_bytes())
        h.update(b"\0")
    canonical = json.dumps(
        [e.model_dump(mode="json") for e in levels],
        sort_keys=True,
        separators=(",", ":"),
    )
    h.update(canonical.encode("utf-8"))
    return h.hexdigest()


@cache
def ui_digest() -> str:
    """The installed ``ice_ui``'s digest (computed once per process)."""
    return compute_digest(INSTALLED_ROOT, levels_manifest())


def build_bundle(root: Path | None = None) -> bytes:
    """A deterministic tar.gz of ``manifest.json`` + ``templates/**`` +
    ``static/**`` from *root* (default: the installed ``ice_ui``)."""
    root = INSTALLED_ROOT if root is None else root
    levels = levels_manifest()
    manifest = BundleManifest(
        format=1,
        digest=compute_digest(root, levels),
        ice_ui_version=ICE_UI_VERSION,
        levels=levels,
    )
    members: list[tuple[str, bytes]] = [
        (MANIFEST_NAME, manifest.model_dump_json().encode("utf-8"))
    ]
    members.extend((rel, path.read_bytes()) for rel, path in _bundle_files(root))

    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tf:
        for name, data in members:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mtime = 0
            info.mode = 0o644
            tf.addfile(info, io.BytesIO(data))
    return gzip.compress(raw.getvalue(), mtime=0)


def _check_member_name(name: str) -> None:
    if not name or "\\" in name or "\0" in name:
        raise BundleError(f"unsafe member name: {name!r}")
    path = PurePosixPath(name)
    if path.is_absolute() or name.startswith("/"):
        raise BundleError(f"absolute path in bundle: {name!r}")
    if ":" in path.parts[0]:
        raise BundleError(f"absolute path in bundle: {name!r}")
    if any(part in ("", ".", "..") for part in name.split("/")):
        raise BundleError(f"path component '..' or '.' in bundle: {name!r}")
    if name != MANIFEST_NAME and not (
        path.parts[0] in BUNDLE_ROOTS and len(path.parts) > 1
    ):
        raise BundleError(f"member outside templates/, static/ and manifest: {name!r}")


def extract_bundle(
    data: bytes, dest: Path, *, expected_digest: str | None = None
) -> BundleManifest:
    """Validate and extract a UI bundle into *dest* (which must not exist).

    Every spec section 3 rejection raises ``BundleError`` with a reason:
    absolute paths, ``..`` components, symlinks or hardlinks, non-regular
    members, members outside ``manifest.json``/``templates/``/``static/``,
    total uncompressed size over 20 MB, and a digest recomputed from the
    extracted files plus the manifest's ``levels`` that differs from the
    manifest's own ``digest`` (and from *expected_digest*, the URL's digest,
    when given). Extraction goes to a temporary sibling directory that is
    renamed to *dest* only after every check passes."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        raise BundleError(f"destination already exists: {dest}")
    tmp = Path(tempfile.mkdtemp(prefix=f".{dest.name}.", dir=dest.parent))
    try:
        manifest = _extract_into(data, tmp, expected_digest)
        os.replace(tmp, dest)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return manifest


def _extract_into(
    data: bytes, tmp: Path, expected_digest: str | None
) -> BundleManifest:
    seen: set[str] = set()
    total = 0
    manifest_bytes: bytes | None = None
    try:
        tf = tarfile.open(fileobj=io.BytesIO(data), mode="r:gz")
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise BundleError(f"not a gzip tar archive: {exc}") from exc
    with tf:
        try:
            for member in tf:
                if member.issym() or member.islnk():
                    raise BundleError(f"link member in bundle: {member.name!r}")
                if not member.isreg():
                    raise BundleError(f"non-regular member in bundle: {member.name!r}")
                _check_member_name(member.name)
                if member.name in seen:
                    raise BundleError(f"duplicate member: {member.name!r}")
                seen.add(member.name)
                total += member.size
                if total > MAX_UNCOMPRESSED_BYTES:
                    raise BundleError(
                        f"bundle exceeds {MAX_UNCOMPRESSED_BYTES} uncompressed bytes"
                    )
                fh = tf.extractfile(member)
                assert fh is not None  # isreg() guarantees a file object
                body = fh.read()
                if member.name == MANIFEST_NAME:
                    manifest_bytes = body
                    continue
                target = tmp / member.name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(body)
        except (tarfile.TarError, OSError, EOFError) as exc:
            raise BundleError(f"corrupt archive: {exc}") from exc

    if manifest_bytes is None:
        raise BundleError(f"{MANIFEST_NAME} missing from bundle")
    try:
        manifest = BundleManifest.model_validate_json(manifest_bytes)
    except ValueError as exc:  # pydantic.ValidationError is a ValueError
        raise BundleError(f"invalid {MANIFEST_NAME}: {exc}") from exc

    actual = compute_digest(tmp, manifest.levels)
    if actual != manifest.digest:
        raise BundleError(
            f"digest mismatch: manifest says {manifest.digest}, contents hash to {actual}"
        )
    if expected_digest is not None and expected_digest != actual:
        raise BundleError(
            f"digest mismatch: expected {expected_digest}, contents hash to {actual}"
        )
    return manifest
