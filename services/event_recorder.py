# services/event_recorder.py
"""
Event recorder — persists machine activity to SQLite for the dashboard.

Records payment, dispense, ice_cycle, error, service_door, temp_exceedance,
and heartbeat events. Provides time-windowed aggregate summaries.

Also durably records sales (``record_sale``) and cash collections
(``record_cash_collection``); see the class docstring below for the
concurrency and durability contract each follows.

Known limitation: ``sales-journal.rejected.jsonl`` (rows ``record_sale``
journalled that ``replay_sales_journal`` could not insert) only ever
grows -- nothing here rotates it, reads it back, or raises a fault from
its existence. See ``_append_rejected_sale_line``'s docstring.
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


class SaleRecordingFailed(Exception):
    """``record_sale`` could not write the sale anywhere durable: the
    ``sales`` insert failed *and* the journal fallback write also failed
    (e.g. a full or read-only data volume) -- unlike the ordinary "insert
    failed, journal caught it" case, which re-raises the original insert
    exception unchanged. Distinguished by type so ``VMC._record_sale`` can
    tell "the row is safe in the journal, an ordinary DATA-101 alert is
    enough" apart from "the row is nowhere; this must not be silently
    treated the same way," and instead preserve it via the PAY-104
    recovery path (see that method's docstring).
    """


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


def _append_line(path: Path, payload: dict) -> None:
    """Append one JSON line to `path`, durably (flush + fsync)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(payload) + "\n")
        f.flush()
        os.fsync(f.fileno())


def _atomic_write_text(path: Path, content: str) -> None:
    """Replace `path`'s entire content atomically and durably.

    Writes to a sibling temp file, flushes + fsyncs it, then ``os.replace``s
    it over `path`. Unlike ``Path.write_text`` (which truncates the target
    then writes into it, with no fsync), this can never leave `path` missing
    or half-written: until the ``os.replace`` call, the original content at
    `path` is untouched, and ``os.replace`` itself is atomic at the
    filesystem level -- a crash before it leaves the old content intact, a
    crash after it leaves the new content intact, and there is no instant at
    which neither exists. Used by ``replay_sales_journal`` to rewrite
    JOURNAL_PATH, since the lines it rewrites there can be the only
    remaining copy of a sale (see that method's docstring) -- matches the
    shape of ``_append_line`` above (flush + fsync) and
    ``services/config_store.py``'s ``save_config`` (temp file + os.replace).
    """
    tmp_path = path.with_name(f"{path.name}.tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)


def _append_journal_line(payload: dict) -> None:
    """Append one JSON line to JOURNAL_PATH, durably (flush + fsync)."""
    _append_line(JOURNAL_PATH, payload)


def _rejected_sales_path() -> Path:
    """Path for journalled sale rows that failed to insert during replay.

    Computed from the current JOURNAL_PATH at call time (not cached at
    import time) so tests that monkeypatch JOURNAL_PATH to a temp file get
    a matching temp rejected-file path alongside it.
    """
    return JOURNAL_PATH.with_name(f"{JOURNAL_PATH.stem}.rejected{JOURNAL_PATH.suffix}")


def _append_rejected_sale_line(payload: dict) -> None:
    """Set aside one journalled sale row that could not be inserted.

    A row that violates the schema (e.g. a null sku) would otherwise be
    retried -- and fail -- on every future replay, forever. Moving it here
    instead lets replay drain the rest of the journal and clear DATA-101,
    while the operator still has the row as evidence (durably: flush +
    fsync, same as the main journal).

    Known limitation (deferred, accepted scope decision): this file only
    ever grows. Nothing here rotates it, reads it back, or raises a fault
    from its existence -- an operator can only discover it via a
    filesystem check or a log grep (see replay_sales_journal's
    logger.exception call for the matching log line). A rotation policy
    or a dedicated alert is out of scope for this module and left to a
    future task.
    """
    _append_line(_rejected_sales_path(), payload)


_CORRUPTION_MESSAGES = ("file is not a database", "database disk image is malformed")


def _is_corruption_error(exc: sqlite3.DatabaseError) -> bool:
    """True only for a genuine "the file is corrupt" signal from sqlite3.

    ``sqlite3.OperationalError`` is a subclass of ``DatabaseError`` and also
    covers "database is locked", "disk I/O error", and "unable to open
    database file" (e.g. a permission problem) -- none of which mean the
    file is corrupt. Quarantining on one of those would rename away a
    perfectly healthy database and permanently lose its history.

    Verified empirically against this project's Python/sqlite3 build:
    genuine corruption (a garbage header, or a malformed page) raises a
    plain ``sqlite3.DatabaseError`` -- NOT an ``OperationalError`` -- with
    one of the two messages below. So: match those messages first (belt and
    suspenders against a future sqlite3 that reclassifies them), and
    otherwise treat any ``OperationalError`` as NOT corruption.
    """
    message = str(exc).lower()
    if any(sig in message for sig in _CORRUPTION_MESSAGES):
        return True
    return not isinstance(exc, sqlite3.OperationalError)


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
        # Set only on the guarded retry below failing (e.g. a disk-full or
        # permission fault at the exact moment recovery is attempted --
        # a plausible cause of the original corruption too). A later task
        # (main.py) can inspect this the same way it inspects db_was_corrupt.
        self.db_unavailable: bool = False
        try:
            self._init_db()
            self.prune()
        except sqlite3.DatabaseError as exc:
            # Either _init_db (schema creation reads the file header) or
            # prune (a DELETE) can be the first statement to actually touch
            # a corrupt file and raise -- handle both here. But only genuine
            # corruption is handled this way: a locked database or a
            # transient I/O/permission error (both raise OperationalError,
            # a DatabaseError subclass) must propagate unchanged rather than
            # be treated as corruption -- see _is_corruption_error. Renaming
            # a merely-locked, perfectly healthy database aside would
            # permanently lose its history for no reason.
            if not _is_corruption_error(exc):
                raise
            self._quarantine_corrupt_db()
            # This retry must not be allowed to raise out of the
            # constructor: _quarantine_corrupt_db only clears the way for a
            # fresh database, it does not guarantee one can actually be
            # created. A full disk, a read-only directory, or a permission
            # fault at this exact moment would otherwise crash startup --
            # and a full disk is a realistic cause of the *original*
            # corruption too, making this sequence a natural one rather
            # than a hypothetical. If it happens, the machine must still
            # start (§5): log it, leave db_was_corrupt/db_unavailable set
            # for main.py to alert on, and let the constructor complete
            # rather than raise.
            #
            # This is safe to leave broken rather than retried further,
            # because every downstream user of self._db_path already
            # tolerates a database that cannot be written:
            # - _writer_loop's per-item try/except/finally: task_done()
            #   swallows an insert failure (e.g. "no such table: events",
            #   since _init_db never got to create one) without dying or
            #   leaving the queue stuck, so flush() still returns promptly.
            # - record_sale opens its own connection per call and already
            #   journals-then-re-raises on any failure, so a sale is never
            #   lost even though the DB insert failed -- the caller's own
            #   try/except raises the alert-class fault DATA-101 and still
            #   finishes the dispense, exactly per §5 and "the dashboard is
            #   the last thing to go down".
            try:
                self._init_db()
                self.prune()
            except Exception:
                logger.exception(
                    f"EventRecorder: could not initialize a fresh database "
                    f"at {self._db_path} after quarantining the corrupt "
                    "one; the database is unavailable for this process. "
                    "record_sale will journal every sale it is asked to "
                    "record (and re-raise, so the caller can alert "
                    "DATA-101); DATA-102 stays set from the quarantine "
                    "above."
                )
                self.db_unavailable = True
        # All inserts go through one daemon thread with one connection so MQTT
        # handlers never block the event loop on SD-card writes.
        self._queue: queue.Queue = queue.Queue()
        self._writer = threading.Thread(
            target=self._writer_loop, name="event-recorder", daemon=True
        )
        self._writer.start()

    @staticmethod
    def _try_rename_aside(source: str, dest: str) -> Optional[bool]:
        """Best-effort rename of one file. Returns True if renamed, False if
        the rename was attempted and failed, or None if there was nothing at
        `source` to rename (the common case for -wal/-shm sidecars)."""
        if not os.path.exists(source):
            return None
        try:
            os.replace(source, dest)
            return True
        except OSError:
            logger.exception(f"EventRecorder: could not rename {source} aside")
            return False

    def _quarantine_corrupt_db(self) -> None:
        """Move an unreadable db file (and its WAL/SHM sidecars) aside.

        Renames to ``<db_path>.corrupt-<timestamp>`` (preserving whatever is
        recoverable) rather than deleting it, then leaves the original path
        clear for the caller's retry of ``_init_db``/``prune`` to create a
        fresh database there. The ``-wal``/``-shm`` sidecars (present
        whenever a previous run's writer connection never got to checkpoint
        and close cleanly -- exactly the scenario a real corruption event
        like power loss produces) are moved the same way, best-effort, so
        they never end up sitting beside the fresh replacement database.

        Sets ``db_was_corrupt``/``corrupt_backup_path`` instead of raising,
        per §5: the machine must still start and run; a later task raises
        DATA-102 from these.

        If the rename of the *main* file itself fails (e.g. on Windows,
        another handle -- antivirus, a backup tool -- still open on it),
        retrying _init_db/prune against that same, still-corrupt path would
        just raise the same error again. Rather than let that crash the
        constructor, fall back to a fresh database at a different path in
        the same directory (``<db_path>.new-<timestamp>``) and switch
        ``self._db_path`` to it for the rest of this process's life. The
        corrupt original is left exactly where it was in that case (it
        could not even be moved), and ``corrupt_backup_path`` is left None
        since nothing was actually preserved aside.
        """
        original_path = self._db_path
        timestamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
        backup_path = f"{original_path}.corrupt-{timestamp}"

        main_renamed = self._try_rename_aside(original_path, backup_path)
        for suffix in ("-wal", "-shm"):
            # Best-effort: a sidecar that doesn't exist or can't be moved
            # never blocks recovery of the main file -- there is no better
            # fallback for a stray WAL/SHM file than leaving it in place.
            self._try_rename_aside(f"{original_path}{suffix}", f"{backup_path}{suffix}")

        self.db_was_corrupt = True

        if main_renamed is not False:
            # True: renamed. None: nothing was there to rename. Either way
            # original_path is now clear for a fresh database.
            self.corrupt_backup_path = backup_path if main_renamed else None
            logger.error(
                f"EventRecorder: {original_path} was unreadable; the previous "
                f"file has been preserved at {backup_path} and a fresh "
                f"database will now be created at {original_path}"
            )
            return

        fallback_path = f"{original_path}.new-{timestamp}"
        self.corrupt_backup_path = None
        self._db_path = fallback_path
        logger.error(
            f"EventRecorder: {original_path} was unreadable and could not be "
            f"renamed aside (left in place); a fresh database will instead "
            f"be created at {fallback_path} and used for this process"
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

    @staticmethod
    def _configure_sale_connection(conn: sqlite3.Connection) -> None:
        """WAL + synchronous=NORMAL, shared by record_sale and replay_sales_journal."""
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")

    @staticmethod
    def _insert_sale_row(
        conn: sqlite3.Connection,
        ts: float,
        sku: str,
        name: str,
        slot: Optional[int],
        price: float,
        methods: dict,
        *,
        idempotent: bool = False,
    ) -> int:
        """Insert one row into `sales`. Returns the number of rows inserted.

        idempotent=False (record_sale's default, for a fresh live sale) is
        a plain insert -- it always inserts exactly one row.

        idempotent=True (replay_sales_journal always; record_sale only when
        its caller opts in with a deterministic ts) inserts a row only if
        no row with the same (ts, sku) already exists, so inserting an
        already-committed (ts, sku) a second time (e.g. replay running
        twice against the same journal line after a crash between the DB
        commit and the journal truncation, or the PAY-104 recovery route
        retrying against the same session snapshot) writes nothing instead
        of a duplicate. (ts, sku) is safe as a natural key for both
        callers: replay_sales_journal's ts comes from time.time()
        (sub-microsecond resolution) and the FSM sells one item at a time
        -- record_sale is awaited before the FSM returns to idle -- so two
        genuinely distinct live sales can never share both ts and sku; the
        PAY-104 route's ts is the session snapshot's saved_at, fixed once
        at dispense time and never rewritten, so two attempts to record
        the *same* pending sale share it while a genuinely *different*
        pending sale (a different dispense) gets a different saved_at. See
        replay_sales_journal for the full argument on its own case.
        """
        if idempotent:
            cur = conn.execute(
                "INSERT INTO sales (ts, sku, name, slot, price, methods) "
                "SELECT ?, ?, ?, ?, ?, ? WHERE NOT EXISTS ("
                "SELECT 1 FROM sales WHERE ts = ? AND sku = ?)",
                (ts, sku, name, slot, price, json.dumps(methods), ts, sku),
            )
        else:
            cur = conn.execute(
                "INSERT INTO sales (ts, sku, name, slot, price, methods) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (ts, sku, name, slot, price, json.dumps(methods)),
            )
        return cur.rowcount

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
        idempotent: bool = False,
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

        ``idempotent`` (default ``False``, last parameter, opt-in): when
        ``True``, routes through ``_insert_sale_row``'s ``INSERT ... SELECT
        ... WHERE NOT EXISTS`` form keyed on ``(ts, sku)`` instead of a
        plain insert -- the same mechanism ``replay_sales_journal`` already
        relies on so a crash between its insert and its journal truncation
        cannot duplicate a row (see ``_insert_sale_row``'s docstring for
        why ``(ts, sku)`` cannot collide between two genuinely distinct
        sales). **Every existing caller keeps today's plain-insert
        behaviour** -- a live sale from the FSM's dispense path must never
        be deduplicated, because two genuine sales of the same SKU (two
        different customers, back to back) are a normal thing and each
        one is a real row. This flag exists for exactly one caller: the
        PAY-104 recovery route (``web_interface/routes/health.py``), which
        has a caller-supplied, deterministic ``ts`` (the session
        snapshot's ``saved_at``, fixed at dispense time and never
        rewritten) to key on, making a second attempt against the same
        pending sale -- same process, a different process, after a
        restart, disk fixed or not -- insert zero rows instead of a
        duplicate. Callers that pass ``idempotent=True`` without also
        pinning ``ts`` to something stable get no benefit from this (a
        fresh ``time.time()`` default never repeats), so this is opt-in
        precisely where a caller already has a natural, stable key and
        opt-out (the default) everywhere a fresh sale's timestamp is
        expected to differ from every other sale's.

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
        Journalling a row that was meant to be idempotent is still safe:
        ``replay_sales_journal`` always inserts idempotently on
        ``(ts, sku)`` regardless of how the row reached the journal, so a
        later replay of this same row cannot duplicate it either.

        If the journal append *itself* also fails (e.g. the same full or
        read-only volume that caused the insert to fail), this raises
        ``SaleRecordingFailed`` instead of the original insert exception --
        the row is then recorded nowhere durable at all, which the plain
        "insert failed, DATA-101, journal has it" path must not be
        mistaken for. See ``SaleRecordingFailed``'s docstring and
        ``VMC._record_sale``.
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
            self._configure_sale_connection(conn)
            self._insert_sale_row(
                conn, ts, sku, name, slot, price, methods, idempotent=idempotent
            )
            conn.commit()
        except Exception as insert_exc:
            logger.exception(
                f"EventRecorder: record_sale failed for sku={sku!r}; journaling instead"
            )
            try:
                _append_journal_line(payload)
            except Exception:
                logger.exception(
                    f"EventRecorder: record_sale's journal fallback ALSO failed "
                    f"for sku={sku!r}; the sale is not recorded anywhere durable "
                    "-- raising SaleRecordingFailed instead of the plain insert "
                    "error so the caller cannot mistake this for the ordinary, "
                    "journal-covered failure path"
                )
                raise SaleRecordingFailed(
                    f"record_sale and its journal fallback both failed for sku={sku!r}"
                ) from insert_exc
            raise
        finally:
            if conn is not None:
                conn.close()

    def replay_sales_journal(self) -> int:
        """Insert any journalled sales; return the count actually inserted.

        Returns 0 with the journal file left completely untouched (absent
        stays absent, an empty file stays empty) when there is nothing to
        do. Otherwise the journal is rewritten to hold **exactly the rows
        that remain unresolved**: a row that was inserted, that was
        recognized as an already-committed duplicate (see idempotency
        below), or that was successfully set aside as rejected evidence is
        removed from the file; a row that could not be inserted *and*
        could not even be written to the rejected-evidence file is kept,
        verbatim, for a later retry.

        The return value is always the count of rows this call genuinely
        **inserted** into `sales` -- nothing else. It is deliberately
        **not** the signal a caller should use to decide whether every
        journalled sale is now durably accounted for: 0 is ambiguous by
        itself (it means both "there was nothing to do" and "there was
        content, but every row was a duplicate or a reject -- nothing new
        landed"). The unambiguous signal is the journal's state *after*
        this call returns: **a caller should clear DATA-101 when
        `JOURNAL_PATH` is now absent or empty, not when this method's
        return value is greater than zero.** A non-empty journal after
        this call means at least one row is still stuck and DATA-101 must
        stay set; an absent-or-empty one means every row that was in it is
        now either in `sales` or durably preserved as rejected evidence.

        A well-formed line is inserted even when the file's final line is a
        partial write (no trailing newline, invalid JSON) -- that one line
        is dropped (logged, never kept) since a partially-written line can
        never be completed by any later retry; it never costs the earlier
        good lines.

        Each well-formed row is inserted in its own transaction (commit or
        rollback per row), for two reasons:

        - Idempotency: the insert only happens if no row with the same
          (ts, sku) already exists (see _insert_sale_row). This is what
          makes replay safe to run twice against the same already-committed
          line -- e.g. a crash between the DB commit and the journal
          rewrite below would otherwise leave the line to be replayed
          again. When this happens the insert affects 0 rows -- not an
          error -- and is logged distinctly (naming ts and sku) so a
          genuine skip is never silently indistinguishable from a sale
          quietly dropped. (ts, sku) cannot collide between two genuinely
          distinct sales: ts is time.time() (sub-microsecond resolution)
          and the FSM sells one item at a time -- record_sale is awaited
          before the FSM returns to idle.
        - A row that cannot be inserted at all (e.g. a well-formed line
          whose sku is null, violating the NOT NULL column) must not roll
          back its neighbours, and must not be retried forever either --
          that would silently block every later journalled sale from ever
          reaching `sales` again, with no way for DATA-101 to clear.
          Instead it is set aside (durably) in a
          `sales-journal.rejected.jsonl` file beside the journal and
          logged, so an operator can find it, while replay itself
          completes and the fault clears. If *that* write also fails (e.g.
          the same full disk that caused the insert to fail in the first
          place), the row is neither lost nor silently dropped: it is kept
          in the journal, verbatim, for a later retry, and this method
          still returns normally rather than raising -- see Finding B/C of
          the round-3 review.

        Known limitation (deferred, Finding E of the round-3 review): the
        rejected-evidence file itself is unbounded -- see
        _append_rejected_sale_line's docstring.
        """
        if not JOURNAL_PATH.exists():
            return 0
        content = JOURNAL_PATH.read_text(encoding="utf-8")
        if not content.strip():
            return 0

        count = 0
        remaining_lines: list[str] = []
        conn = sqlite3.connect(self._db_path, timeout=5.0)
        try:
            self._configure_sale_connection(conn)
            for line in content.splitlines():
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    # A partial write (e.g. a crash mid-append) can never
                    # be completed by a later retry -- drop it, it is not
                    # kept in the rewritten journal.
                    logger.warning(
                        "EventRecorder: dropping corrupt/partial journal "
                        f"line: {line!r}"
                    )
                    continue

                try:
                    inserted = self._insert_sale_row(
                        conn,
                        row["ts"],
                        row["sku"],
                        row["name"],
                        row.get("slot"),
                        row["price"],
                        row["methods"],
                        idempotent=True,
                    )
                    conn.commit()
                except Exception:
                    conn.rollback()
                    logger.exception(
                        "EventRecorder: journalled sale row could not be "
                        f"inserted (sku={row.get('sku')!r}); setting it "
                        "aside as evidence rather than blocking replay"
                    )
                    try:
                        _append_rejected_sale_line(row)
                    except Exception:
                        logger.exception(
                            "EventRecorder: could not write rejected-sale "
                            f"evidence file for sku={row.get('sku')!r}; "
                            "leaving this row in the journal for a later "
                            "retry instead of losing it"
                        )
                        remaining_lines.append(line)
                    continue

                if inserted:
                    count += inserted
                else:
                    # rowcount 0: the idempotent WHERE NOT EXISTS matched
                    # an already-committed row at this exact (ts, sku) --
                    # a genuine replay of an already-durable sale, not an
                    # error and not silently dropped (Finding D).
                    logger.info(
                        "EventRecorder: replay skipped an already-recorded "
                        f"sale (duplicate) ts={row['ts']!r} sku={row['sku']!r}"
                    )
        finally:
            conn.close()

        if remaining_lines:
            _atomic_write_text(JOURNAL_PATH, "\n".join(remaining_lines) + "\n")
        else:
            _atomic_write_text(JOURNAL_PATH, "")
        return count

    def flush(self, timeout: float = 5.0) -> None:
        """Block until every queued row is written (tests, shutdown, reads).

        Returns immediately -- without waiting out `timeout` -- if the
        writer thread is not alive (its initial database connect failed;
        see `_writer_loop`). Once that thread has died nothing will ever
        call `task_done()` again, so the old unconditional wait-loop would
        spin to the full timeout on *every* future call, and
        `get_historical_average` alone calls `_compute_window` (which calls
        `flush()`) up to 30 times per invocation.
        """
        if not self._writer.is_alive():
            n = self._queue.unfinished_tasks
            if n:
                logger.warning(
                    f"EventRecorder: flush() called but the writer thread "
                    f"for db_path={self._db_path!r} is not running; "
                    f"returning immediately with {n} row(s) that will never "
                    "be written"
                )
            return
        deadline = time.monotonic() + timeout
        while self._queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.005)
        n = self._queue.unfinished_tasks
        if n:
            logger.warning(f"EventRecorder: flush timed out with {n} rows still queued")

    def _writer_loop(self) -> None:
        try:
            conn = sqlite3.connect(self._db_path, check_same_thread=False)
        except Exception:
            logger.exception(
                f"EventRecorder: writer thread could not open db_path="
                f"{self._db_path!r}; this daemon thread is exiting and no "
                "queued event or cash-collection row will ever be written "
                "for the rest of this process. record() will keep "
                "accepting rows without raising (they simply accumulate "
                "unwritten); flush() will detect this thread is not alive "
                "and return immediately instead of spinning to its full "
                "timeout on every call."
            )
            return
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
        # `id DESC` breaks a tied `ts` deterministically (see
        # `services/reports.py`'s `collections()` docstring for the
        # identical hazard). Benign either way here: two rows can only tie
        # on `ts` if they share the same timestamp, so whichever is picked
        # as "previous" yields an identical `WHERE sales.ts > ?` cutoff --
        # added for consistency and defensive clarity, not because either
        # choice was wrong.
        prev = conn.execute(
            "SELECT ts FROM cash_collections ORDER BY ts DESC, id DESC LIMIT 1"
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
