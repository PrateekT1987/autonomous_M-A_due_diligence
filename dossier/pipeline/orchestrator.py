"""Pipeline orchestration and the durable job queue.

One run = resolve → ingest → evidence → index → analyse → write → verify.
The orchestrator owns run state, progress, error handling, retries and the
clean-up of prior artefacts so that a re-run is reproducible rather than
additive.

Runs are recorded in the ``jobs`` table. In ``embedded`` mode a single worker
thread drains that queue inside the API process; in ``worker`` mode the API only
enqueues and a separate ``python -m dossier.cli worker`` process does the work.
Because state lives in SQLite, either mode survives a restart: queued jobs are
picked up again and stale in-flight jobs are recovered.
"""
from __future__ import annotations

import logging
import os
import random
import socket
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone
from typing import Callable

from .. import config, db, obs
from ..llm import LLM
from ..obs import METRICS
from .analyse import AnalystAgent
from .research import ResearchAgent
from .write import WriterAgent

log = logging.getLogger("dossier.orchestrator")

STAGES: list[tuple[str, str, float]] = [
    ("resolve", "Resolving the target", 0.03),
    ("filings", "Reading SEC filings", 0.18),
    ("financials", "Extracting financial statements", 0.34),
    ("news", "Scanning recent news", 0.43),
    ("evidence", "Building the evidence ledger", 0.62),
    ("index", "Indexing passages for retrieval", 0.74),
    ("analyse", "Scoring the risk matrix", 0.86),
    ("write", "Drafting the information memorandum", 0.96),
    ("verify", "Verifying every citation", 1.0),
]
STAGE_LABEL = {key: label for key, label, _ in STAGES}
STAGE_PROGRESS = {key: p for key, _, p in STAGES}

WORKER_ID = f"{socket.gethostname()}-{os.getpid()}-{random.randrange(16**6):06x}"

_active_lock = threading.Lock()
_worker_lock = threading.Lock()
_worker_thread: threading.Thread | None = None
_worker_stop: threading.Event | None = None


class RunBusy(RuntimeError):
    pass


class RunCancelled(RuntimeError):
    pass


def _deal(deal_id: str) -> dict:
    row = db.q1("SELECT * FROM deals WHERE id = ?", (deal_id,))
    if not row:
        raise ValueError(f"Unknown deal {deal_id}")
    return dict(row)


def emit(
    run_id: str,
    deal_id: str,
    stage: str,
    message: str,
    level: str = "info",
    data: dict | None = None,
    trace_id: str | None = None,
) -> None:
    db.x(
        "INSERT INTO run_events (run_id, deal_id, ts, stage, level, message, data, trace_id) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (
            run_id,
            deal_id,
            db.now(),
            stage,
            level,
            message,
            db.dumps(data or {}),
            trace_id or obs.current_trace_id(),
        ),
    )
    log.info(
        "[%s/%s] %s",
        run_id,
        stage,
        message,
        extra={
            "run_id": run_id,
            "deal_id": deal_id,
            "stage": stage,
            "trace_id": trace_id or obs.current_trace_id(),
        },
    )


def _set_run(run_id: str, **fields) -> None:
    if not fields:
        return
    sets = ", ".join(f"{k} = ?" for k in fields)
    db.x(f"UPDATE runs SET {sets} WHERE id = ?", (*fields.values(), run_id))


def _heartbeat(run_id: str) -> None:
    stamp = db.now()
    db.x("UPDATE jobs SET heartbeat_at = ?, updated_at = ? WHERE run_id = ? AND status = 'running'", (stamp, stamp, run_id))


def _cancelled(run_id: str) -> bool:
    row = db.q1("SELECT cancel_requested, status FROM runs WHERE id = ?", (run_id,))
    return bool(row and (row["cancel_requested"] or row["status"] == "cancelled"))


def clear_artifacts(deal_id: str) -> None:
    """Remove everything derived from a previous run, keeping the deal record."""
    for table in ("chunks", "risks", "documents", "evidence", "sources"):
        db.x(f"DELETE FROM {table} WHERE deal_id = ?", (deal_id,))
    db.x(
        "UPDATE deals SET status = 'draft', summary = '{}', updated_at = ? WHERE id = ?",
        (db.now(), deal_id),
    )


def _new_run_id(deal_id: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    base = f"RUN-{stamp}-{deal_id[-4:]}"
    candidate, n = base, 2
    while db.q1("SELECT 1 FROM runs WHERE id = ?", (candidate,)):
        candidate = f"{base}-{n}"
        n += 1
    return candidate


def _enqueue(
    deal_id: str,
    run_id: str,
    *,
    polish: bool | None = None,
    trace_id: str | None = None,
    delay_seconds: int = 0,
) -> None:
    now = db.now()
    available = (
        datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)
    ).isoformat(timespec="seconds")
    db.insert(
        "jobs",
        {
            "deal_id": deal_id,
            "run_id": run_id,
            "status": "queued",
            "attempts": 0,
            "max_attempts": max(1, int(config.JOB_MAX_ATTEMPTS or 1)),
            "polish": None if polish is None else (1 if polish else 0),
            "available_at": available if delay_seconds else now,
            "created_at": now,
            "updated_at": now,
            "trace_id": trace_id or obs.current_trace_id(),
        },
    )
    METRICS.gauge("dossier_queue_depth", _queue_depth())


def create_run(
    deal_id: str,
    mode: str = "full",
    polish: bool | None = None,
    trace_id: str | None = None,
) -> str:
    deal = _deal(deal_id)
    trace_id = trace_id or obs.current_trace_id()
    with _active_lock:
        active = db.q1(
            "SELECT id FROM runs WHERE deal_id = ? AND status IN ('queued', 'running') ORDER BY started_at DESC LIMIT 1",
            (deal_id,),
        )
        if active:
            raise RunBusy(f"A run is already in progress ({active['id']})")
        run_id = _new_run_id(deal_id)
        try:
            db.insert(
                "runs",
                {
                    "id": run_id,
                    "deal_id": deal_id,
                    "status": "queued",
                    "stage": "queued",
                    "progress": 0.0,
                    "mode": mode,
                    "started_at": db.now(),
                    "stats": "{}",
                    "trace_id": trace_id,
                },
            )
        except Exception as exc:  # noqa: BLE001 - the unique index rejects a race
            if "UNIQUE" in str(exc).upper():
                raise RunBusy("A run is already in progress for this deal") from exc
            raise
    try:
        _enqueue(deal_id, run_id, polish=polish, trace_id=trace_id)
    except Exception:
        _set_run(run_id, status="failed", stage="error", finished_at=db.now(), error="could not enqueue run")
        raise
    emit(
        run_id,
        deal_id,
        "queued",
        f"Run queued for {deal.get('company') or deal['name']}",
        data={"mode": mode},
        trace_id=trace_id,
    )
    METRICS.inc("dossier_runs_enqueued_total", {"mode": mode})
    return run_id


def run_pipeline(deal_id: str, run_id: str, *, polish: bool | None = None, llm: LLM | None = None) -> None:
    """Execute every stage. Always leaves the run in a terminal state."""
    deal = _deal(deal_id)
    row = db.q1("SELECT trace_id FROM runs WHERE id = ?", (run_id,))
    trace_id = (row["trace_id"] if row and row["trace_id"] else None) or run_id
    obs.set_trace_id(trace_id)
    llm = (llm or LLM()).bind(deal_id=deal_id, run_id=run_id, trace_id=trace_id)
    polish = config.resolve_polish(polish)
    _set_run(run_id, status="running")
    emit(
        run_id,
        deal_id,
        "start",
        f"Pipeline started · {llm.provider} · {'polish' if polish else 'deterministic'}",
        data={"llm": llm.available(), "provider": llm.provider, "polish": polish},
        trace_id=trace_id,
    )
    stats: dict = {}
    started = time.monotonic()

    def step(stage: str, fn: Callable[[], None]) -> None:
        if _cancelled(run_id):
            raise RunCancelled("Run cancelled by operator")
        _heartbeat(run_id)
        _set_run(run_id, stage=stage, progress=STAGE_PROGRESS[stage])
        emit(run_id, deal_id, stage, STAGE_LABEL[stage])
        with obs.span(f"pipeline.{stage}", **{"dossier.deal_id": deal_id, "dossier.run_id": run_id}):
            fn()
        if _cancelled(run_id):
            raise RunCancelled("Run cancelled by operator")

    try:
        research = ResearchAgent(deal, run_id, llm)
        step("resolve", research.resolve)

        # Re-read the deal: resolve() filled company/ticker/cik.
        deal = _deal(deal_id)
        research = ResearchAgent(deal, run_id, llm)
        step("filings", research.ingest_filings)
        step("financials", research.ingest_financials)
        step("news", research.ingest_news)
        step("evidence", research.build_evidence)
        stats["evidence"] = db.q1("SELECT COUNT(*) c FROM evidence WHERE deal_id = ?", (deal_id,))["c"]
        stats["sources"] = db.q1("SELECT COUNT(*) c FROM sources WHERE deal_id = ?", (deal_id,))["c"]
        step("index", research.build_index)

        deal = _deal(deal_id)
        analyst = AnalystAgent(deal, run_id, llm)
        step("analyse", lambda: analyst.build(polish=polish))
        stats["risks"] = db.q1("SELECT COUNT(*) c FROM risks WHERE deal_id = ?", (deal_id,))["c"]

        deal = _deal(deal_id)
        writer = WriterAgent(deal, run_id, llm)
        doc = writer.build_and_store(polish=polish)
        stats["words"] = doc["stats"]["words"]
        stats["trust"] = doc["trust"]["score"]
        stats["coverage"] = doc["trust"]["citation_coverage"]
        stats["llm_calls"] = llm.calls
        stats["llm_tokens"] = llm.prompt_tokens + llm.completion_tokens

        _set_run(run_id, stage="verify", progress=0.99)
        emit(
            run_id,
            deal_id,
            "verify",
            f"Trust check · {doc['trust']['score']}% · citation coverage {doc['trust']['citation_coverage']}% · "
            f"{len(doc['trust']['numeric_conflicts'])} numeric conflicts",
            data=doc["trust"].get("unsupported") and {"unsupported": len(doc["trust"]["unsupported"])} or {},
        )

        _set_run(
            run_id,
            status="complete",
            stage="done",
            progress=1.0,
            finished_at=db.now(),
            stats=db.dumps(stats),
        )
        emit(run_id, deal_id, "done", "Run complete", data=stats)
        db.audit(
            "run.complete",
            actor="worker",
            tenant_id=deal.get("tenant_id") or "default",
            deal_id=deal_id,
            run_id=run_id,
            trace_id=trace_id,
            detail=stats,
        )
        METRICS.inc("dossier_runs_total", {"status": "complete"})
        METRICS.observe("dossier_run_duration_seconds", time.monotonic() - started)
    except RunCancelled:
        _set_run(
            run_id,
            status="cancelled",
            stage="cancelled",
            finished_at=db.now(),
            stats=db.dumps(stats),
        )
        emit(run_id, deal_id, "cancelled", "Run cancelled", level="warn")
        db.x("UPDATE deals SET status = 'draft', updated_at = ? WHERE id = ?", (db.now(), deal_id))
        db.audit(
            "run.cancelled",
            actor="worker",
            tenant_id=deal.get("tenant_id") or "default",
            deal_id=deal_id,
            run_id=run_id,
            trace_id=trace_id,
        )
        METRICS.inc("dossier_runs_total", {"status": "cancelled"})
    except Exception as exc:  # noqa: BLE001 - a run must fail visibly, not crash the server
        log.exception("Run %s failed", run_id, extra={"run_id": run_id, "deal_id": deal_id})
        _set_run(
            run_id,
            status="failed",
            stage="error",
            finished_at=db.now(),
            error=f"{type(exc).__name__}: {exc}",
            stats=db.dumps(stats),
        )
        emit(
            run_id,
            deal_id,
            "error",
            f"{type(exc).__name__}: {exc}",
            level="error",
            data={"trace": traceback.format_exc()[-2000:]},
        )
        db.x("UPDATE deals SET status = 'error', updated_at = ? WHERE id = ?", (db.now(), deal_id))
        db.audit(
            "run.failed",
            actor="worker",
            tenant_id=deal.get("tenant_id") or "default",
            deal_id=deal_id,
            run_id=run_id,
            trace_id=trace_id,
            detail={"error": f"{type(exc).__name__}: {exc}"[:500]},
        )
        obs.alert(
            "run_failed",
            deal_id=deal_id,
            run_id=run_id,
            error=f"{type(exc).__name__}: {exc}"[:300],
        )
        METRICS.inc("dossier_runs_total", {"status": "failed"})
        METRICS.observe("dossier_run_duration_seconds", time.monotonic() - started)


# --------------------------------------------------------------------- queue
def _queue_depth() -> int:
    row = db.q1("SELECT COUNT(*) c FROM jobs WHERE status IN ('queued', 'running')")
    return int(row["c"]) if row else 0


def claim_job(worker_id: str = WORKER_ID) -> dict | None:
    """Atomically claim the oldest runnable job for this worker."""
    now = db.now()
    with db.tx() as c:
        row = c.execute(
            """
            SELECT j.* FROM jobs j
            WHERE j.status = 'queued'
              AND j.available_at <= ?
              AND NOT EXISTS (
                  SELECT 1 FROM jobs o
                  WHERE o.deal_id = j.deal_id AND o.status = 'running'
              )
            ORDER BY j.id
            LIMIT 1
            """,
            (now,),
        ).fetchone()
        if not row:
            return None
        cur = c.execute(
            """
            UPDATE jobs
            SET status = 'running', attempts = attempts + 1, locked_by = ?,
                locked_at = ?, heartbeat_at = ?, updated_at = ?
            WHERE id = ? AND status = 'queued'
            """,
            (worker_id, now, now, now, row["id"]),
        )
        if cur.rowcount != 1:
            return None
        job = dict(row)
    job.update({"status": "running", "attempts": job["attempts"] + 1, "locked_by": worker_id})
    METRICS.inc("dossier_jobs_claimed_total")
    METRICS.gauge("dossier_queue_depth", _queue_depth())
    return job


def _finish_job(run_id: str, status: str, error: str | None = None) -> None:
    db.x(
        "UPDATE jobs SET status = ?, error = ?, updated_at = ? WHERE run_id = ?",
        (status, error, db.now(), run_id),
    )
    METRICS.gauge("dossier_queue_depth", _queue_depth())


def process_job(job: dict, *, llm: LLM | None = None) -> None:
    """Run one claimed job, retrying transient failures up to max_attempts."""
    run_id = job["run_id"]
    deal_id = job["deal_id"]
    if job.get("trace_id"):
        obs.set_trace_id(job["trace_id"])
    polish = None if job.get("polish") is None else bool(job.get("polish"))
    try:
        run_pipeline(deal_id, run_id, polish=polish, llm=llm)
    except Exception as exc:  # noqa: BLE001 - run_pipeline normally absorbs errors
        log.exception("Unhandled job failure for %s", run_id)
        _set_run(run_id, status="failed", stage="error", finished_at=db.now(), error=str(exc)[:500])
        emit(run_id, deal_id, "error", f"{type(exc).__name__}: {exc}", level="error")

    run = db.q1("SELECT status FROM runs WHERE id = ?", (run_id,))
    status = run["status"] if run else "failed"
    if status == "complete":
        _finish_job(run_id, "done")
        return
    if status == "cancelled":
        _finish_job(run_id, "cancelled")
        return

    attempts = int(job.get("attempts") or 1)
    max_attempts = int(job.get("max_attempts") or config.JOB_MAX_ATTEMPTS or 1)
    if attempts < max_attempts:
        delay = min(2**attempts, 60)
        db.x(
            "UPDATE jobs SET status = 'queued', available_at = ?, locked_by = NULL, updated_at = ? WHERE run_id = ?",
            (
                (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat(timespec="seconds"),
                db.now(),
                run_id,
            ),
        )
        _set_run(run_id, status="queued", stage="queued", progress=0.0, finished_at=None, error=None)
        db.x("UPDATE deals SET status = 'researching', updated_at = ? WHERE id = ?", (db.now(), deal_id))
        emit(
            run_id,
            deal_id,
            "retry",
            f"Attempt {attempts} failed; retrying in {delay}s",
            level="warn",
            data={"attempt": attempts, "max_attempts": max_attempts},
        )
        METRICS.inc("dossier_jobs_retried_total")
    else:
        _finish_job(run_id, "failed", error=db.q1("SELECT error FROM runs WHERE id = ?", (run_id,))["error"])


def recover_stale_jobs() -> int:
    """Requeue jobs abandoned by a crashed worker or a restart."""
    cutoff = (
        datetime.now(timezone.utc) - timedelta(seconds=max(60, config.JOB_STALE_SECONDS))
    ).isoformat(timespec="seconds")
    rows = db.q(
        "SELECT * FROM jobs WHERE status = 'running' AND COALESCE(heartbeat_at, locked_at) < ?",
        (cutoff,),
    )
    recovered = 0
    for row in rows:
        job = dict(row)
        run = db.q1("SELECT status FROM runs WHERE id = ?", (job["run_id"],))
        if not run or run["status"] in ("complete", "failed", "cancelled"):
            _finish_job(job["run_id"], "done" if run and run["status"] == "complete" else "failed", "stale job reconciled")
            recovered += 1
            continue
        if int(job["attempts"] or 0) < int(job["max_attempts"] or 1):
            db.x(
                "UPDATE jobs SET status = 'queued', available_at = ?, locked_by = NULL, updated_at = ? WHERE id = ?",
                (db.now(), db.now(), job["id"]),
            )
            _set_run(job["run_id"], status="queued", stage="queued", progress=0.0, finished_at=None, error=None)
            emit(job["run_id"], job["deal_id"], "retry", "Recovered a stale run from a previous worker", level="warn")
            METRICS.inc("dossier_jobs_recovered_total")
        else:
            db.x(
                "UPDATE jobs SET status = 'failed', error = ?, updated_at = ? WHERE id = ?",
                ("worker heartbeat expired", db.now(), job["id"]),
            )
            _set_run(
                job["run_id"],
                status="failed",
                stage="error",
                finished_at=db.now(),
                error="worker heartbeat expired",
            )
            db.x("UPDATE deals SET status = 'error', updated_at = ? WHERE id = ?", (db.now(), job["deal_id"]))
        recovered += 1
    if recovered:
        log.warning("Recovered %s stale job(s)", recovered)
    METRICS.gauge("dossier_queue_depth", _queue_depth())
    return recovered


def worker_loop(stop: threading.Event | None = None, *, once: bool = False) -> None:
    """Drain the queue until stopped. Used by embedded mode and `dossier worker`."""
    poll = max(0.2, float(config.WORKER_POLL_SECONDS or 1.0))
    recover_stale_jobs()
    while stop is None or not stop.is_set():
        job = claim_job()
        if not job:
            if once:
                return
            if stop is not None:
                stop.wait(poll)
            else:
                time.sleep(poll)
            continue
        log.info("Worker %s running %s", WORKER_ID, job["run_id"], extra={"run_id": job["run_id"], "deal_id": job["deal_id"]})
        process_job(job)
        if once:
            return


def start_worker() -> threading.Thread | None:
    """Start the embedded worker exactly once. No-op in worker/test modes."""
    global _worker_thread, _worker_stop
    if config.RUN_MODE not in ("embedded", "auto", "dev", ""):
        return None
    with _worker_lock:
        if _worker_thread and _worker_thread.is_alive():
            return _worker_thread
        _worker_stop = threading.Event()
        _worker_thread = threading.Thread(
            target=worker_loop, args=(_worker_stop,), name="dossier-worker", daemon=True
        )
        _worker_thread.start()
        log.info("Embedded worker started (%s)", WORKER_ID)
        return _worker_thread


def stop_worker() -> None:
    global _worker_thread, _worker_stop
    with _worker_lock:
        if _worker_stop:
            _worker_stop.set()
        _worker_thread = None
        _worker_stop = None


def start_run(deal_id: str, *, mode: str = "full", polish: bool | None = None, llm: LLM | None = None) -> str:
    """Create a durable run and let the configured worker execute it."""
    if mode == "full":
        if is_running(deal_id):
            raise RunBusy("A run is already in progress for this deal")
        clear_artifacts(deal_id)
    run_id = create_run(deal_id, mode=mode, polish=polish)
    if mode == "full":
        db.x("UPDATE deals SET status = 'researching', updated_at = ? WHERE id = ?", (db.now(), deal_id))
    start_worker()
    return run_id


def run_sync(deal_id: str, *, polish: bool | None = None, llm: LLM | None = None) -> str:
    """Synchronous run for CLI/tests (no queue, no worker)."""
    clear_artifacts(deal_id)
    run_id = create_run(deal_id, polish=polish)
    db.x("DELETE FROM jobs WHERE run_id = ?", (run_id,))
    db.x("UPDATE deals SET status = 'researching', updated_at = ? WHERE id = ?", (db.now(), deal_id))
    run_pipeline(deal_id, run_id, polish=polish, llm=llm)
    return run_id


def cancel_run(run_id: str) -> dict:
    run = db.q1("SELECT * FROM runs WHERE id = ?", (run_id,))
    if not run:
        raise LookupError("Run not found")
    if run["status"] in ("complete", "failed", "cancelled"):
        return {"cancelled": False, "status": run["status"]}
    db.x("UPDATE runs SET cancel_requested = 1 WHERE id = ?", (run_id,))
    job = db.q1("SELECT * FROM jobs WHERE run_id = ?", (run_id,))
    if job and job["status"] == "queued":
        _finish_job(run_id, "cancelled")
        _set_run(run_id, status="cancelled", stage="cancelled", finished_at=db.now())
        db.x("UPDATE deals SET status = 'draft', updated_at = ? WHERE id = ?", (db.now(), run["deal_id"]))
    else:
        emit(run_id, run["deal_id"], "cancel", "Cancellation requested; stopping after the current stage", level="warn")
    METRICS.inc("dossier_runs_cancelled_total")
    return {"cancelled": True, "status": "cancelled" if not job or job["status"] == "queued" else "cancelling"}


def is_running(deal_id: str) -> bool:
    row = db.q1(
        "SELECT 1 FROM runs WHERE deal_id = ? AND status IN ('queued', 'running') LIMIT 1",
        (deal_id,),
    )
    return bool(row)


def active_run(deal_id: str) -> str | None:
    row = db.q1(
        "SELECT id FROM runs WHERE deal_id = ? AND status IN ('queued', 'running') ORDER BY started_at DESC LIMIT 1",
        (deal_id,),
    )
    return row["id"] if row else None


def latest_run(deal_id: str) -> dict | None:
    row = db.q1("SELECT * FROM runs WHERE deal_id = ? ORDER BY started_at DESC LIMIT 1", (deal_id,))
    return dict(row) if row else None


def run_status(run_id: str) -> dict | None:
    row = db.q1("SELECT * FROM runs WHERE id = ?", (run_id,))
    if not row:
        return None
    d = dict(row)
    d["stats"] = db.loads(d.get("stats"), {})
    return d


def queue_stats() -> dict:
    rows = db.q(
        "SELECT status, COUNT(*) c FROM jobs GROUP BY status"
    )
    counts = {r["status"]: int(r["c"]) for r in rows}
    return {
        "mode": config.RUN_MODE,
        "worker_id": WORKER_ID,
        "queued": counts.get("queued", 0),
        "running": counts.get("running", 0),
        "done": counts.get("done", 0),
        "failed": counts.get("failed", 0),
        "cancelled": counts.get("cancelled", 0),
    }
