"""Import direction inside `ice_sims.mdb`: simulator -> everything else,
never the reverse (same ast approach as tests/sims/test_vending_structure.py)."""

from pathlib import Path

import ice_sims.mdb as mdb_pkg
from tests.sims.structure_helpers import imported_modules

PACKAGE_DIR = Path(mdb_pkg.__file__).parent
LEAVES = ["cash", "policy", "event_log", "customer", "faults"]


def test_nothing_imports_the_simulator_except_the_entry_module():
    for name in LEAVES:
        imports = imported_modules(PACKAGE_DIR / f"{name}.py")
        assert "ice_sims.mdb.simulator" not in imports, name
        assert "ice_sims.base" not in imports, name
        assert "ice_sims.mdb_gateway" not in imports, name
    entry = imported_modules(PACKAGE_DIR.parent / "mdb_gateway.py")
    assert "ice_sims.mdb.simulator" in entry


def test_customer_and_faults_do_not_import_the_models_or_each_other():
    for name in ("customer", "faults"):
        ours = {
            m
            for m in imported_modules(PACKAGE_DIR / f"{name}.py")
            if m.startswith("ice_sims")
        }
        assert ours == set(), (name, ours)


def test_no_mdb_module_imports_edge_or_ui():
    for path in [*PACKAGE_DIR.glob("*.py"), PACKAGE_DIR.parent / "mdb_gateway.py"]:
        imports = imported_modules(path)
        assert not {m for m in imports if m.startswith(("ice_edge", "ice_ui"))}, path
