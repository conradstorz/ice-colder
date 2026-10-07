# scripts/gen_dispensers_example.py
"""Writes `dispensers.example.toml` from
`services.dispensers_doc.render_example()`.

Run this after editing `services/dispenser_schema.py` (a field's
description/unit/range) or `services/dispensers_doc.py`'s sample values, so
the committed example never drifts from the schema it documents:

    uv run python scripts/gen_dispensers_example.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))

from services.dispensers_doc import render_example  # noqa: E402


def main() -> None:
    out_path = _REPO_ROOT / "dispensers.example.toml"
    text = render_example()
    # Binary write with explicit utf-8 bytes: a text-mode write would
    # translate "\n" to the platform line ending (CRLF on Windows), which
    # would make the committed file's bytes depend on the OS that
    # generated it.
    out_path.write_bytes(text.encode("utf-8"))
    print(out_path)


if __name__ == "__main__":
    main()
