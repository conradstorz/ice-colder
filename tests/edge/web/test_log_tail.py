"""``context.tail`` reads fixed blocks backwards and keeps the old semantics."""

from pathlib import Path

from ice_edge.web.context import tail


def _old_tail(file_path: Path, lines: int = 50) -> list[str]:
    """The byte-at-a-time implementation tail() replaced, kept as the oracle."""
    with file_path.open("rb") as f:
        f.seek(0, 2)
        end = f.tell()
        buffer = bytearray()
        count = 0
        for pos in range(end - 1, -1, -1):
            f.seek(pos)
            char = f.read(1)
            if char == b"\n":
                count += 1
                if count >= lines:
                    break
            buffer.extend(char)
        return buffer[::-1].decode("utf-8", errors="replace").strip().splitlines()


def test_missing_file(tmp_path):
    assert tail(tmp_path / "nope.log") == ["[Log file not found]"]


def test_long_lines_return_last_fifty_in_order(tmp_path):
    path = tmp_path / "big.log"
    with path.open("wb") as f:
        for i in range(1000):
            f.write(f"{i:04d} ".encode() + b"x" * (10 * 1024) + b"\n")
    result = tail(path, 50)
    assert len(result) == 50
    assert [line[:4] for line in result] == [f"{i:04d}" for i in range(950, 1000)]
    assert all(len(line) == 10 * 1024 + 5 for line in result)


def test_matches_old_output_on_mixed_small_file(tmp_path):
    path = tmp_path / "mixed.log"
    path.write_bytes(b"one\r\ntwo\r\nbad \xff byte\nfour\nfive")
    # Old implementation counted a trailing newline as a line; with no trailing
    # newline here the two agree when asked for more lines than are present.
    assert tail(path, 50) == _old_tail(path, 50)
    assert tail(path, 50)[2] == "bad � byte"


def test_fewer_lines_than_requested_and_no_newline(tmp_path):
    path = tmp_path / "short.log"
    path.write_bytes(b"only line")
    assert tail(path, 50) == ["only line"]


def test_trailing_newline_is_not_an_extra_line(tmp_path):
    path = tmp_path / "t.log"
    path.write_bytes(b"a\nb\nc\n")
    assert tail(path, 2) == ["b", "c"]
    assert tail(path, 3) == ["a", "b", "c"]


def test_multibyte_char_straddling_block_boundary(tmp_path):
    path = tmp_path / "u.log"
    path.write_text("first\n" + "é" * 9000 + "\nlast\n", encoding="utf-8")
    assert tail(path, 2) == ["é" * 9000, "last"]
