"""Touch-feedback assets: the click sound, its generator, and the
no-<audio>-markup guard (script behaviour is proven in real Chrome by
tests/edge/web/test_touch_feedback_browser.py). See docs/superpowers/specs/2026-09-30-touch-feedback-design.md §4.2–4.4."""

from __future__ import annotations

import importlib.util
import io
import wave

from fastapi.testclient import TestClient

from ice_edge.web.server import app
from tests.paths import REPO_ROOT, STATIC_DIR, TEMPLATES_DIR

ROOT = REPO_ROOT
STATIC = STATIC_DIR
CLICK_WAV = STATIC / "click.wav"
GENERATOR = ROOT / "scripts" / "make_click_wav.py"


def _load_generator():
    spec = importlib.util.spec_from_file_location("make_click_wav", GENERATOR)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _wav_params(data: bytes):
    with wave.open(io.BytesIO(data)) as w:
        return w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()


def test_click_wav_is_a_short_mono_16bit_click():
    assert CLICK_WAV.exists(), "run: uv run python scripts/make_click_wav.py"
    data = CLICK_WAV.read_bytes()
    channels, width, rate, frames = _wav_params(data)
    assert channels == 1
    assert width == 2
    assert rate == 22050
    duration_ms = frames * 1000 / rate
    assert 10 <= duration_ms <= 100, duration_ms
    assert len(data) < 5000, len(data)


def test_click_wav_is_not_silent():
    with wave.open(str(CLICK_WAV)) as w:
        raw = w.readframes(w.getnframes())
    peak = max(
        abs(int.from_bytes(raw[i : i + 2], "little", signed=True))
        for i in range(0, len(raw), 2)
    )
    assert peak > 8000, "click is inaudibly quiet"


def test_generator_is_reproducible(tmp_path):
    mod = _load_generator()
    assert mod.render_click() == mod.render_click()
    out = tmp_path / "click.wav"
    mod.main(out)
    assert out.read_bytes() == CLICK_WAV.read_bytes(), (
        "committed click.wav differs from the generator's output; regenerate it"
    )


BASE_HTML = TEMPLATES_DIR / "base.html"


def test_boosted_swap_cannot_duplicate_audio():
    # No <audio> markup anywhere in base.html: the script creates its own
    # Audio object once, so a boosted outerHTML swap of <main> can never
    # insert a second one.
    html = BASE_HTML.read_text(encoding="utf-8")
    assert "tap-sound" not in html
    assert "<audio" not in html


def test_static_feedback_files_are_served():
    client = TestClient(app)
    wav = client.get("/static/click.wav")
    assert wav.status_code == 200
    assert wav.headers["content-type"].startswith("audio/")
    js = client.get("/static/feedback.js")
    assert js.status_code == 200
    assert "javascript" in js.headers["content-type"]
