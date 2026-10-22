import shutil

import pytest
from jinja2.sandbox import SandboxedEnvironment

from ice_cloud.ui_registry import UiRegistry
from tests.cloud.reports import make_report
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


@pytest.fixture
def trusted_registry(tmp_path, cloud_templates, marked_bundle):
    return UiRegistry(
        tmp_path / "data",
        cloud_templates=cloud_templates,
        trusted_digests={marked_bundle[0]},
    )


def _render_fragment(resolved) -> str:
    return resolved.templates.get_template("partials/status_fragment.html").render(
        request=None,
        status=make_report().health.status,
        is_healthy=True,
        issues=[],
        active_faults=[],
        payment_enabled=True,
        payment_reasons=[],
        machine_stopped=False,
        maintenance=None,
    )


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


def test_registered_bundle_renders_its_own_template_sandboxed(
    trusted_registry, marked_bundle
):
    registry = trusted_registry
    digest, data = marked_bundle
    assert registry.register(digest, data) is True
    assert registry.register(digest, data) is False

    resolved = registry.resolve(digest)
    assert resolved.mismatch is False
    assert isinstance(resolved.templates.env, SandboxedEnvironment)
    assert MARKER in _render_fragment(resolved)
    # The installed UI is untouched.
    assert MARKER not in _render_fragment(registry.resolve(ui_digest()))
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


def test_installed_ui_keeps_request(trusted_registry, marked_bundle):
    registry = trusted_registry
    digest, data = marked_bundle
    registry.register(digest, data)
    installed = registry.resolve(ui_digest())
    assert installed.sandboxed is False
    assert (
        installed.templates.env.from_string("{{ request }}").render(request="R") == "R"
    )
    bundle = registry.resolve(digest)
    assert bundle.sandboxed is True
    assert bundle.templates.env.from_string("{{ request }}").render(request="R") == ""


def test_untrusted_uploaded_bundle_resolves_to_installed_with_mismatch(
    registry, marked_bundle
):
    digest, data = marked_bundle
    assert registry.register(digest, data) is True
    resolved = registry.resolve(digest)
    assert resolved.mismatch is True and resolved.sandboxed is False
    assert resolved.digest == ui_digest()
    assert MARKER not in _render_fragment(resolved)


def test_trusted_uploaded_bundle_renders_sandboxed(
    tmp_path, cloud_templates, marked_bundle
):
    digest, data = marked_bundle
    trusted = UiRegistry(
        tmp_path / "data", cloud_templates=cloud_templates, trusted_digests={digest}
    )
    trusted.register(digest, data)
    resolved = trusted.resolve(digest)
    assert resolved.sandboxed is True and resolved.mismatch is False
    assert MARKER in _render_fragment(resolved)


def test_concurrent_registers_store_once(
    registry, marked_bundle, tmp_path, monkeypatch
):
    import threading

    import ice_cloud.ui_registry as ui_registry

    digest, data = marked_bundle
    start = threading.Barrier(2)
    extracted = threading.Barrier(2)
    real = ui_registry.prepare_bundle

    def wrapped(*args, **kwargs):
        result = real(*args, **kwargs)
        extracted.wait(timeout=10)  # both finish extraction before either locks
        return result

    monkeypatch.setattr(ui_registry, "prepare_bundle", wrapped)
    results: list[bool] = []

    def worker():
        start.wait(timeout=10)
        results.append(registry.register(digest, data))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not any(t.is_alive() for t in threads)
    assert sorted(results) == [False, True]
    assert registry.has(digest)
    assert [p.name for p in (tmp_path / "data" / "ui").iterdir()] == [digest]


def test_init_sweeps_stale_temp_dirs(tmp_path, cloud_templates):
    ui = tmp_path / "data" / "ui"
    (ui / ".abc.tmp").mkdir(parents=True)
    (ui / ".abc.tmp" / "f").write_text("x")
    (ui / ("a" * 64)).mkdir()
    UiRegistry(tmp_path / "data", cloud_templates=cloud_templates)
    assert [p.name for p in ui.iterdir()] == ["a" * 64]


def test_register_extracts_outside_the_resolve_lock(
    registry, marked_bundle, monkeypatch
):
    import threading

    import ice_cloud.ui_registry as ui_registry

    digest, data = marked_bundle
    entered = threading.Event()
    release = threading.Event()
    real = ui_registry.prepare_bundle

    def slow(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=30)
        return real(*args, **kwargs)

    monkeypatch.setattr(ui_registry, "prepare_bundle", slow)
    uploader = threading.Thread(target=registry.register, args=(digest, data))
    uploader.start()
    assert entered.wait(timeout=10)

    resolved: list[object] = []
    reader = threading.Thread(
        target=lambda: resolved.append(registry.resolve(registry.installed_digest))
    )
    reader.start()
    reader.join(timeout=10)
    finished = not reader.is_alive()
    release.set()
    uploader.join(timeout=30)
    assert finished, "resolve blocked while a bundle was being extracted"
    assert resolved
