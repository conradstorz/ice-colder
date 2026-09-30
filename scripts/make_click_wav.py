"""Generate the touch-feedback click sound as a short, deterministic WAV.

Produces a ~30 ms mono 16-bit click: seeded white noise shaped by an
envelope with a 1 ms linear attack followed by an exponential decay. See
docs/superpowers/specs/2026-09-30-touch-feedback-design.md §4.2-4.4.
"""

from __future__ import annotations

import io
import math
import random
import struct
import sys
import wave
from pathlib import Path

RATE = 22050
DURATION_MS = 30
SEED = 20260930


def render_click() -> bytes:
    """Render the click sound and return complete WAV file bytes."""
    rng = random.Random(SEED)
    n_samples = int(RATE * DURATION_MS / 1000)
    attack_samples = max(1, int(RATE * 1 / 1000))
    decay_tau_s = 6 / 1000
    amplitude = 0.8 * 32767

    samples = bytearray()
    for i in range(n_samples):
        t_s = i / RATE
        noise = rng.uniform(-1, 1)
        if i < attack_samples:
            envelope = i / attack_samples
        else:
            envelope = math.exp(-t_s / decay_tau_s)
        value = int(noise * envelope * amplitude)
        value = max(-32768, min(32767, value))
        samples += struct.pack("<h", value)

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(bytes(samples))
    return buffer.getvalue()


def main(out_path: Path) -> None:
    """Write render_click()'s bytes to out_path."""
    out_path = Path(out_path)
    data = render_click()
    out_path.write_bytes(data)


if __name__ == "__main__":
    default_out = (
        Path(__file__).resolve().parent.parent
        / "web_interface"
        / "static"
        / "click.wav"
    )
    arg_path = Path(sys.argv[1]) if len(sys.argv) > 1 else default_out
    main(arg_path)
    print(len(arg_path.read_bytes()))
