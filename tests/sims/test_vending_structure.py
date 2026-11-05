"""Import direction inside `ice_sims.vending`: simulator -> everything else,
never the reverse (same ast approach as tests/meta/test_routes_shape.py)."""

from pathlib import Path

import ice_sims.vending as vending_pkg
from tests.sims.structure_helpers import imported_modules

PACKAGE_DIR = Path(vending_pkg.__file__).parent
LEAVES = ["board", "channels", "dispense", "customer", "faults", "ha"]


def test_leaf_modules_never_import_the_simulator_or_base():
    for name in LEAVES:
        imports = imported_modules(PACKAGE_DIR / f"{name}.py")
        assert "ice_sims.vending.simulator" not in imports, name
        assert "ice_sims.base" not in imports, name
        assert "ice_sims.vending_machine" not in imports, name
        assert "ice_sims.vending" not in imports, name


def test_board_imports_nothing_from_the_package():
    imports = imported_modules(PACKAGE_DIR / "board.py")
    # Only the two pure models its `ro_tank`/`climate` handles are typed with.
    allowed = {"ice_sims.vending.ro_tank", "ice_sims.vending.climate"}
    ours = {m for m in imports if m.startswith("ice_sims")}
    assert all(any(m == a or m.startswith(a + ".") for a in allowed) for m in ours), (
        ours
    )


def test_faults_import_only_board_and_contracts():
    imports = imported_modules(PACKAGE_DIR / "faults.py")
    ours = {m for m in imports if m.startswith("ice_sims")}
    assert all(
        m == "ice_sims.vending.board" or m.startswith("ice_sims.vending.board.")
        for m in ours
    ), ours
    assert not {m for m in imports if m.startswith(("ice_edge", "ice_ui"))}


def test_no_vending_module_imports_edge_or_ui():
    for path in PACKAGE_DIR.glob("*.py"):
        imports = imported_modules(path)
        assert not {m for m in imports if m.startswith(("ice_edge", "ice_ui"))}, path
