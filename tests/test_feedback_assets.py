"""Touch-feedback assets: the click sound, its generator, and (Task 2) the
script and base.html wiring. See docs/superpowers/specs/2026-09-30-touch-feedback-design.md §4.2–4.4."""

from __future__ import annotations

import importlib.util
import io
import wave
from pathlib import Path

from fastapi.testclient import TestClient

from web_interface.server import app

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "web_interface" / "static"
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
    assert (
        out.read_bytes() == CLICK_WAV.read_bytes()
    ), "committed click.wav differs from the generator's output; regenerate it"


FEEDBACK_JS = STATIC / "feedback.js"
BASE_HTML = ROOT / "web_interface" / "templates" / "base.html"


def test_feedback_js_exists_and_registers_pointerdown():
    src = FEEDBACK_JS.read_text(encoding="utf-8")
    assert "pointerdown" in src
    assert "touchstart" in src
    assert "tap-sound" in src
    assert ".play()" in src


def test_base_html_wires_audio_and_script():
    html = BASE_HTML.read_text(encoding="utf-8")
    assert 'id="tap-sound"' in html
    assert 'src="/static/click.wav"' in html
    assert 'preload="auto"' in html
    assert '<script src="/static/feedback.js" defer></script>' in html
    # the script must load after htmx so htmx-request styling and the sound
    # both exist by the time the first swap can happen
    assert html.index("htmx.min.js") < html.index("feedback.js")


def test_static_feedback_files_are_served():
    client = TestClient(app)
    wav = client.get("/static/click.wav")
    assert wav.status_code == 200
    assert wav.headers["content-type"].startswith("audio/")
    js = client.get("/static/feedback.js")
    assert js.status_code == 200
    assert "javascript" in js.headers["content-type"]
