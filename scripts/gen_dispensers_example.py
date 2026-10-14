# scripts/gen_dispensers_example.py
"""Writes `dispensers.example.toml` from
`ice_edge.vend.dispensers_doc.render_example()`.

Run this after editing `ice_contracts/board/dispenser_schema.py` (a field's
description/unit/range) or `ice_edge/vend/dispensers_doc.py`'s sample values, so
the committed example never drifts from the schema it documents:

    uv run python scripts/gen_dispensers_example.py
"""

from __future__ import annotations

import sys

from _repo import repo_root

_REPO_ROOT = repo_root()
sys.path.insert(0, str(_REPO_ROOT))

from ice_edge.vend.dispensers_doc import render_example  # noqa: E402


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
