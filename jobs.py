"""Ticket/status store for the async upload pipeline.

No database by design (per demo requirements) - each ticket is instead
mirrored to a small JSON file under CACHE_DIR, so tickets survive a
process restart without needing a real DB. A PDF is deduplicated by
content hash, not filename, so resubmitting the same file returns the
existing ticket instead of kicking off a second background job.
"""

import hashlib
import json
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

RECEIVED = "PROCESSING"
OCR_PROCESSING = "OCR_PROCESSING"
VALIDATING = "VALIDATING"
COMPLETED = "COMPLETED"
FAILED = "FAILED"

_TERMINAL = {COMPLETED}  # FAILED is retryable, so it's not terminal for dedup purposes


@dataclass
class Job:
    ticket_id: str
    content_hash: str
    status: str = RECEIVED
    progress: int = 5
    message: str = "PDF received successfully. Ticket created. Processing started."
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

_lock = threading.Lock()
_jobs: dict[str, Job] = {}
_hash_to_ticket: dict[str, str] = {}


def _cache_path(ticket_id: str) -> str:
    return os.path.join(CACHE_DIR, f"{ticket_id}.json")


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
    except OSError:
        pass


def _load_cache() -> None:
    """Rebuild _jobs/_hash_to_ticket from cache/*.json on startup, dropping
    (and deleting) anything already past CACHE_TTL_SECONDS."""
    if not os.path.isdir(CACHE_DIR):
        return
    for name in os.listdir(CACHE_DIR):
        if not name.endswith(".json"):
            continue
        path = os.path.join(CACHE_DIR, name)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            job = Job(**data)
        except (OSError, ValueError, TypeError):
            continue
        if _is_expired(job):
            try:
                os.remove(path)
            except OSError:
                pass
            continue
        _jobs[job.ticket_id] = job
        _hash_to_ticket[job.content_hash] = job.ticket_id


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
            return None
        job = _jobs.get(ticket_id)
        if job is not None and job.status != FAILED:
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
    return job


def get_job(ticket_id: str) -> Optional[Job]:
    _purge_expired()
    with _lock:
        return _jobs.get(ticket_id)


def update(ticket_id: str, **fields: Any) -> None:
    with _lock:
        job = _jobs.get(ticket_id)
        if job is None:
            return
        for key, value in fields.items():
            setattr(job, key, value)
        job.updated_at = time.time()
        snapshot = asdict(job)
    _save_to_cache(ticket_id, snapshot)
