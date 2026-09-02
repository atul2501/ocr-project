"""In-memory ticket/status store for the async upload pipeline.

No database by design (per demo requirements) - state lives only for the
life of the process. A PDF is deduplicated by content hash, not filename,
so resubmitting the same file returns the existing ticket instead of
kicking off a second background job.
"""

import hashlib
import threading
import time
import uuid
from dataclasses import dataclass, field
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


_lock = threading.Lock()
_jobs: dict[str, Job] = {}
_hash_to_ticket: dict[str, str] = {}


def hash_pdf(pdf_bytes: bytes) -> str:
    return hashlib.sha256(pdf_bytes).hexdigest()


def find_existing(content_hash: str) -> Optional[Job]:
    """Return the in-flight or completed job for this exact PDF, if any.

    A FAILED job is not returned here, so resubmitting the same PDF after
    a failure starts a fresh job/ticket rather than being stuck dedup'd
    onto the failed one.
    """
    with _lock:
        ticket_id = _hash_to_ticket.get(content_hash)
        if ticket_id is None:
            return None
        job = _jobs.get(ticket_id)
        if job is not None and job.status != FAILED:
            return job
        return None


def create_job(content_hash: str) -> Job:
    ticket_id = f"TCK-{uuid.uuid4().hex[:12]}"
    job = Job(ticket_id=ticket_id, content_hash=content_hash)
    with _lock:
        _jobs[ticket_id] = job
        _hash_to_ticket[content_hash] = ticket_id
    return job


def get_job(ticket_id: str) -> Optional[Job]:
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
