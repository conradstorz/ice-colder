"""UI digest and bundle build/extract (cloud spec section 3)."""

import gzip
import hashlib
import io
import json
import re
import shutil
import tarfile
from importlib.metadata import version
from pathlib import Path

import pytest

from ice_ui import bundle
from ice_ui.bundle import (
    BundleError,
    build_bundle,
    compute_digest,
    extract_bundle,
    levels_manifest,
    ui_digest,
)
from ice_ui.levels import LEVEL_HEALTH_FAULTS, LEVEL_HOME
from ice_ui.templating import STATIC_DIR, TEMPLATES_DIR


@pytest.fixture
def ui_copy(tmp_path) -> Path:
    root = tmp_path / "ice_ui_copy"
    shutil.copytree(TEMPLATES_DIR, root / "templates")
    shutil.copytree(STATIC_DIR, root / "static")
    return root


def _tar_gz(members: list[tuple[tarfile.TarInfo, bytes | None]]) -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tf:
        for info, body in members:
            tf.addfile(info, io.BytesIO(body) if body is not None else None)
    return gzip.compress(raw.getvalue(), mtime=0)


def _file(name: str, body: bytes) -> tuple[tarfile.TarInfo, bytes]:
    info = tarfile.TarInfo(name)
    info.size = len(body)
    return info, body


def _valid_members() -> list[tuple[str, bytes]]:
    """(name, bytes) of every member of a genuine bundle."""
    out = []
    with tarfile.open(fileobj=io.BytesIO(build_bundle()), mode="r:gz") as tf:
        for m in tf:
            out.append((m.name, tf.extractfile(m).read()))
    return out


# --- levels manifest and digest ------------------------------------------


def test_version_matches_installed_metadata():
    assert bundle.ICE_UI_VERSION == version("ice-ui")


def test_levels_manifest_is_sorted_and_covers_module_levels():
    entries = levels_manifest()
    urls = [e.url for e in entries]
    assert urls == sorted(urls)
    by_url = {e.url: e for e in entries}
    assert by_url[LEVEL_HOME.url].parent_url is None
    assert by_url[LEVEL_HEALTH_FAULTS.url].parent_url == "/health"
    assert by_url[LEVEL_HEALTH_FAULTS.url].title == "Faults"
    assert len(entries) >= 29  # spec: 29 named levels


def test_compute_digest_is_stable_and_hex(ui_copy):
    levels = levels_manifest()
    first = compute_digest(ui_copy, levels)
    assert first == compute_digest(ui_copy, levels)
    assert re.fullmatch(r"[0-9a-f]{64}", first)


def test_compute_digest_matches_spec_formula(tmp_path):
    root = tmp_path / "r"
    (root / "templates").mkdir(parents=True)
    (root / "static").mkdir()
    (root / "templates" / "b.html").write_bytes(b"B")
    (root / "templates" / "a.html").write_bytes(b"A")
    (root / "static" / "x.js").write_bytes(b"X")
    (root / "ignored.txt").write_bytes(b"nope")
    levels = levels_manifest()[:2]
    h = hashlib.sha256()
    for rel, body in [
        ("static/x.js", b"X"),
        ("templates/a.html", b"A"),
        ("templates/b.html", b"B"),
    ]:
        h.update(rel.encode() + b"\0" + body + b"\0")
    h.update(
        json.dumps(
            [e.model_dump() for e in levels], sort_keys=True, separators=(",", ":")
        ).encode()
    )
    assert compute_digest(root, levels) == h.hexdigest()


def test_digest_changes_when_one_template_byte_changes(ui_copy):
    levels = levels_manifest()
    before = compute_digest(ui_copy, levels)
    target = ui_copy / "templates" / "base.html"
    body = bytearray(target.read_bytes())
    body[0] ^= 0x01
    target.write_bytes(bytes(body))
    assert compute_digest(ui_copy, levels) != before


def test_digest_changes_when_a_level_changes(ui_copy):
    levels = levels_manifest()
    changed = [levels[0].model_copy(update={"title": "Renamed"}), *levels[1:]]
    assert compute_digest(ui_copy, levels) != compute_digest(ui_copy, changed)


def test_ui_digest_is_cached_and_equals_installed_tree():
    assert ui_digest() is ui_digest()
    assert ui_digest() == compute_digest(bundle.INSTALLED_ROOT, levels_manifest())


# --- build / extract round trip ---------------------------------------


def test_extract_bundle_round_trips(tmp_path):
    data = build_bundle()
    dest = tmp_path / "ui" / ui_digest()
    manifest = extract_bundle(data, dest, expected_digest=ui_digest())
    assert manifest.format == 1
    assert manifest.digest == ui_digest()
    assert manifest.ice_ui_version == bundle.ICE_UI_VERSION
    assert manifest.levels == levels_manifest()
    assert compute_digest(dest, manifest.levels) == ui_digest()
    assert (dest / "templates" / "base.html").read_bytes() == (
        TEMPLATES_DIR / "base.html"
    ).read_bytes()
    assert not [p for p in dest.parent.iterdir() if p.name != dest.name]


def test_build_bundle_is_deterministic():
    assert build_bundle() == build_bundle()


def test_build_bundle_from_a_modified_root_has_its_own_digest(ui_copy, tmp_path):
    (ui_copy / "templates" / "base.html").write_bytes(b"<html>changed</html>")
    data = build_bundle(ui_copy)
    manifest = extract_bundle(data, tmp_path / "out")
    assert manifest.digest != ui_digest()
    assert (tmp_path / "out" / "templates" / "base.html").read_bytes() == (
        b"<html>changed</html>"
    )


# --- rejections ----------------------------------------------------------


def _assert_rejected(tmp_path, data, match):
    dest = tmp_path / "ui" / "d"
    with pytest.raises(BundleError, match=match):
        extract_bundle(data, dest)
    assert not dest.exists()
    leftovers = list((tmp_path / "ui").iterdir()) if (tmp_path / "ui").exists() else []
    assert leftovers == [], "a rejected bundle must leave no temp directory behind"


def test_rejects_absolute_path(tmp_path):
    data = _tar_gz([_file("/etc/passwd", b"x")])
    _assert_rejected(tmp_path, data, "absolute path")


def test_rejects_dotdot_component(tmp_path):
    data = _tar_gz([_file("templates/../../x", b"x")])
    _assert_rejected(tmp_path, data, r"\.\.")


def test_rejects_symlink_member(tmp_path):
    link = tarfile.TarInfo("templates/link")
    link.type = tarfile.SYMTYPE
    link.linkname = "/etc/passwd"
    _assert_rejected(tmp_path, _tar_gz([(link, None)]), "link member")


def test_rejects_hardlink_member(tmp_path):
    link = tarfile.TarInfo("templates/hard")
    link.type = tarfile.LNKTYPE
    link.linkname = "templates/base.html"
    _assert_rejected(tmp_path, _tar_gz([(link, None)]), "link member")


def test_rejects_non_regular_member(tmp_path):
    directory = tarfile.TarInfo("templates")
    directory.type = tarfile.DIRTYPE
    _assert_rejected(tmp_path, _tar_gz([(directory, None)]), "non-regular")


def test_rejects_member_outside_the_three_roots(tmp_path):
    data = _tar_gz([_file("other/x", b"x")])
    _assert_rejected(tmp_path, data, "outside")


def test_rejects_oversize_via_the_member_size_header(tmp_path):
    header = tarfile.TarInfo("static/huge.bin")
    header.size = bundle.MAX_UNCOMPRESSED_BYTES + 1
    # Header only: the declared size alone must trigger the rejection, long
    # before any of the (absent) data would be read.
    data = gzip.compress(header.tobuf(), mtime=0)
    _assert_rejected(tmp_path, data, "exceeds")


def test_rejects_cumulative_oversize(tmp_path):
    half = bundle.MAX_UNCOMPRESSED_BYTES // 2 + 1
    data = _tar_gz(
        [_file("static/a.bin", b"\0" * half), _file("static/b.bin", b"\0" * half)]
    )
    _assert_rejected(tmp_path, data, "exceeds")


def test_rejects_digest_mismatch_with_manifest(tmp_path):
    members = dict(_valid_members())
    members["templates/base.html"] += b"<!-- tampered -->"
    data = _tar_gz([_file(name, body) for name, body in members.items()])
    _assert_rejected(tmp_path, data, "digest mismatch")


def test_rejects_when_url_digest_differs_from_contents(tmp_path):
    dest = tmp_path / "ui" / "d"
    with pytest.raises(BundleError, match="digest mismatch"):
        extract_bundle(build_bundle(), dest, expected_digest="0" * 64)
    assert not dest.exists()


def test_rejects_missing_manifest(tmp_path):
    data = _tar_gz([_file("templates/a.html", b"x")])
    _assert_rejected(tmp_path, data, "manifest.json missing")


def test_rejects_invalid_manifest(tmp_path):
    data = _tar_gz([_file("manifest.json", b'{"format": 2}')])
    _assert_rejected(tmp_path, data, "invalid manifest.json")


def test_rejects_duplicate_member(tmp_path):
    data = _tar_gz([_file("templates/a.html", b"1"), _file("templates/a.html", b"2")])
    _assert_rejected(tmp_path, data, "duplicate")


def test_rejects_non_archive(tmp_path):
    _assert_rejected(tmp_path, b"definitely not gzip", "not a gzip tar|corrupt")


def test_rejects_existing_destination(tmp_path):
    dest = tmp_path / "taken"
    dest.mkdir()
    with pytest.raises(BundleError, match="already exists"):
        extract_bundle(build_bundle(), dest)


# --- member-count caps and prepare_bundle ----------------------------------


def test_rejects_more_than_max_members(tmp_path):
    members = [_file(f"static/f{i}", b"") for i in range(bundle.MAX_MEMBERS + 1)]
    _assert_rejected(tmp_path, _tar_gz(members), "members")


def test_rejects_overlong_member_name(tmp_path):
    name = "static/" + "a" * (bundle.MAX_MEMBER_NAME_CHARS + 1 - len("static/"))
    assert len(name) == bundle.MAX_MEMBER_NAME_CHARS + 1
    data = _tar_gz([_file(name, b"x")])
    _assert_rejected(tmp_path, data, "too long")


def test_prepare_bundle_returns_a_renameable_directory(tmp_path):
    data = build_bundle()
    parent = tmp_path / "ui"
    tmp, manifest = bundle.prepare_bundle(data, parent, expected_digest=ui_digest())
    assert manifest.digest == ui_digest()
    assert (tmp / "templates").is_dir()
    assert tmp.parent == parent
    tmp.rename(parent / "final")
    assert (parent / "final" / "static").is_dir()


def test_failing_prepare_bundle_leaves_no_temp_directory(tmp_path):
    parent = tmp_path / "ui"
    with pytest.raises(BundleError):
        bundle.prepare_bundle(b"garbage", parent, expected_digest=None)
    assert list(parent.iterdir()) == []
