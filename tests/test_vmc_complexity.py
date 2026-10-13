"""Size ratchet for controller/vmc.py (vmc-diet design §3.6).

Counts, with ast + tokenize, the VMC class's code lines (non-blank,
non-comment, non-docstring), the file's total lines, and the largest
method's code lines. Over a budget fails; more than 10% under a budget
fails too, naming the number to lower it to -- budgets only move down.
"""

import ast
from pathlib import Path

VMC_PATH = Path(__file__).resolve().parent.parent / "controller" / "vmc.py"

# Budgets. Lower them when a stage lands; never raise them.
CLASS_CODE_LINES = 683  # set to the measured value in Step 3
FILE_LINES = 1501
LARGEST_METHOD_CODE_LINES = 64


def measure(path: Path = VMC_PATH) -> dict:
    src = path.read_text(encoding="utf-8")
    lines = src.splitlines()
    tree = ast.parse(src)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "VMC")
    doc: set[int] = set()
    for n in ast.walk(cls):
        if (
            isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and n.body
        ):
            b = n.body[0]
            if (
                isinstance(b, ast.Expr)
                and isinstance(b.value, ast.Constant)
                and isinstance(b.value.value, str)
            ):
                doc.update(range(b.lineno, b.end_lineno + 1))

    def is_code(i: int) -> bool:
        s = lines[i - 1].strip()
        return bool(s) and not s.startswith("#") and i not in doc

    methods = {
        n.name: sum(1 for i in range(n.lineno, n.end_lineno + 1) if is_code(i))
        for n in cls.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    largest = max(methods, key=methods.get)
    return {
        "class_code_lines": sum(
            1 for i in range(cls.lineno, cls.end_lineno + 1) if is_code(i)
        ),
        "file_lines": len(lines),
        "largest_method": largest,
        "largest_method_code_lines": methods[largest],
    }


def _check(name: str, actual: int, budget: int) -> None:
    assert actual <= budget, f"{name}: {actual} over budget {budget}"
    assert actual >= budget * 0.9, (
        f"{name}: {actual} is well under budget; lower {name} to {actual}"
    )


def test_vmc_size_within_budget():
    m = measure()
    _check("CLASS_CODE_LINES", m["class_code_lines"], CLASS_CODE_LINES)
    _check("FILE_LINES", m["file_lines"], FILE_LINES)
    _check(
        f"LARGEST_METHOD_CODE_LINES ({m['largest_method']})",
        m["largest_method_code_lines"],
        LARGEST_METHOD_CODE_LINES,
    )


if __name__ == "__main__":
    print(measure())
