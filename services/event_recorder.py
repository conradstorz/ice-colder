# services/event_recorder.py
"""
Event recorder — persists machine activity to SQLite for the dashboard.

Records payment, dispense, ice_cycle, error, service_door, temp_exceedance,
and heartbeat events. Provides time-windowed aggregate summaries.

Also durably records sales (``record_sale``) and cash collections
(``record_cash_collection``); see the class docstring below for the
concurrency and durability contract each follows.
"""

import json
import os
import queue
import sqlite3
import threading
import time
from collections import namedtuple
from datetime import datetime
from pathlib import Path
from typing import Optional

from loguru import logger

from services.mqtt_messages import (
    HardwareIO,
    IceMakerEvent,
    PaymentEvent,
    SensorReading,
    SubsystemHeartbeat,
)
from services.paths import DATA_DIR

# Must match simulators/base.py HEARTBEAT_INTERVAL
_HEARTBEAT_INTERVAL = 10.0

# get_historical_average only covers prior *complete* periods, so its result
# changes slowly; cache it briefly to spare the DB from up to 30 SELECTs per
# call on every dashboard poll.
_HISTORICAL_AVERAGE_CACHE_TTL = 60.0

SUMMARY_KEYS = (
    "money_in",
    "products_out",
    "ice_cycles",
    "errors",
    "service_door_opens",
    "temp_exceedances",
    "uptime_pct",
    "vends_failed",
    "refunds",
)

# Failed record_sale() inserts are journaled here (append + fsync) so the
# sale is never silently lost; replay_sales_journal() drains it at startup.
# Module-level (not an instance attribute) so tests can point it at a temp
# directory via monkeypatch.setattr(event_recorder, "JOURNAL_PATH", ...).
JOURNAL_PATH = DATA_DIR / "sales-journal.jsonl"

# Tags a writer-queue item as a cash-collection job rather than the plain
# 4-tuple `record()` puts on the queue for an events row. A namedtuple is
# still a tuple, but isinstance() distinguishes it by its own class, so
# `_writer_loop` can tell the two job shapes apart without record()/flush()
# changing at all.
_CashCollectionJob = namedtuple("_CashCollectionJob", "user_id user_name ts")


def is_cash(method: str) -> bool:
    """Classify a raw payment-method string as cash or not.

    True for ``cash``, ``coin``, ``bill``, and any method whose lowercase
    name starts with ``cash_`` or ``coin_`` (covers the simulator's
    ``cash_coin`` and ``cash_bill``). Everything else (``card``, ``nfc``,
    ``test``, ...) is not cash.

    Defined here rather than in ``services/reports.py`` (which this task
    does not create) because both this module's ``expected_cash``
    computation and the future reports module need the same classification.
    A later task adds ``services/reports.py`` and re-exports ``is_cash``
    from here as the public home for report code; ``reports`` will import
    from ``event_recorder``, never the reverse, so there is no cycle.
    """
    m = method.lower()
    return (
        m in ("cash", "coin", "bill") or m.startswith("cash_") or m.startswith("coin_")
    )


def _append_journal_line(payload: dict) -> None:
    """Append one JSON line to JOURNAL_PATH, durably (flush + fsync)."""
    JOURNAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(JOURNAL_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(payload) + "\n")
        f.flush()
        os.fsync(f.fileno())


class EventRecorder:
    """
    Persists machine events to SQLite and provides time-windowed summaries.

    Usage:
        recorder = EventRecorder(db_path="data/events.db")
        recorder.register_handlers(mqtt_client)
        vmc.set_event_recorder(recorder)  # for FSM error events
        summary = recorder.get_summary(24)
        avg = recorder.get_historical_average(24)

    Two durability paths coexist here:

    - ``record()`` (events) is fire-and-forget: it queues a row and the
      single writer-thread connection inserts it asynchronously, so MQTT
      handlers never block on SD-card writes. ``record_cash_collection()``
      rides the same queue for the same reason.
    - ``record_sale()`` is synchronous and durable: it opens its own
      connection (WAL mode, synchronous=NORMAL) and returns only once the
      row is committed, because the caller (the VMC's dispense path) must
      know the sale is on disk before it resumes. It deliberately does not
      use the writer queue.
    """

    def __init__(
        self,
        db_path: str = "data/events.db",
        temp_min: float = -20.0,
        temp_max: float = 80.0,
        retention_days: int = 90,
    ):
        self._db_path = db_path
        self._temp_min = temp_min
        self._temp_max = temp_max
        self._retention_days = retention_days
        self._last_prune = 0.0
        self._historical_avg_cache: dict[int, tuple[float, dict]] = {}
        # Corrupt-database recovery (§5): not a raise -- a flag + the backup
        # path a later task (main.py) inspects to raise the alert-class
        # fault DATA-102. See _quarantine_corrupt_db.
        self.db_was_corrupt: bool = False
        self.corrupt_backup_path: Optional[str] = None
        try:
            self._init_db()
            self.prune()
        except sqlite3.DatabaseError:
            # Either _init_db (schema creation reads the file header) or
            # prune (a DELETE) can be the first statement to actually touch
            # a corrupt file and raise -- handle both here.
            self._quarantine_corrupt_db()
            self._init_db()
            self.prune()
        # All inserts go through one daemon thread with one connection so MQTT
        # handlers never block the event loop on SD-card writes.
        self._queue: queue.Queue = queue.Queue()
        self._writer = threading.Thread(
            target=self._writer_loop, name="event-recorder", daemon=True
        )
        self._writer.start()

    def _quarantine_corrupt_db(self) -> None:
        """Rename an unreadable db file aside and expose the fact.

        Renames to ``<db_path>.corrupt-<timestamp>`` (preserving whatever is
        recoverable) rather than deleting it, then leaves a fresh, empty
        database to be created at the original path by the caller's retry
        of ``_init_db``/``prune``. Sets ``db_was_corrupt``/
        ``corrupt_backup_path`` instead of raising, per §5: the machine must
        still start and run; a later task raises DATA-102 from these.
        """
        timestamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
        backup_path = f"{self._db_path}.corrupt-{timestamp}"
        try:
            os.replace(self._db_path, backup_path)
        except OSError:
            logger.exception(
                f"EventRecorder: could not rename corrupt database {self._db_path}"
            )
            backup_path = None
        self.db_was_corrupt = True
        self.corrupt_backup_path = backup_path
        logger.error(
            f"EventRecorder: {self._db_path} was unreadable and has been reset "
            f"to a fresh database (previous file preserved at {backup_path})"
        )

    def _init_db(self):
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        # Explicit close (not `with sqlite3.connect(...) as conn:`, which
        # commits/rolls back but never closes) so a corrupt file can be
        # renamed right after this raises, with no lingering open handle --
        # important on Windows, where a rename fails while a handle is open.
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS events (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    timestamp  REAL NOT NULL,
                    value      REAL,
                    metadata   TEXT
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_type_ts ON events (event_type, timestamp)"
            )
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sales (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts         REAL NOT NULL,
                    sku        TEXT NOT NULL,
                    name       TEXT NOT NULL,
                    slot       INTEGER,
                    price      REAL NOT NULL,
                    methods    TEXT NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_sales_ts ON sales (ts)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_sales_sku_ts ON sales (sku, ts)"
            )
            conn.execute("""
                CREATE TABLE IF NOT EXISTS cash_collections (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts            REAL NOT NULL,
                    user_id       TEXT NOT NULL,
                    user_name     TEXT NOT NULL,
                    expected_cash REAL NOT NULL
                )
            """)
            conn.commit()
        finally:
            conn.close()

    def record(
        self, event_type: str, value: float = 1.0, metadata: Optional[dict] = None
    ):
        """Queue one event row; the writer thread inserts it."""
        meta_str = json.dumps(metadata) if metadata else None
        self._queue.put((event_type, time.time(), value, meta_str))
        logger.debug(f"EventRecorder: {event_type} value={value}")

    def record_cash_collection(self, user_id: str, user_name: str) -> None:
        """Queue a cash-collection row.

        Enqueued on the same writer queue as any event -- `expected_cash` is
        computed inside the writer thread at insert time (see
        `_insert_cash_collection`) so it is consistent with every sale
        already durably written, which computing it here or in a route
        cannot guarantee.
        """
        self._queue.put(
            _CashCollectionJob(user_id=user_id, user_name=user_name, ts=time.time())
        )
        logger.debug(f"EventRecorder: cash collection queued for user={user_id}")

    def record_sale(
        self,
        sku: str,
        name: str,
        slot: Optional[int],
        price: float,
        methods: dict[str, float],
        ts: Optional[float] = None,
    ) -> None:
        """Durably record one sale; returns only once the row is committed.

        Opens its own connection per call rather than sharing one across
        calls: the VMC invokes this through ``asyncio.to_thread``, whose
        thread pool can and does run successive calls on different threads,
        and a single long-lived ``sqlite3.Connection`` is not safe to reuse
        across threads without an explicit lock serializing every caller
        behind it -- which would also defeat the purpose of using a thread
        pool. A fresh connection per call needs no lock and each call's WAL
        + synchronous=NORMAL setup is cheap next to a disk fsync.

        ``journal_mode=WAL`` is a persistent property of the database file,
        not of this connection -- once set here it also applies to the
        writer thread's long-lived connection to the same file.

        On failure: append one JSON line to ``JOURNAL_PATH`` (append +
        fsync) with the sale's fields, then re-raise the original
        exception. Re-raising (rather than returning a success flag) is the
        shape that suits the caller described in the spec: the VMC awaits
        this call inside a plain ``try/except`` around the one line that
        calls it, catches the exception, raises the alert-class fault
        DATA-101, and *falls through* to finish the dispense -- the journal
        line written here is what makes "raise DATA-101 and still let the
        sale complete" safe, since the sale is not lost even though the
        insert failed. A boolean return would require every caller to
        remember to check it; an exception cannot be silently ignored.
        """
        ts = time.time() if ts is None else ts
        payload = {
            "ts": ts,
            "sku": sku,
            "name": name,
            "slot": slot,
            "price": price,
            "methods": methods,
        }
        conn = None
        try:
            conn = sqlite3.connect(self._db_path, timeout=5.0)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute(
                "INSERT INTO sales (ts, sku, name, slot, price, methods) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (ts, sku, name, slot, price, json.dumps(methods)),
            )
            conn.commit()
        except Exception:
            logger.exception(
                f"EventRecorder: record_sale failed for sku={sku!r}; journaling instead"
            )
            _append_journal_line(payload)
            raise
        finally:
            if conn is not None:
                conn.close()

    def replay_sales_journal(self) -> int:
        """Insert any journalled sales, truncate the file, return the count.

        No-op (returns 0, file untouched) when the file is absent or empty.
        A well-formed line is inserted even when the file's final line is a
        partial write (no trailing newline, invalid JSON) -- that one line
        is dropped with a warning, but it never costs the earlier good
        lines, and the file is still truncated afterward since a
        partially-written line cannot be completed by any later retry.
        """
        if not JOURNAL_PATH.exists():
            return 0
        content = JOURNAL_PATH.read_text(encoding="utf-8")
        if not content.strip():
            return 0

        rows = []
        for line in content.splitlines():
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                logger.warning(
                    f"EventRecorder: dropping corrupt/partial journal line: {line!r}"
                )

        count = 0
        if rows:
            conn = sqlite3.connect(self._db_path, timeout=5.0)
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                for row in rows:
                    conn.execute(
                        "INSERT INTO sales (ts, sku, name, slot, price, methods) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            row["ts"],
                            row["sku"],
                            row["name"],
                            row.get("slot"),
                            row["price"],
                            json.dumps(row["methods"]),
                        ),
                    )
                    count += 1
                conn.commit()
            finally:
                conn.close()

        JOURNAL_PATH.write_text("", encoding="utf-8")
        return count

    def flush(self, timeout: float = 5.0) -> None:
        """Block until every queued row is written (tests, shutdown, reads)."""
        deadline = time.monotonic() + timeout
        while self._queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.005)
        n = self._queue.unfinished_tasks
        if n:
            logger.warning(f"EventRecorder: flush timed out with {n} rows still queued")

    def _writer_loop(self) -> None:
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        while True:
            row = self._queue.get()
            try:
                if isinstance(row, _CashCollectionJob):
                    self._insert_cash_collection(conn, row)
                else:
                    conn.execute(
                        "INSERT INTO events (event_type, timestamp, value, metadata) VALUES (?, ?, ?, ?)",
                        row,
                    )
                    conn.commit()
                    if time.time() - self._last_prune > 86400:
                        self._prune_with(conn)
            except Exception:
                if isinstance(row, _CashCollectionJob):
                    logger.exception(
                        f"EventRecorder: failed to write cash collection for user={row.user_id}"
                    )
                else:
                    logger.exception(f"EventRecorder: failed to write {row[0]}")
            finally:
                self._queue.task_done()

    def _insert_cash_collection(
        self, conn: sqlite3.Connection, job: "_CashCollectionJob"
    ) -> None:
        """Insert one cash_collections row with expected_cash computed here.

        expected_cash is the sum of the cash-class shares of sales.methods
        with ts strictly greater than the previous collection's ts (all
        time for the first row). Doing this in the writer thread -- the
        same thread that inserts every sale -- is what makes it consistent
        with everything already written, rather than racing a route or the
        caller's own read.
        """
        prev = conn.execute(
            "SELECT ts FROM cash_collections ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        if prev is None:
            cursor = conn.execute("SELECT methods FROM sales")
        else:
            cursor = conn.execute("SELECT methods FROM sales WHERE ts > ?", (prev[0],))

        expected_cash = 0.0
        for (methods_json,) in cursor.fetchall():
            methods = json.loads(methods_json)
            for method, amount in methods.items():
                if is_cash(method):
                    expected_cash += amount

        conn.execute(
            "INSERT INTO cash_collections (ts, user_id, user_name, expected_cash) "
            "VALUES (?, ?, ?, ?)",
            (job.ts, job.user_id, job.user_name, expected_cash),
        )
        conn.commit()

    def prune(self):
        """Delete events older than the retention window (SD-card growth guard)."""
        conn = sqlite3.connect(self._db_path)
        try:
            self._prune_with(conn)
        finally:
            conn.close()

    def _prune_with(self, conn: sqlite3.Connection) -> None:
        cutoff = time.time() - self._retention_days * 86400
        cur = conn.execute("DELETE FROM events WHERE timestamp < ?", (cutoff,))
        conn.commit()
        self._last_prune = time.time()
        if cur.rowcount:
            logger.info(
                f"EventRecorder: pruned {cur.rowcount} events older than "
                f"{self._retention_days} days"
            )

    def _compute_window(self, start_ts: float, end_ts: float) -> dict:
        """Compute aggregates for events in [start_ts, end_ts).

        uptime_pct measures the fraction of _HEARTBEAT_INTERVAL-sized time
        buckets in the window that have at least one heartbeat from ANY
        subsystem — i.e. "was something alive during this interval", not raw
        heartbeat row count. Counting rows overcounts when multiple
        subsystems beat concurrently (three simulators beating every 10s
        would read ~300%, clamped to 100%) while saying nothing about whether
        any *particular* subsystem was up. Counting distinct covered buckets
        is the most this metric can honestly claim; per-subsystem health is a
        separate concern (see health_monitor / subsystem_offline events).
        """
        self.flush()
        with sqlite3.connect(self._db_path) as conn:

            def count(etype):
                return conn.execute(
                    "SELECT COUNT(*) FROM events WHERE event_type=? AND timestamp>=? AND timestamp<?",
                    (etype, start_ts, end_ts),
                ).fetchone()[0]

            def total(etype):
                return conn.execute(
                    "SELECT COALESCE(SUM(value), 0.0) FROM events WHERE event_type=? AND timestamp>=? AND timestamp<?",
                    (etype, start_ts, end_ts),
                ).fetchone()[0]

            covered_buckets = conn.execute(
                "SELECT COUNT(DISTINCT CAST(timestamp / ? AS INTEGER)) FROM events "
                "WHERE event_type='heartbeat' AND timestamp>=? AND timestamp<?",
                (_HEARTBEAT_INTERVAL, start_ts, end_ts),
            ).fetchone()[0]
            period_secs = end_ts - start_ts
            uptime_pct = min(
                100.0,
                covered_buckets * _HEARTBEAT_INTERVAL / period_secs * 100,
            )
            return {
                "money_in": round(total("payment"), 2),
                "products_out": count("dispense"),
                "ice_cycles": count("ice_cycle"),
                "errors": count("error"),
                "service_door_opens": count("service_door"),
                "temp_exceedances": count("temp_exceedance"),
                "uptime_pct": uptime_pct,
                "vends_failed": count("vend_failed"),
                "refunds": round(total("refund"), 2),
            }

    def get_summary(self, period_hours: int) -> dict:
        """Return aggregate metrics for the last period_hours."""
        now = time.time()
        # Add 1 ms so events inserted at exactly `now` are included by the
        # half-open interval [start, end) used in _compute_window.
        return self._compute_window(now - period_hours * 3600, now + 0.001)

    def register_handlers(self, mqtt_client):
        """Register MQTT handlers. Multiple callers can register for the same topic.

        Note: "dispense" events are NOT recorded from a direct
        ``hardware/dispenser`` subscription here — only the VMC (see
        ``controller.vmc.VMC._handle_mqtt_dispenser``) knows whether a
        completion was actually accepted for the active sale (correct slot,
        state == dispensing). Recording directly from the raw MQTT topic would
        overcount products_out on duplicate/late completions the VMC ignores.
        The VMC calls ``record("dispense", ...)`` itself when it accepts one.
        """
        mqtt_client.register("payment/credit", self._on_payment)
        mqtt_client.register("ice_maker/event", self._on_ice_maker_event)
        mqtt_client.register("hardware/io/service_door", self._on_service_door)
        mqtt_client.register("sensors/temp/+", self._on_sensor)
        mqtt_client.register("heartbeat/+", self._on_heartbeat)

    async def _on_payment(self, topic: str, data: dict):
        event = PaymentEvent.model_validate(data)
        self.record("payment", value=event.amount)

    async def _on_ice_maker_event(self, topic: str, data: dict):
        event = IceMakerEvent.model_validate(data)
        if event.event == "ice_dropped":
            self.record("ice_cycle", value=1.0)

    async def _on_service_door(self, topic: str, data: dict):
        hw = HardwareIO.model_validate(data)
        if hw.state:
            self.record("service_door", value=1.0)

    async def _on_sensor(self, topic: str, data: dict):
        reading = SensorReading.model_validate(data)
        if not (self._temp_min <= reading.value <= self._temp_max):
            self.record(
                "temp_exceedance",
                value=reading.value,
                metadata={"location": reading.location},
            )

    async def _on_heartbeat(self, topic: str, data: dict):
        hb = SubsystemHeartbeat.model_validate(data)
        if hb.uptime_seconds < 0:
            # uptime_seconds == -1 is the MQTT Last-Will payload meaning the
            # subsystem went OFFLINE — recording it as a "heartbeat" would
            # make an outage count as uptime in _compute_window.
            self.record("subsystem_offline", metadata={"subsystem": hb.subsystem})
            return
        self.record("heartbeat", value=float(hb.uptime_seconds))

    def get_historical_average(self, period_hours: int) -> dict:
        """
        Return per-period averages over prior complete periods (up to 30).
        Only includes periods where at least one heartbeat was recorded
        (machine was running). Returns all-None if fewer than 2 such periods
        exist. Result is cached briefly per period_hours since it only
        covers prior *complete* periods and doesn't change quickly.
        """
        now = time.time()
        cached = self._historical_avg_cache.get(period_hours)
        if cached is not None:
            cached_at, cached_result = cached
            if now - cached_at < _HISTORICAL_AVERAGE_CACHE_TTL:
                return cached_result

        period_secs = period_hours * 3600
        window_start = now - period_secs

        active_windows = []
        for i in range(1, 31):
            end = window_start - (i - 1) * period_secs
            start = end - period_secs
            w = self._compute_window(start, end)
            if w["uptime_pct"] > 0:
                active_windows.append(w)

        if len(active_windows) < 2:
            result = {k: None for k in SUMMARY_KEYS}
        else:
            avg = {}
            for key in SUMMARY_KEYS:
                values = [w[key] for w in active_windows if w[key] is not None]
                avg[key] = round(sum(values) / len(values), 2) if values else None
            result = avg

        self._historical_avg_cache[period_hours] = (now, result)
        return result
