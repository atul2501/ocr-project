"""Ticket/status store for the async upload pipeline, backed by Postgres
(see db.py) so tickets, the dedup index, and in-flight PDF bytes all
survive a process restart *and* a redeploy - Render's local disk is
ephemeral across deploys, so anything durable has to live in the database,
not a file. A PDF is deduplicated by content hash, not filename, so
resubmitting the same file returns the existing ticket instead of kicking
off a second background job.
"""

import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

import db

logger = logging.getLogger(__name__)

QUEUED = "QUEUED"
RECEIVED = "PROCESSING"
OCR_PROCESSING = "OCR_PROCESSING"
VALIDATING = "VALIDATING"
COMPLETED = "COMPLETED"
FAILED = "FAILED"

_TERMINAL = {COMPLETED}  # FAILED is retryable, so it's not terminal for dedup purposes
_NON_TERMINAL = {QUEUED, RECEIVED, OCR_PROCESSING, VALIDATING}  # statuses a
                            # ticket can be "stuck" in if the process dies
                            # mid-job - used by reconcile_pending() on startup

CACHE_TTL_SECONDS = 7 * 24 * 60 * 60  # 1 week

PENDING_DIR = "pending"  # local scratch copy of the PDF while a job is
                          # in-flight, used as a fast path so workers don't
                          # round-trip through the database on every page -
                          # the durable copy lives in the pdf_bytes column,
                          # so reconcile_pending() can restore this file
                          # even after a redeploy wipes local disk


@dataclass
class Job:
    ticket_id: str
    content_hash: str
    status: str = QUEUED
    progress: int = 5
    message: str = "PDF received successfully. Ticket created. Queued for processing."
    total_pages: int = 0
    result: Optional[list] = None
    error: Optional[str] = None
    sap_status: str = "NOT_IMPLEMENTED"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "ticket_id": self.ticket_id,
            "status": self.status,
            "progress": self.progress,
            "message": self.message,
            "result": self.result,
            "error": self.error,
            "sap_status": self.sap_status,
        }


def pending_path(ticket_id: str) -> str:
    return os.path.join(PENDING_DIR, f"{ticket_id}.pdf")


def safe_remove(path: str) -> None:
    """Best-effort delete - a missing/locked file shouldn't crash a job."""
    try:
        os.remove(path)
    except OSError:
        pass


def _row_to_job(row) -> Job:
    return Job(
        ticket_id=row["ticket_id"],
        content_hash=row["content_hash"],
        status=row["status"],
        progress=row["progress"],
        message=row["message"],
        total_pages=row["total_pages"],
        result=row["result"],
        error=row["error"],
        sap_status=row["sap_status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


async def purge_expired() -> None:
    """Drop any ticket older than CACHE_TTL_SECONDS from the database."""
    cutoff = time.time() - CACHE_TTL_SECONDS
    deleted = await db.pool().fetch(
        "DELETE FROM jobs WHERE created_at < $1 RETURNING ticket_id", cutoff
    )
    if deleted:
        logger.info(f"purged {len(deleted)} expired ticket(s)")


async def reconcile_pending() -> list[str]:
    """Called once at server startup, after db.connect(). A ticket can be
    left in a non-terminal status if the process died mid-job (crash,
    redeploy, OOM-kill) - without this it would sit "in progress" forever
    with no worker ever picking it back up.

    For each such ticket: if its spooled PDF is still on local disk, hand
    its ID back to the caller to re-queue. If not (e.g. a redeploy wiped
    local disk), restore it from the durable pdf_bytes column instead of
    giving up. Only if neither copy exists is the ticket marked FAILED, so
    the caller knows to resubmit rather than poll a ticket that will never
    finish. Also sweeps pending/ of files that don't belong to any active
    ticket (e.g. an upload that was still streaming to disk when the
    process died).
    """
    rows = await db.pool().fetch(
        "SELECT ticket_id, pdf_bytes FROM jobs WHERE status = ANY($1::text[])",
        list(_NON_TERMINAL),
    )

    to_requeue = []
    failed = 0
    for row in rows:
        ticket_id = row["ticket_id"]
        path = pending_path(ticket_id)
        if os.path.isfile(path):
            to_requeue.append(ticket_id)
            continue
        pdf_bytes = row["pdf_bytes"]
        if pdf_bytes:
            os.makedirs(PENDING_DIR, exist_ok=True)
            with open(path, "wb") as f:
                f.write(pdf_bytes)
            to_requeue.append(ticket_id)
            logger.info(f"restored pending PDF from database: {ticket_id}")
        else:
            await update(
                ticket_id,
                status=FAILED,
                message="Processing failed",
                error="Interrupted by a server restart before this file could be processed - please resubmit",
            )
            failed += 1
    if rows:
        logger.info(f"reconciled {len(rows)} in-flight ticket(s) from before restart: {len(to_requeue)} re-queued, {failed} marked failed")

    if os.path.isdir(PENDING_DIR):
        active = set(to_requeue)
        removed = 0
        for name in os.listdir(PENDING_DIR):
            ticket_id = name[:-4] if name.endswith(".pdf") else None
            if ticket_id is None or ticket_id not in active:
                safe_remove(os.path.join(PENDING_DIR, name))
                removed += 1
        if removed:
            logger.info(f"swept {removed} orphaned file(s) from {PENDING_DIR}")

    return to_requeue


async def find_existing(content_hash: str) -> Optional[Job]:
    """Return the in-flight or completed job for this exact PDF, if any.

    A FAILED job is not returned here, so resubmitting the same PDF after
    a failure starts a fresh job/ticket rather than being stuck dedup'd
    onto the failed one.
    """
    await purge_expired()
    row = await db.pool().fetchrow(
        "SELECT * FROM jobs WHERE content_hash = $1 AND status != $2 "
        "ORDER BY created_at DESC LIMIT 1",
        content_hash, FAILED,
    )
    if row is None:
        logger.info(f"no existing ticket for content hash {content_hash[:12]}...")
        return None
    job = _row_to_job(row)
    logger.info(f"found existing ticket {job.ticket_id} (status={job.status}) for content hash {content_hash[:12]}...")
    return job


async def create_job(content_hash: str, pdf_bytes: bytes) -> Job:
    """pdf_bytes is kept in the database (not just PENDING_DIR) until the
    job reaches a terminal state, so it can be restored by reconcile_pending()
    if a redeploy happens mid-job."""
    await purge_expired()
    job = Job(ticket_id=f"TCK-{uuid.uuid4().hex[:12]}", content_hash=content_hash)
    await db.pool().execute(
        """INSERT INTO jobs (ticket_id, content_hash, status, progress, message,
                              total_pages, result, error, sap_status, pdf_bytes,
                              created_at, updated_at)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)""",
        job.ticket_id, job.content_hash, job.status, job.progress, job.message,
        job.total_pages, job.result, job.error, job.sap_status, pdf_bytes,
        job.created_at, job.updated_at,
    )
    logger.info(f"created ticket: {job.ticket_id} for content hash {content_hash[:12]}...")
    return job


async def get_job(ticket_id: str) -> Optional[Job]:
    await purge_expired()
    row = await db.pool().fetchrow("SELECT * FROM jobs WHERE ticket_id = $1", ticket_id)
    logger.debug(f"status lookup: {ticket_id} -> {'found' if row else 'not found'}")
    return _row_to_job(row) if row else None


async def update(ticket_id: str, **fields: Any) -> None:
    if not fields:
        return
    fields = dict(fields)
    fields["updated_at"] = time.time()
    if fields.get("status") in (COMPLETED, FAILED):
        # terminal - drop the durable PDF copy so finished tickets don't
        # keep a multi-MB blob sitting in the database for a week
        fields["pdf_bytes"] = None

    set_clause = ", ".join(f"{key} = ${i + 2}" for i, key in enumerate(fields))
    result = await db.pool().execute(
        f"UPDATE jobs SET {set_clause} WHERE ticket_id = $1", ticket_id, *fields.values()
    )
    if result == "UPDATE 0":
        logger.warning(f"update on unknown ticket: {ticket_id}")
        return
    logger.info(f"ticket {ticket_id} updated: {fields.get('status', '?')} - {fields.get('message', '')}")
