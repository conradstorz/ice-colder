"""Touch-feedback assets: the click sound, its generator, and (Task 2) the
script and base.html wiring. See docs/superpowers/specs/2026-09-30-touch-feedback-design.md §4.2–4.4."""

from __future__ import annotations

import importlib.util
import io
import wave
from pathlib import Path

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
