"""Unit tests for the reorg codemod. Pure: no git, no filesystem writes."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

from scripts.refactor import codemod, move_map
from scripts.refactor.move_map import (
    FILE_MOVES,
    MODULE_MOVES,
    PACKAGE_ROOTS,
    TEST_MOVES,
)

REPO = Path(__file__).resolve().parents[1]


def rw(src: str) -> tuple[str, list[str]]:
    return codemod.rewrite_source(src, MODULE_MOVES)


# ---------------------------------------------------------------- imports


def test_parenthesised_from_import_keeps_formatting() -> None:
    src = "from services.mqtt_messages import (A,\n    B)\n"
    out, review = rw(src)
    assert out == "from ice_contracts.board.mqtt_messages import (A,\n    B)\n"
    assert review == []


def test_from_package_import_moved_module() -> None:
    out, _ = rw("from services import report_scheduler\n")
    assert out == "from ice_edge.reporting import report_scheduler\n"


def test_from_package_import_keeps_alias() -> None:
    out, _ = rw("from services import event_recorder as event_recorder_module\n")
    assert (
        out == "from ice_edge.ledger import event_recorder as event_recorder_module\n"
    )


def test_import_as() -> None:
    out, review = rw("import services.config_store as cs\n")
    assert out == "import ice_edge.config.config_store as cs\n"
    assert review == []


def test_from_controller_import() -> None:
    out, _ = rw("from controller import mqtt_inbound\n")
    assert out == "from ice_edge.hal import mqtt_inbound\n"


def test_unaliased_dotted_import_rewritten_and_reviewed() -> None:
    out, review = rw("import services.paths\n")
    assert out == "import ice_common.paths\n"
    assert len(review) == 1


def test_unaliased_single_name_import_keeps_bound_name() -> None:
    out, review = rw("import contracts\n")
    assert out == "import ice_contracts as contracts\n"
    assert review == []


def test_import_main_keeps_bound_name() -> None:
    out, _ = rw("import main\nfrom main import x\n")
    assert out == "import ice_edge.__main__ as main\nfrom ice_edge.__main__ import x\n"


def test_package_row_prefix_moves_submodules() -> None:
    out, _ = rw("from web_interface.routes.home import router\n")
    assert out == "from ice_edge.web.routes.home import router\n"


def test_more_specific_row_beats_package_row() -> None:
    out, _ = rw("from contracts import common, vending_machine as vm\n")
    assert out == "from ice_contracts.board import common, vending_machine as vm\n"


def test_attribute_of_package_follows_package() -> None:
    out, _ = rw("from contracts import CONTRACT_VERSION\n")
    assert out == "from ice_contracts import CONTRACT_VERSION\n"


def test_mixed_names_split_into_statements() -> None:
    out, _ = rw("from services import paths, mailer, not_moved\n")
    lines = [
        part.strip() for part in out.replace(";", "\n").splitlines() if part.strip()
    ]
    assert "from services import not_moved" in lines
    assert "from ice_common import paths" in lines
    assert "from ice_edge.notify import mailer" in lines
    compile(out, "<out>", "exec")


def test_relative_import_is_review_item_and_untouched() -> None:
    src = "from . import thing\nfrom .sibling import x\n"
    out, review = rw(src)
    assert out == src
    assert len(review) == 2


def test_lookalikes_unchanged() -> None:
    src = "from servicesx import y\nimport configparser\nimport config\n"
    out, review = rw(src)
    assert out == src
    assert review == []


def test_idempotent() -> None:
    once, _ = rw("from services import report_scheduler\nimport services.paths\n")
    twice, _ = rw(once)
    assert twice == once


# ---------------------------------------------------------------- strings


def test_patch_target_string_rewritten() -> None:
    out, _ = rw('mock.patch("services.mailer.smtp_send")\n')
    assert out == 'mock.patch("ice_edge.notify.mailer.smtp_send")\n'


def test_whole_string_equal_to_module_rewritten() -> None:
    out, _ = rw('name = "services.mailer"\n')
    assert out == 'name = "ice_edge.notify.mailer"\n'


@pytest.mark.parametrize(
    "literal",
    [
        "hardware/dispenser",
        "hardware/io/+",
        "config.json",
        "config.example.json",
        "config.json.bak",
        "services/mailer.py",
        "contracts",
        "simulators",
        "services.mailerx.thing",
    ],
)
def test_non_module_strings_unchanged(literal: str) -> None:
    src = f'x = "{literal}"\n'
    out, review = rw(src)
    assert out == src
    assert review == []


def test_fstring_never_rewritten_but_reported() -> None:
    src = 'x = f"web_interface.routes.{name}"\n'
    out, review = rw(src)
    assert out == src
    assert len(review) == 1


def test_fstring_without_moved_prefix_not_reported() -> None:
    src = 'x = f"hardware/{name}"\ny = f"hello {name}"\n'
    out, review = rw(src)
    assert out == src
    assert review == []


def test_main_row_exempt_from_string_rewriting() -> None:
    src = 'a = "main"\nb = \'hx-target="main"\'\nc = "main.thing"\n'
    out, review = rw(src)
    assert out == src
    assert review == []


def test_longest_prefix_wins_in_strings() -> None:
    out, _ = rw('x = "contracts.common.Thing"\ny = "contracts.other.Thing"\n')
    assert (
        out
        == 'x = "ice_contracts.board.common.Thing"\ny = "ice_contracts.other.Thing"\n'
    )


# ---------------------------------------------------------------- text mode


def txt(text: str) -> tuple[str, list[str]]:
    return codemod.rewrite_text(text, MODULE_MOVES, FILE_MOVES, TEST_MOVES)


def test_text_slash_path() -> None:
    out, _ = txt("see controller/vmc.py for details")
    assert out == "see packages/edge/src/ice_edge/sale/vmc.py for details"


def test_text_dotted_reference() -> None:
    out, _ = txt("`services.startup_dispensers.load_dispenser_profiles`")
    assert out == "`ice_edge.app.startup_dispensers.load_dispenser_profiles`"


def test_text_test_path() -> None:
    out, _ = txt('"tests/test_vmc_flows.py": 2,')
    assert out == '"tests/edge/sale/test_vmc_flows.py": 2,'


def test_text_file_move_tree_and_file() -> None:
    out, _ = txt("COPY web_interface/templates/ x\nweb_interface/tailwind.config.js")
    assert "packages/ui/src/ice_ui/templates/" in out
    assert "packages/ui/tailwind.config.js" in out


def test_text_package_directory() -> None:
    out, _ = txt("simulators/base.py and web_interface/routes/home.py")
    assert out == (
        "packages/sims/src/ice_sims/base.py and "
        "packages/edge/src/ice_edge/web/routes/home.py"
    )


def test_text_leaves_mqtt_topics_and_reports_them() -> None:
    src = "topic hardware/io/+ and config.json\n"
    out, review = txt(src)
    assert out == src
    assert len(review) == 1
    assert "hardware/io" in review[0]


def test_text_does_not_touch_longer_filenames() -> None:
    src = "controller/vmc.pyc services.mailerx"
    out, _ = txt(src)
    assert "vmc.pyc" in out
    assert out.endswith("services.mailerx")


def test_text_idempotent() -> None:
    once, _ = txt("controller/vmc.py services.mailer tests/test_access.py")
    twice, _ = txt(once)
    assert twice == once


# ---------------------------------------------------------------- self-exclusion


def test_codemod_never_rewrites_its_own_map() -> None:
    text = (REPO / "scripts" / "refactor" / "move_map.py").read_text(encoding="utf-8")
    # Sanity: the raw rewrite WOULD change the map (its strings are the map).
    changed, _ = codemod.rewrite_source('x = "services.mailer.y"\n', MODULE_MOVES)
    assert changed != 'x = "services.mailer.y"\n'
    out, review = codemod.rewrite_python_file(
        "scripts/refactor/move_map.py", text, MODULE_MOVES
    )
    assert out == text
    assert review == []


def test_file_selection_excludes() -> None:
    tracked = [
        "main.py",
        "services/mailer.py",
        "scripts/refactor/move_map.py",
        "scripts/refactor/codemod.py",
        "scripts/_repo.py",
        "tests/test_codemod.py",
        "tests/meta/test_codemod.py",
        "tests/test_access.py",
        "docs/conf.py",
        ".venv/lib/x.py",
        "colder-docker/app.py",
        "tmp/scratch.py",
        "web_interface/templates/x.txt",
    ]
    assert codemod.select_python_files(tracked) == [
        "main.py",
        "services/mailer.py",
        "scripts/_repo.py",
        "tests/test_access.py",
    ]


# ---------------------------------------------------------------- table invariants


def test_no_duplicate_old_within_phase_and_no_duplicate_new() -> None:
    olds = Counter((m.phase, m.old) for m in MODULE_MOVES)
    assert [k for k, n in olds.items() if n > 1] == []
    news = Counter(m.new for m in MODULE_MOVES)
    assert [k for k, n in news.items() if n > 1] == []


def test_every_new_starts_with_a_package_root() -> None:
    for m in MODULE_MOVES:
        assert m.new.split(".")[0] in PACKAGE_ROOTS, m
    for f in FILE_MOVES:
        assert any(
            f.new.startswith(root + "/") for root in PACKAGE_ROOTS.values()
        ) or any(
            f.new.startswith(root.rsplit("/src/", 1)[0] + "/")
            for root in PACKAGE_ROOTS.values()
        ), f


def test_every_test_dest_under_tests() -> None:
    for t in TEST_MOVES:
        assert t.dest_dir.startswith("tests/"), t
        assert t.file.startswith("test_") and t.file.endswith(".py"), t


def test_test_moves_cover_each_file_once() -> None:
    names = Counter(t.file for t in TEST_MOVES)
    assert [k for k, n in names.items() if n > 1] == []


def test_basenames_preserved_except_main_and_root_packages() -> None:
    for m in MODULE_MOVES:
        if m.old == "main":
            assert m.new == "ice_edge.__main__"
            continue
        if m.package and "." not in m.old:
            continue  # the three root-package renames
        assert m.old.rsplit(".", 1)[-1] == m.new.rsplit(".", 1)[-1], m


def test_module_file_helpers() -> None:
    assert move_map.module_file("ice_edge.sale.vmc", package=False) == Path(
        "packages/edge/src/ice_edge/sale/vmc.py"
    )
    assert move_map.module_file("ice_contracts", package=True) == Path(
        "packages/contracts/src/ice_contracts/__init__.py"
    )
    assert move_map.module_file("ice_edge.__main__", package=False) == Path(
        "packages/edge/src/ice_edge/__main__.py"
    )
    assert move_map.old_module_file("services.paths", package=False) == Path(
        "services/paths.py"
    )
    assert move_map.old_module_file("web_interface.routes", package=True) == Path(
        "web_interface/routes/__init__.py"
    )


# ---------------------------------------------------------------- planning


def test_plan_package_row_skips_files_claimed_by_other_rows() -> None:
    tracked = [
        "contracts/__init__.py",
        "contracts/common.py",
        "contracts/extra/README.md",
        "contracts/board_notes.txt",
    ]
    plan = codemod.plan_moves(2, MODULE_MOVES, FILE_MOVES, TEST_MOVES, tracked)
    pairs = dict(plan)
    root = "packages/contracts/src/ice_contracts"
    assert pairs["contracts/__init__.py"] == f"{root}/__init__.py"
    assert pairs["contracts/common.py"] == f"{root}/board/common.py"
    assert pairs["contracts/board_notes.txt"] == f"{root}/board_notes.txt"
    assert pairs["contracts/extra/README.md"] == f"{root}/extra/README.md"


def test_plan_only_prefix_filters_rows() -> None:
    tracked = ["services/mailer.py", "services/paths.py", "tests/test_mailer.py"]
    plan = codemod.plan_moves(
        4, MODULE_MOVES, FILE_MOVES, TEST_MOVES, tracked, only_prefix="ice_edge.notify"
    )
    assert sorted(plan) == [
        ("services/mailer.py", "packages/edge/src/ice_edge/notify/mailer.py"),
        ("services/notifier.py", "packages/edge/src/ice_edge/notify/notifier.py"),
    ]


def test_plan_test_moves() -> None:
    plan = codemod.plan_moves(
        4,
        MODULE_MOVES,
        FILE_MOVES,
        TEST_MOVES,
        ["tests/test_vmc_flows.py"],
        only_prefix="tests/edge/",
    )
    assert ("tests/test_vmc_flows.py", "tests/edge/sale/test_vmc_flows.py") in plan
    assert all(dst.startswith("tests/edge/") for _, dst in plan)


def test_plan_file_move_tree() -> None:
    tracked = ["web_interface/templates/a.html", "web_interface/templates/p/b.html"]
    plan = codemod.plan_moves(3, MODULE_MOVES, FILE_MOVES, TEST_MOVES, tracked)
    pairs = dict(plan)
    assert (
        pairs["web_interface/templates/a.html"]
        == "packages/ui/src/ice_ui/templates/a.html"
    )
    assert (
        pairs["web_interface/templates/p/b.html"]
        == "packages/ui/src/ice_ui/templates/p/b.html"
    )


def test_validate_plan_idempotent_and_conflicts() -> None:
    exists = {"a.py", "z/b.py", "both_src.py", "both_dst.py"}
    plan = [
        ("a.py", "new/a.py"),  # ready
        ("gone.py", "z/b.py"),  # already applied
        ("missing.py", "nowhere.py"),  # source missing, dest missing
        ("both_src.py", "both_dst.py"),  # conflict
    ]
    todo, errors = codemod.validate_plan(plan, lambda p: p in exists)
    assert todo == [("a.py", "new/a.py")]
    assert len(errors) == 2


def test_check_package_roots_reports_missing_root() -> None:
    errors = codemod.check_package_roots(
        [m for m in MODULE_MOVES if m.phase == 2], lambda p: False
    )
    assert "package root packages/contracts/src/ice_contracts missing" in errors
    assert codemod.check_package_roots(MODULE_MOVES, lambda p: True) == []


def test_uncovered_tests_detects_new_test_files() -> None:
    existing = [
        "tests/test_access.py",
        "tests/test_brand_new.py",
        "tests/conftest.py",
        "controller/test_sale.py",
    ]
    assert codemod.uncovered_tests(existing, TEST_MOVES) == ["tests/test_brand_new.py"]


def test_deep_module_named_like_a_suffix_is_rewritten() -> None:
    phase4 = [m for m in MODULE_MOVES if m.phase == 4]
    out, review = codemod.rewrite_source(
        'x = "web_interface.routes.tests_level.log"\n', phase4
    )
    assert out == 'x = "ice_edge.web.routes.tests_level.log"\n'
    assert review == []


def test_suffix_guard_blocks_but_reports() -> None:
    src = 'x = "contracts.json"\n'
    out, review = rw(src)
    assert out == src
    assert len(review) == 1


def test_text_suffix_guard_reports_and_deep_module_rewritten() -> None:
    text, review = txt("see contracts.json here")
    assert text == "see contracts.json here"
    assert len(review) == 1
    phase4 = [m for m in MODULE_MOVES if m.phase == 4]
    text, _ = codemod.rewrite_text(
        "web_interface.routes.tests_level.log", phase4, FILE_MOVES, TEST_MOVES
    )
    assert text == "ice_edge.web.routes.tests_level.log"
