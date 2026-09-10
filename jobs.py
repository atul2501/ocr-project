"""Ticket/status store for the async upload pipeline.

No database by design (per demo requirements) - each ticket is instead
mirrored to a small JSON file under CACHE_DIR, so tickets survive a
process restart without needing a real DB. A PDF is deduplicated by
content hash, not filename, so resubmitting the same file returns the
existing ticket instead of kicking off a second background job.
"""

import hashlib
import json
import logging
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

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


CACHE_DIR = "cache"
CACHE_TTL_SECONDS = 7 * 24 * 60 * 60  # 1 week

PENDING_DIR = "pending"  # uploaded PDFs are spooled here (named
                          # "{ticket_id}.pdf") while queued/in-progress, so
                          # the upload queue only ever carries ticket IDs,
                          # never raw PDF bytes - see main.py's /upload

_lock = threading.Lock()
_jobs: dict[str, Job] = {}
_hash_to_ticket: dict[str, str] = {}


def _cache_path(ticket_id: str) -> str:
    return os.path.join(CACHE_DIR, f"{ticket_id}.json")


def pending_path(ticket_id: str) -> str:
    return os.path.join(PENDING_DIR, f"{ticket_id}.pdf")


def safe_remove(path: str) -> None:
    """Best-effort delete - a missing/locked file shouldn't crash a job."""
    try:
        os.remove(path)
    except OSError:
        pass


def _is_expired(job: Job) -> bool:
    return time.time() - job.created_at > CACHE_TTL_SECONDS


def _purge_expired() -> None:
    """Drop any ticket older than CACHE_TTL_SECONDS, from memory and disk."""
    with _lock:
        expired = [ticket_id for ticket_id, job in _jobs.items() if _is_expired(job)]
        for ticket_id in expired:
            job = _jobs.pop(ticket_id, None)
            if job is not None:
                _hash_to_ticket.pop(job.content_hash, None)
    for ticket_id in expired:
        try:
            os.remove(_cache_path(ticket_id))
        except OSError:
            pass
    if expired:
        logger.info(f"purged {len(expired)} expired ticket(s): {expired}")


def _save_to_cache(ticket_id: str, snapshot: dict) -> None:
    """Write (or overwrite) this ticket's cache file. Best-effort: a cache
    write failure shouldn't take down the job itself."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = _cache_path(ticket_id)
    tmp_path = f"{path}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(snapshot, f)
        os.replace(tmp_path, path)
        logger.debug(f"cached ticket to disk: {ticket_id}")
    except OSError:
        logger.warning(f"failed to write cache file for ticket: {ticket_id}")


def _load_cache() -> None:
    """Rebuild _jobs/_hash_to_ticket from cache/*.json on startup, dropping
    (and deleting) anything already past CACHE_TTL_SECONDS."""
    if not os.path.isdir(CACHE_DIR):
        return
    loaded = 0
    for name in os.listdir(CACHE_DIR):
        if not name.endswith(".json"):
            continue
        path = os.path.join(CACHE_DIR, name)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            job = Job(**data)
        except (OSError, ValueError, TypeError) as e:
            logger.warning(f"skipping unreadable cache file {path}: {type(e).__name__}: {e}")
            continue
        if _is_expired(job):
            try:
                os.remove(path)
            except OSError:
                pass
            continue
        _jobs[job.ticket_id] = job
        _hash_to_ticket[job.content_hash] = job.ticket_id
        loaded += 1
    logger.info(f"loaded {loaded} ticket(s) from cache: {CACHE_DIR}")


def reconcile_pending() -> list[str]:
    """Called once at server startup, after _load_cache(). A ticket can be
    left in a non-terminal status if the process died mid-job (crash,
    redeploy, OOM-kill) - without this it would sit "in progress" forever
    with no worker ever picking it back up.

    For each such ticket: if its spooled PDF is still on disk, hand its ID
    back to the caller to re-queue; otherwise mark it FAILED so the caller
    knows to resubmit rather than poll a ticket that will never finish.
    Also sweeps pending/ of files that don't belong to any active ticket
    (e.g. an upload that was still streaming to disk when the process died).
    """
    with _lock:
        stuck = [job for job in _jobs.values() if job.status in _NON_TERMINAL]

    to_requeue = []
    for job in stuck:
        if os.path.isfile(pending_path(job.ticket_id)):
            to_requeue.append(job.ticket_id)
        else:
            update(
                job.ticket_id,
                status=FAILED,
                message="Processing failed",
                error="Interrupted by a server restart before this file could be processed - please resubmit",
            )
    if stuck:
        logger.info(f"reconciled {len(stuck)} in-flight ticket(s) from before restart: {len(to_requeue)} re-queued, {len(stuck) - len(to_requeue)} marked failed")

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


_load_cache()


def hash_pdf(pdf_bytes: bytes) -> str:
    return hashlib.sha256(pdf_bytes).hexdigest()


def find_existing(content_hash: str) -> Optional[Job]:
    """Return the in-flight or completed job for this exact PDF, if any.

    A FAILED job is not returned here, so resubmitting the same PDF after
    a failure starts a fresh job/ticket rather than being stuck dedup'd
    onto the failed one.
    """
    _purge_expired()
    with _lock:
        ticket_id = _hash_to_ticket.get(content_hash)
        if ticket_id is None:
            logger.info(f"no existing ticket for content hash {content_hash[:12]}...")
            return None
        job = _jobs.get(ticket_id)
        if job is not None and job.status != FAILED:
            logger.info(f"found existing ticket {ticket_id} (status={job.status}) for content hash {content_hash[:12]}...")
            return job
        return None


def create_job(content_hash: str) -> Job:
    _purge_expired()
    ticket_id = f"TCK-{uuid.uuid4().hex[:12]}"
    job = Job(ticket_id=ticket_id, content_hash=content_hash)
    with _lock:
        _jobs[ticket_id] = job
        _hash_to_ticket[content_hash] = ticket_id
        snapshot = asdict(job)
    _save_to_cache(ticket_id, snapshot)
    logger.info(f"created ticket: {ticket_id} for content hash {content_hash[:12]}...")
    return job


def get_job(ticket_id: str) -> Optional[Job]:
    _purge_expired()
    with _lock:
        job = _jobs.get(ticket_id)
    logger.debug(f"status lookup: {ticket_id} -> {'found' if job else 'not found'}")
    return job


def update(ticket_id: str, **fields: Any) -> None:
    with _lock:
        job = _jobs.get(ticket_id)
        if job is None:
            logger.warning(f"update on unknown ticket: {ticket_id}")
            return
        for key, value in fields.items():
            setattr(job, key, value)
        job.updated_at = time.time()
        snapshot = asdict(job)
    logger.info(f"ticket {ticket_id} updated: {fields.get('status', job.status)} - {fields.get('message', job.message)}")
    _save_to_cache(ticket_id, snapshot)
