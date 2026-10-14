import shutil

import pytest
from jinja2.sandbox import SandboxedEnvironment

from ice_cloud.ui_registry import UiRegistry
from ice_ui.bundle import INSTALLED_ROOT, BundleError, build_bundle, ui_digest

MARKER = "MARKER-FROM-MACHINE-UI"


@pytest.fixture
def cloud_templates(tmp_path):
    d = tmp_path / "cloud_templates"
    d.mkdir()
    return d


@pytest.fixture
def registry(tmp_path, cloud_templates):
    return UiRegistry(tmp_path / "data", cloud_templates=cloud_templates)


@pytest.fixture
def marked_bundle(tmp_path):
    """A bundle of a copy of ice_ui whose status fragment carries MARKER."""
    root = tmp_path / "ice_ui_copy"
    shutil.copytree(INSTALLED_ROOT / "templates", root / "templates")
    shutil.copytree(INSTALLED_ROOT / "static", root / "static")
    fragment = root / "templates" / "partials" / "status_fragment.html"
    fragment.write_text(
        fragment.read_text(encoding="utf-8") + f"\n<!-- {MARKER} -->\n",
        encoding="utf-8",
    )
    data = build_bundle(root)
    import gzip
    import io
    import json
    import tarfile

    with tarfile.open(fileobj=io.BytesIO(gzip.decompress(data))) as tf:
        digest = json.loads(tf.extractfile("manifest.json").read())["digest"]
    return digest, data


def test_installed_digest_is_not_a_mismatch(registry):
    resolved = registry.resolve(ui_digest())
    assert resolved.mismatch is False
    assert resolved.digest == registry.installed_digest == ui_digest()


def test_unknown_digest_falls_back_to_installed_with_mismatch(registry):
    resolved = registry.resolve("0" * 64)
    assert resolved.mismatch is True
    assert resolved.digest == ui_digest()
    assert not isinstance(resolved.templates.env, SandboxedEnvironment)


def test_garbage_digest_is_a_mismatch_not_an_error(registry):
    assert registry.resolve("../../etc").mismatch is True


def test_registered_bundle_renders_its_own_template_sandboxed(registry, marked_bundle):
    digest, data = marked_bundle
    assert registry.register(digest, data) is True
    assert registry.register(digest, data) is False

    resolved = registry.resolve(digest)
    assert resolved.mismatch is False
    assert isinstance(resolved.templates.env, SandboxedEnvironment)
    source = resolved.templates.env.loader.get_source(
        resolved.templates.env, "partials/status_fragment.html"
    )[0]
    assert MARKER in source
    # The installed UI is untouched.
    installed = registry.resolve(ui_digest())
    assert (
        MARKER
        not in installed.templates.env.loader.get_source(
            installed.templates.env, "partials/status_fragment.html"
        )[0]
    )
    assert resolved.static_dir.name == "static"
    assert resolved.templates is registry.resolve(digest).templates


def test_register_rejects_corrupt_bytes_and_wrong_digest(registry, marked_bundle):
    digest, data = marked_bundle
    with pytest.raises(BundleError):
        registry.register(digest, b"not a tarball")
    with pytest.raises(BundleError):
        registry.register("f" * 64, data)
    with pytest.raises(BundleError):
        registry.register("../escape", data)
    assert registry.resolve(digest).mismatch is True
