"""Email (IMAP) intake for the async upload pipeline.

EmailPoller watches one IMAP folder. On its first poll after the server
starts it only records "the newest email right now" - existing mail is never
read. Every later poll returns just the emails that arrived since (from any
sender) that carry PDF attachments, so main.py can push those through the
normal OCR queue.

Each email is tracked until all its PDFs have finished. A PDF that fails is
re-submitted on later polls up to EMAIL_MAX_ATTEMPTS; once every PDF is
either completed or out of attempts, the email is marked read (or moved to
IMAP_PROCESSED_FOLDER) and each PDF's result becomes available to
collect_ready(), which hands every result out exactly once.

Results not yet collected are mirrored to a small JSON file (no DB, same
approach as jobs.py) so they survive a restart. Emails still being worked on
at restart are not resumed - their unfinished tickets are re-queued by
jobs.reconcile_pending(), but the email stays unread and is not re-scanned.
"""

import imaplib
import json
import logging
import os
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from email import message_from_bytes, policy
from typing import Optional

import jobs
from api import (
    IMAP_FOLDER,
    IMAP_HOST,
    IMAP_PASSWORD,
    IMAP_PORT,
    IMAP_PROCESSED_FOLDER,
    IMAP_TIMEOUT_SECONDS,
    IMAP_USE_SSL,
    IMAP_USERNAME,
    MAX_UPLOAD_BYTES,
)

logger = logging.getLogger(__name__)

MAILBOX_PATH = "mailbox_state.json"


@dataclass
class EmailPdf:
    filename: str
    data: bytes


@dataclass
class IncomingEmail:
    uid: int
    sender: str
    subject: str
    date: str
    pdfs: list[EmailPdf] = field(default_factory=list)


def _parse(uid: int, raw: bytes) -> IncomingEmail:
    msg = message_from_bytes(raw, policy=policy.default)
    parsed = IncomingEmail(
        uid=uid,
        sender=str(msg.get("from", "")),
        subject=str(msg.get("subject", "")),
        date=str(msg.get("date", "")),
    )
    for part in msg.walk():
        if part.is_multipart():
            continue
        filename = part.get_filename() or ""
        if not (filename.lower().endswith(".pdf") or part.get_content_type() == "application/pdf"):
            continue
        payload = part.get_payload(decode=True)
        if not payload or not payload.startswith(b"%PDF-"):
            continue
        if len(payload) > MAX_UPLOAD_BYTES:
            logger.warning(f"[email] skipped oversized attachment {filename!r} ({len(payload)} bytes)")
            continue
        parsed.pdfs.append(EmailPdf(filename=filename or "attachment.pdf", data=payload))
    return parsed


def _quote(folder: str) -> str:
    return '"' + folder.replace("\\", "\\\\").replace('"', '\\"') + '"'


@contextmanager
def _session(readonly: bool):
    """A fresh logged-in connection with IMAP_FOLDER selected. Reconnecting
    every poll is cheap at this frequency and means a dropped connection
    can't leave the watcher stuck."""
    if IMAP_USE_SSL:
        imap = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=IMAP_TIMEOUT_SECONDS)
    else:
        imap = imaplib.IMAP4(IMAP_HOST, IMAP_PORT, timeout=IMAP_TIMEOUT_SECONDS)
    try:
        imap.login(IMAP_USERNAME, IMAP_PASSWORD)
        typ, _ = imap.select(_quote(IMAP_FOLDER), readonly=readonly)
        if typ != "OK":
            raise RuntimeError(f"could not open folder {IMAP_FOLDER!r}")
        yield imap
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def _fetch(imap: imaplib.IMAP4, uid: int, part: str) -> Optional[bytes]:
    typ, data = imap.uid("FETCH", str(uid), f"(BODY.PEEK[{part}])")  # PEEK: never sets \Seen
    if typ != "OK" or not data or not isinstance(data[0], tuple):
        return None
    return data[0][1]


class EmailPoller:
    def __init__(self) -> None:
        self._last_uid: Optional[int] = None  # None until the first poll sets the baseline

    def fetch_new(self) -> list[IncomingEmail]:
        """Blocking - call from a thread. Returns emails with PDF attachments
        that arrived since the last poll (empty on the very first call, which
        only sets the baseline). Mail without a PDF is left untouched."""
        emails: list[IncomingEmail] = []
        with _session(readonly=True) as imap:
            if self._last_uid is None:
                typ, data = imap.status(_quote(IMAP_FOLDER), "(UIDNEXT)")
                match = re.search(rb"UIDNEXT (\d+)", data[0]) if typ == "OK" and data and data[0] else None
                if match is None:
                    raise RuntimeError(f"could not read UIDNEXT for folder {IMAP_FOLDER!r}")
                self._last_uid = int(match.group(1)) - 1
                logger.info(f"[email] watching {IMAP_FOLDER} from now on (baseline uid={self._last_uid}); existing mail is not read")
                return emails

            typ, data = imap.uid("SEARCH", None, f"UID {self._last_uid + 1}:*")
            if typ != "OK":
                raise RuntimeError("IMAP search failed")
            # "N:*" always includes the newest message even when its uid < N
            uids = sorted(uid for uid in map(int, data[0].split()) if uid > self._last_uid)

            for uid in uids:
                try:
                    raw = _fetch(imap, uid, "")
                    if raw is None:
                        raise imaplib.IMAP4.error("fetch failed")
                except (imaplib.IMAP4.error, OSError):
                    logger.exception(f"[email] fetch failed for uid {uid}, will retry next poll")
                    break  # keep what we have so far; this uid is retried next poll
                try:
                    parsed = _parse(uid, raw)
                    if parsed.pdfs:
                        emails.append(parsed)
                except Exception:
                    logger.exception(f"[email] could not parse uid {uid}, skipping it")
                self._last_uid = uid
        return emails

    def refetch(self, uid: int) -> Optional[IncomingEmail]:
        """Re-download one email (to retry a failed PDF)."""
        with _session(readonly=True) as imap:
            raw = _fetch(imap, uid, "")
        return _parse(uid, raw) if raw is not None else None

    def mark_done(self, uid: int) -> None:
        """Mark the email read, and move it to IMAP_PROCESSED_FOLDER if set."""
        with _session(readonly=False) as imap:
            imap.uid("STORE", str(uid), "+FLAGS", "(\\Seen)")
            if IMAP_PROCESSED_FOLDER:
                typ, _ = imap.uid("COPY", str(uid), _quote(IMAP_PROCESSED_FOLDER))
                if typ != "OK":
                    logger.warning(f"[email] could not copy uid {uid} to {IMAP_PROCESSED_FOLDER!r}; left it in {IMAP_FOLDER}")
                    return
                imap.uid("STORE", str(uid), "+FLAGS", "(\\Deleted)")
                imap.uid("EXPUNGE", str(uid))  # UID EXPUNGE: only this message, not others flagged \Deleted
        logger.info(f"[email] marked uid {uid} done")


# ---- emails in flight: one slot per PDF until it completes or runs out of attempts ----

@dataclass
class Slot:
    filename: str
    content_hash: str
    ticket_id: str
    attempts: int = 1
    final: bool = False


@dataclass
class Tracked:
    email: IncomingEmail  # pdfs cleared - only the details are kept, not the bytes
    slots: list[Slot]


_lock = threading.Lock()
_tracked: list[Tracked] = []
_entries: dict[str, dict] = {}  # ticket_id -> {email details..., "delivered": bool}


def track(email_info: IncomingEmail, slots: list[Slot]) -> None:
    email_info.pdfs = []
    with _lock:
        _tracked.append(Tracked(email=email_info, slots=slots))


def tracked_emails() -> list[Tracked]:
    with _lock:
        return list(_tracked)


def untrack(tracked: Tracked) -> None:
    with _lock:
        _tracked.remove(tracked)


def finalize(tracked: Tracked, slot: Slot) -> None:
    """This PDF is finished (completed, or failed with no attempts left):
    make its result available to collect_ready(). A no-op registration if the
    same PDF (same ticket) was already recorded, so duplicates are never
    handed out twice."""
    with _lock:
        slot.final = True
        if slot.ticket_id in _entries:
            return
        _entries[slot.ticket_id] = {
            "ticket_id": slot.ticket_id,
            "from": tracked.email.sender,
            "subject": tracked.email.subject,
            "date": tracked.email.date,
            "filename": slot.filename,
            "received_at": time.time(),
            "delivered": False,
        }
        _save()


# ---- persistence of finished-but-not-yet-collected results ----

def _save() -> None:
    tmp_path = f"{MAILBOX_PATH}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(list(_entries.values()), f)
        os.replace(tmp_path, MAILBOX_PATH)
    except OSError:
        logger.warning(f"failed to write {MAILBOX_PATH}")


def _load() -> None:
    if not os.path.isfile(MAILBOX_PATH):
        return
    try:
        with open(MAILBOX_PATH, "r", encoding="utf-8") as f:
            for entry in json.load(f):
                _entries[entry["ticket_id"]] = entry
    except (OSError, ValueError, KeyError, TypeError) as e:
        logger.warning(f"ignoring unreadable {MAILBOX_PATH}: {type(e).__name__}: {e}")
    logger.info(f"loaded {len(_entries)} email ticket(s) from {MAILBOX_PATH}")


_load()


def collect_ready() -> dict:
    """Every finished PDF result not handed out yet, in arrival order - each
    ticket is returned exactly once."""
    items = []
    with _lock:
        changed = False
        for ticket_id, entry in list(_entries.items()):
            job = jobs.get_job(ticket_id)
            if job is None:  # ticket expired out of jobs' cache - forget it too
                del _entries[ticket_id]
                changed = True
                continue
            if entry["delivered"]:
                continue
            items.append({
                "ticket_id": ticket_id,
                "email": {k: entry[k] for k in ("from", "subject", "date", "filename")},
                "status": job.status,
                "result": job.result,
                "error": job.error,
            })
            entry["delivered"] = True
            changed = True
        if changed:
            _save()
        still_processing = sum(1 for t in _tracked for s in t.slots if not s.final)
    return {"count": len(items), "still_processing": still_processing, "items": items}
