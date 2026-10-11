"""Startup reconciliation of sales-journal and event-database faults."""

from loguru import logger

from contracts.vending_machine import FaultCode
from controller.machine import Machine
from services.event_recorder import EventRecorder
from services import event_recorder as event_recorder_module


def reconcile_sales_journal_faults(machine: Machine, recorder: EventRecorder) -> None:
    """At startup: replay any journalled sales and reconcile DATA-101/DATA-102.

    Never exits and never raises out to the caller — a reports/history
    problem must never stop the VMC or MQTT client (program goal 9), so
    every step here is best-effort and any unexpected failure is logged and
    swallowed rather than propagated.

    - ``DATA-102`` is raised once whenever the recorder reports it quarantined
      a corrupt database on this boot (``recorder.db_was_corrupt``, set by
      ``EventRecorder.__init__``, never by this function).
    - ``DATA-101`` is reconciled from the *journal's own state after replay*,
      never from ``replay_sales_journal()``'s return value: that integer is
      only the count of rows this call actually inserted, and it is ``0``
      both when there was nothing to do and when every row was a duplicate
      or a reject — see its docstring. The unambiguous signal is whether
      ``JOURNAL_PATH`` is now absent or empty (drained: clear the fault) or
      still has content (stuck: raise/keep the fault).
    - ``replay_sales_journal()`` itself can raise (e.g. it commits rows to
      ``sales`` but its final ``os.replace`` rewriting the journal cannot
      complete). That exception is caught here, separately from the outer
      swallow-everything handler, specifically so the journal-state check
      below still runs afterward — otherwise the outer handler would log
      and swallow it before ``DATA-101`` is ever reconciled, leaving the
      operator with no alert even though the journal is still non-empty
      (replayed rows included) and the next boot would have to rediscover
      the same problem from scratch.
    """
    try:
        if recorder.db_was_corrupt:
            detail = (
                recorder.corrupt_backup_path
                or "corrupt event database quarantined at startup"
            )
            machine.faults.raise_fault(FaultCode.DATA_102, outcome=detail)
            logger.error(f"Event database was reset after corruption: {detail}")

        try:
            inserted = recorder.replay_sales_journal()
            if inserted:
                logger.info(f"Sales journal replay: inserted {inserted} row(s)")
        except Exception:
            logger.exception(
                "replay_sales_journal raised (e.g. the journal rewrite could "
                "not complete); falling through to check the journal's "
                "current state so DATA-101 is still raised/retained rather "
                "than silently dropped"
            )

        journal_path = event_recorder_module.JOURNAL_PATH
        drained = (
            not journal_path.exists()
            or not journal_path.read_text(encoding="utf-8").strip()
        )
        if drained:
            machine.faults.clear_fault(FaultCode.DATA_101.value)
        else:
            machine.faults.raise_fault(
                FaultCode.DATA_101,
                outcome="sales journal not fully drained after replay",
            )
            logger.error(
                "Sales journal still has unresolved rows after replay; "
                "DATA-101 remains set"
            )
    except Exception:
        logger.exception(
            "reconcile_sales_journal_faults failed; continuing startup "
            "regardless (a reports/history problem must never stop the "
            "VMC or MQTT client)"
        )
