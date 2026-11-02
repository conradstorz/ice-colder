"""events.db migration 2 (money in integer cents) and the sales journal's
``"units"`` marker (written, no longer read)."""

import json
import sqlite3
import time

import pytest

from tests.edge.db_helpers import db_rows
from ice_edge.ledger import event_recorder as event_recorder_module
from ice_edge.ledger.event_recorder import MIGRATIONS, EventRecorder


@pytest.fixture
def journal_path(tmp_path, monkeypatch):
    path = tmp_path / "sales-journal.jsonl"
    monkeypatch.setattr(event_recorder_module, "JOURNAL_PATH", path)
    return path


def _v1_database(path) -> None:
    conn = sqlite3.connect(path)
    for statement in MIGRATIONS[0].split(";"):
        if statement.strip():
            conn.execute(statement)
    conn.execute("PRAGMA user_version = 1")
    conn.executemany(
        "INSERT INTO sales (ts, sku, name, slot, price, methods) VALUES (?,?,?,?,?,?)",
        [
            (1.0, "A", "Alpha", 1, 2.5, json.dumps({"cash": 2.5})),
            (
                2.0,
                "B",
                "Beta",
                2,
                0.1 + 0.2,
                json.dumps({"cash": 0.30000000000000004}),
            ),
            (3.0, "C", "Gamma", None, 2.675, json.dumps({"card": 2.675})),
            (4.0, "D", "Delta", 4, 0.0, json.dumps({})),
            (5.0, "E", "Mixed", 5, 2.5, json.dumps({"cash": 0.3, "card": 2.2})),
        ],
    )
    conn.execute(
        "INSERT INTO cash_collections (ts, user_id, user_name, expected_cash) "
        "VALUES (10.0, 'u1', 'Ada', 12.34)"
    )
    now = time.time()  # recent, so the 90-day prune keeps the rows
    conn.executemany(
        "INSERT INTO events (event_type, timestamp, value, metadata) VALUES (?,?,?,?)",
        [
            ("payment", now, 1.25, None),
            ("refund", now, 0.5, None),
            ("heartbeat", now, 60.0, None),
            ("temp_exceedance", now, 41.5, None),
            ("dispense", now, 1.0, None),
        ],
    )
    conn.commit()
    conn.close()


def test_migration_converts_v1_database_to_cents(tmp_path):
    db = str(tmp_path / "events.db")
    _v1_database(db)

    EventRecorder(db_path=db)

    conn = sqlite3.connect(db)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS) == 2

    sales = {
        sku: (price, json.loads(methods), slot, id_)
        for id_, sku, slot, price, methods in conn.execute(
            "SELECT id, sku, slot, price_cents, methods FROM sales"
        )
    }
    assert sales["A"] == (250, {"cash": 250}, 1, 1)
    assert sales["B"] == (30, {"cash": 30}, 2, 2)
    # 2.675 is stored as the float 2.67499999999999982..., but 2.675 * 100 in
    # double arithmetic rounds to exactly 267.5, and SQLite's ROUND rounds half
    # away from zero, so it yields 268. This is acceptable: the code always stored round(x, 2) values, whose
    # float times 100 is within 1e-9 of a whole cent, so a genuine half-cent
    # float such as 2.675 never occurs in real data. Pinned to document it.
    assert sales["C"][0] == 268
    assert sales["C"][1] == {"card": 268}
    assert sales["C"][2] is None
    assert sales["D"] == (0, {}, 4, 4)
    assert sales["E"][:2] == (250, {"cash": 30, "card": 220})
    raw_empty = conn.execute("SELECT methods FROM sales WHERE sku='D'").fetchone()[0]
    assert raw_empty == "{}"
    assert all(isinstance(p, int) for p, *_ in sales.values())

    cols = [r[1] for r in conn.execute("PRAGMA table_info(sales)")]
    assert "price" not in cols and "price_cents" in cols
    cc_cols = [r[1] for r in conn.execute("PRAGMA table_info(cash_collections)")]
    assert "expected_cash" not in cc_cols and "expected_cash_cents" in cc_cols
    row = conn.execute("SELECT expected_cash_cents FROM cash_collections").fetchone()
    assert row == (1234,) and isinstance(row[0], int)

    indexes = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }
    assert {"idx_sales_ts", "idx_sales_sku_ts", "idx_type_ts"} <= indexes

    events = dict(conn.execute("SELECT event_type, value FROM events"))
    assert events["payment"] == 125
    assert events["refund"] == 50
    assert events["heartbeat"] == 60.0
    assert events["temp_exceedance"] == 41.5
    assert events["dispense"] == 1.0
    conn.close()


def test_migration_is_not_reapplied(tmp_path):
    db = str(tmp_path / "events.db")
    _v1_database(db)
    EventRecorder(db_path=db)
    EventRecorder(db_path=db)
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT price_cents FROM sales WHERE sku='A'").fetchone() == (
        250,
    )
    conn.close()


def test_fresh_database_has_cents_schema(tmp_path):
    db = str(tmp_path / "events.db")
    rec = EventRecorder(db_path=db)
    rec.record_sale("A", "Alpha", 1, 250, {"cash": 250}, ts=1.0)
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT price_cents FROM sales").fetchone() == (250,)
    conn.close()


# --- journal "units" marker -------------------------------------------------


def _sales(db):
    return {
        sku: (price, json.loads(methods))
        for sku, price, methods in db_rows(
            db, "SELECT sku, price_cents, methods FROM sales"
        )
    }


def test_cents_journal_row_replays_unchanged(tmp_path, journal_path):
    rec = EventRecorder(db_path=str(tmp_path / "events.db"))
    journal_path.write_text(
        json.dumps(
            {
                "ts": 1.0,
                "sku": "A",
                "name": "Alpha",
                "slot": 1,
                "units": "cents",
                "price_cents": 250,
                "methods": {"cash": 250},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    assert rec.replay_sales_journal() == 1
    assert _sales(rec.db_path) == {"A": (250, {"cash": 250})}


def test_failed_insert_journals_a_cents_row(tmp_path, journal_path, monkeypatch):
    rec = EventRecorder(db_path=str(tmp_path / "events.db"))

    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(EventRecorder, "_insert_sale_row", staticmethod(boom))
    with pytest.raises(sqlite3.OperationalError):
        rec.record_sale("A", "Alpha", 1, 250, {"cash": 250}, ts=1.0)
    row = json.loads(journal_path.read_text(encoding="utf-8").splitlines()[0])
    assert row["units"] == "cents"
    assert row["price_cents"] == 250
    assert row["methods"] == {"cash": 250}
    assert "price" not in row
