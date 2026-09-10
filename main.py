import asyncio
import glob
import hashlib
import json
import os
import time
import uuid
from contextlib import asynccontextmanager
from tempfile import NamedTemporaryFile
from fastapi import FastAPI, HTTPException, Request
from api import MAX_UPLOAD_BYTES, PDF_WORKER_COUNT, UPLOAD_QUEUE_MAXSIZE
from model import (
    INVOICE_DIR,
    OUTPUT_PATH,
    executor,
    group_into_invoices,
    is_blank_invoice,
    is_blank_result,
    logger,
    pdf_to_images,
    process_page,
)
import jobs  # imported after model so logging.basicConfig (in model) is
             # already configured before jobs._load_cache() logs at import time

# Bounded queue that /upload feeds and a fixed pool of worker tasks drains -
# caps how many PDFs are being rendered/OCR'd at once (PDF_WORKER_COUNT)
# instead of spawning one background task per upload with no limit, which
# would let e.g. 100 near-simultaneous uploads all start rendering (memory-
# heavy) at once. Page-level OCR calls within an in-progress PDF still share
# the MAX_WORKERS-sized `executor` from model.py regardless of this queue.
#
# The queue carries only ticket IDs, not PDF bytes - /upload streams each
# upload straight to jobs.pending_path(ticket_id) on disk as it arrives, so
# queue depth (even at UPLOAD_QUEUE_MAXSIZE=2000) costs a few KB of ticket
# bookkeeping rather than gigabytes of buffered PDF content in memory.
_upload_queue: "asyncio.Queue[str]" = asyncio.Queue(maxsize=UPLOAD_QUEUE_MAXSIZE)


async def _upload_worker(worker_id: int) -> None:
    while True:
        ticket_id = await _upload_queue.get()
        try:
            logger.info(f"[worker {worker_id}] picked up ticket: {ticket_id}")
            await _process_pdf_job(ticket_id)
        except Exception:
            logger.exception(f"[worker {worker_id}] unhandled error processing ticket: {ticket_id}")
        finally:
            _upload_queue.task_done()


@asynccontextmanager
async def lifespan(app: FastAPI):
    workers = [asyncio.create_task(_upload_worker(i)) for i in range(PDF_WORKER_COUNT)]
    logger.info(f"started {PDF_WORKER_COUNT} upload worker(s)")

    # Re-queue tickets left in-flight by a previous crash/restart (their PDF
    # is still sitting in jobs.PENDING_DIR), so a redeploy under load doesn't
    # silently strand part of a large batch.
    for ticket_id in jobs.reconcile_pending():
        try:
            _upload_queue.put_nowait(ticket_id)
        except asyncio.QueueFull:
            jobs.update(ticket_id, status=jobs.FAILED, message="Processing failed", error="Server restarted with a full queue - please resubmit")

    yield
    for worker in workers:
        worker.cancel()
    logger.info("stopped upload worker(s)")


app = FastAPI(title="Receipt OCR API", lifespan=lifespan)

@app.get("/")
def root():
    return {
        "message": "Receipt OCR API is running. See /docs for interactive testing.",
        "endpoints": ["/health", "POST /extract"],
    }


@app.get("/health")
def health():
    return {"status": "Sucess"}

async def _process_pdf(pdf_bytes: bytes):
    request_started = time.monotonic()
    with NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(pdf_bytes)
        tmp_path = tmp.name
    source_id = os.path.basename(tmp_path)
    logger.info(f"[extract] saved upload to temp file: {source_id} ({len(pdf_bytes)} bytes)")

    try:
        loop = asyncio.get_running_loop()
        stage_started = time.monotonic()
        logger.info(f"[extract] rendering PDF pages: {source_id}")
        rendered = await loop.run_in_executor(
            None, lambda: list(enumerate(pdf_to_images(tmp_path), start=1))
        )
        render_elapsed = time.monotonic() - stage_started
        logger.info(f"[extract] rendered {len(rendered)} page(s): {source_id} [{render_elapsed:.2f}s]")
        pages = []
        for page_num, (image_bytes, blank) in rendered:
            if blank:
                logger.info(f"skipped (blank page, no OCR call): {source_id}#page{page_num}")
                continue
            pages.append((page_num, image_bytes))

        stage_started = time.monotonic()
        logger.info(f"[extract] dispatching OCR for {len(pages)} page(s): {source_id}")
        futures = [
            loop.run_in_executor(executor, process_page, tmp_path, page_num, image_bytes)
            for page_num, image_bytes in pages
        ]

        results = []
        for future in asyncio.as_completed(futures):
            key, result = await future
            if isinstance(result, dict):  # process_page's {"error": ...} sentinel
                logger.info(f"skipped (failed): {key}")
                continue
            kept = [invoice for invoice in result if not is_blank_result(invoice)]
            if not kept:
                logger.info(f"skipped (blank): {key}")
                continue
            results.extend((source_id, invoice) for invoice in kept)
            logger.info(f"done: {key} ({len(kept)} invoice(s))")
        ocr_elapsed = time.monotonic() - stage_started
        logger.info(f"[extract] OCR complete, {len(results)} invoice page result(s): {source_id} [{ocr_elapsed:.2f}s]")
    finally:
        os.remove(tmp_path)
        logger.info(f"[extract] removed temp file: {source_id}")

    stage_started = time.monotonic()
    logger.info(f"[extract] grouping pages into invoices: {source_id}")
    invoices = [inv for inv in group_into_invoices(results) if not is_blank_invoice(inv)]
    output = [invoice.to_dict() for invoice in invoices]
    grouping_elapsed = time.monotonic() - stage_started
    total_elapsed = time.monotonic() - request_started
    logger.info(
        f"[extract] finished: {source_id} -> {len(output)} invoice(s) returned "
        f"[render: {render_elapsed:.2f}s, ocr: {ocr_elapsed:.2f}s, grouping: {grouping_elapsed:.2f}s, total: {total_elapsed:.2f}s]"
    )
    return output



@app.post("/extract")
async def extract_binary(request: Request):
    """Upload a single PDF (Postman: Body > binary) and get its extracted JSON back."""
    pdf_bytes = await request.body()
    logger.info(f"[extract] upload received: {len(pdf_bytes)} bytes")
    if not pdf_bytes:
        logger.warning("[extract] rejected upload: empty request body")
        raise HTTPException(status_code=400, detail="Empty request body")
    if not pdf_bytes.startswith(b"%PDF-"):
        logger.warning("[extract] rejected upload: not a PDF")
        raise HTTPException(status_code=400, detail="Only PDF files are supported")

    output = await _process_pdf(pdf_bytes)
    logger.info(f"[extract] response sent: {len(output)} invoice(s)")
    return output


async def _process_pdf_job(ticket_id: str) -> None:
    """Background counterpart to _process_pdf() that reports progress onto
    the ticket instead of returning the result directly - runs after the
    /upload response has already been sent to the client. Reads the PDF
    that /upload already streamed to jobs.pending_path(ticket_id)."""
    job_started = time.monotonic()
    tmp_path = jobs.pending_path(ticket_id)
    source_id = os.path.basename(tmp_path)
    if not os.path.isfile(tmp_path):
        logger.error(f"[job {ticket_id}] pending file missing, cannot process: {tmp_path}")
        jobs.update(ticket_id, status=jobs.FAILED, message="Processing failed", error="Uploaded file was missing when processing started")
        return

    try:
        jobs.update(ticket_id, status=jobs.OCR_PROCESSING, progress=10, message="Running OCR on the document...")

        loop = asyncio.get_running_loop()
        stage_started = time.monotonic()
        logger.info(f"[job {ticket_id}] rendering PDF pages: {source_id}")
        rendered = await loop.run_in_executor(
            None, lambda: list(enumerate(pdf_to_images(tmp_path), start=1))
        )
        render_elapsed = time.monotonic() - stage_started
        logger.info(f"[job {ticket_id}] rendered {len(rendered)} page(s): {source_id} [{render_elapsed:.2f}s]")
        pages = []
        for page_num, (image_bytes, blank) in rendered:
            if blank:
                logger.info(f"skipped (blank page, no OCR call): {source_id}#page{page_num}")
                continue
            pages.append((page_num, image_bytes))

        total_pages = len(pages)
        jobs.update(ticket_id, total_pages=total_pages)
        logger.info(f"[job {ticket_id}] dispatching OCR for {total_pages} page(s): {source_id}")

        stage_started = time.monotonic()
        futures = [
            loop.run_in_executor(executor, process_page, tmp_path, page_num, image_bytes)
            for page_num, image_bytes in pages
        ]

        results = []
        completed = 0
        for future in asyncio.as_completed(futures):
            key, result = await future
            completed += 1
            page_elapsed = time.monotonic() - stage_started
            if total_pages:
                jobs.update(
                    ticket_id,
                    progress=10 + int(70 * completed / total_pages),
                    message=f"OCR processing: {completed}/{total_pages} page(s)",
                )
            if isinstance(result, dict):  # process_page's {"error": ...} sentinel
                logger.info(f"skipped (failed): {key} [{page_elapsed:.2f}s since OCR dispatch]")
                continue
            kept = [invoice for invoice in result if not is_blank_result(invoice)]
            if not kept:
                logger.info(f"skipped (blank): {key} [{page_elapsed:.2f}s since OCR dispatch]")
                continue
            results.extend((source_id, invoice) for invoice in kept)
            logger.info(f"done: {key} ({len(kept)} invoice(s)) [{page_elapsed:.2f}s since OCR dispatch]")
        ocr_elapsed = time.monotonic() - stage_started
        logger.info(f"[job {ticket_id}] OCR complete, {len(results)} invoice page result(s): {source_id} [{ocr_elapsed:.2f}s]")

        jobs.update(ticket_id, status=jobs.VALIDATING, progress=90, message="Validating and grouping extracted invoices...")
        stage_started = time.monotonic()
        logger.info(f"[job {ticket_id}] grouping pages into invoices: {source_id}")
        invoices = [inv for inv in group_into_invoices(results) if not is_blank_invoice(inv)]
        output = [invoice.to_dict() for invoice in invoices]
        grouping_elapsed = time.monotonic() - stage_started

        jobs.update(
            ticket_id,
            status=jobs.COMPLETED,
            progress=100,
            message="Invoice processed successfully",
            result=output,
        )
        total_elapsed = time.monotonic() - job_started
        logger.info(
            f"[job {ticket_id}] completed: {source_id} -> {len(output)} invoice(s) "
            f"[render: {render_elapsed:.2f}s, ocr: {ocr_elapsed:.2f}s, grouping: {grouping_elapsed:.2f}s, total: {total_elapsed:.2f}s]"
        )
    except Exception as e:
        logger.exception(f"[job {ticket_id}] failed: {source_id} [{time.monotonic() - job_started:.2f}s since job start]")
        jobs.update(
            ticket_id,
            status=jobs.FAILED,
            message="Processing failed",
            error=f"{type(e).__name__}: {e}",
        )
    finally:
        jobs.safe_remove(tmp_path)
        logger.info(f"[job {ticket_id}] removed pending file: {source_id}")


@app.post("/upload")
async def upload_pdf(request: Request):
    """Accept a PDF (Postman: Body > binary), immediately return a ticket ID,
    and run the actual OCR/extraction on a bounded worker pool. Poll GET
    /status/{ticket_id} for progress and the final result. Uploads past
    PDF_WORKER_COUNT (api.py) queue up rather than all processing at once;
    once UPLOAD_QUEUE_MAXSIZE tickets are already queued, new uploads get a
    503 instead of piling up unboundedly.

    The body is streamed straight to disk (jobs.pending_path) instead of
    being buffered in memory - with many uploads in flight at once (e.g. a
    1000-PDF batch), holding every PDF's bytes in RAM until a worker picks
    it up would risk exhausting memory well before the queue itself is full."""
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > MAX_UPLOAD_BYTES:
                raise HTTPException(status_code=413, detail=f"PDF exceeds the {MAX_UPLOAD_BYTES // (1024*1024)}MB upload limit")
        except ValueError:
            pass

    os.makedirs(jobs.PENDING_DIR, exist_ok=True)
    tmp_path = os.path.join(jobs.PENDING_DIR, f"upload-{uuid.uuid4().hex}.pdf")
    hasher = hashlib.sha256()
    header = b""
    size = 0
    try:
        with open(tmp_path, "wb") as f:
            async for chunk in request.stream():
                if not chunk:
                    continue
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail=f"PDF exceeds the {MAX_UPLOAD_BYTES // (1024*1024)}MB upload limit")
                if len(header) < 5:
                    header += chunk[: 5 - len(header)]
                hasher.update(chunk)
                f.write(chunk)
    except HTTPException:
        jobs.safe_remove(tmp_path)
        logger.warning(f"[upload] rejected upload: exceeded {MAX_UPLOAD_BYTES} byte limit")
        raise
    except Exception:
        jobs.safe_remove(tmp_path)
        logger.exception("[upload] failed while receiving upload")
        raise HTTPException(status_code=500, detail="Failed to receive upload")

    logger.info(f"[upload] upload received: {size} bytes")
    if size == 0:
        jobs.safe_remove(tmp_path)
        logger.warning("[upload] rejected upload: empty request body")
        raise HTTPException(status_code=400, detail="Empty request body")
    if not header.startswith(b"%PDF-"):
        jobs.safe_remove(tmp_path)
        logger.warning("[upload] rejected upload: not a PDF")
        raise HTTPException(status_code=400, detail="Only PDF files are supported")

    content_hash = hasher.hexdigest()
    logger.info(f"[upload] content hash: {content_hash[:12]}...")
    existing = jobs.find_existing(content_hash)
    if existing is not None:
        jobs.safe_remove(tmp_path)
        logger.info(f"[upload] deduped to existing ticket: {existing.ticket_id}")
        return {
            "success": True,
            "ticket_id": existing.ticket_id,
            "status": existing.status,
            "message": "This PDF was already submitted - returning its existing ticket.",
        }

    if _upload_queue.full():
        jobs.safe_remove(tmp_path)
        logger.warning(f"[upload] rejected upload: queue full ({_upload_queue.qsize()} ticket(s) waiting)")
        raise HTTPException(status_code=503, detail="Server is busy processing other PDFs - please retry shortly")

    job = jobs.create_job(content_hash)
    os.replace(tmp_path, jobs.pending_path(job.ticket_id))
    try:
        _upload_queue.put_nowait(job.ticket_id)
    except asyncio.QueueFull:
        jobs.update(job.ticket_id, status=jobs.FAILED, message="Queue full", error="Server is busy processing other PDFs")
        jobs.safe_remove(jobs.pending_path(job.ticket_id))
        logger.warning(f"[upload] rejected upload: queue full at put time: {job.ticket_id}")
        raise HTTPException(status_code=503, detail="Server is busy processing other PDFs - please retry shortly")
    logger.info(f"[upload] queued ticket: {job.ticket_id} (queue depth: {_upload_queue.qsize()})")

    return {
        "success": True,
        "ticket_id": job.ticket_id,
        "status": job.status,
        "message": job.message,
    }


@app.get("/status/{ticket_id}")
def get_status(ticket_id: str):
    logger.info(f"[status] poll: {ticket_id}")
    job = jobs.get_job(ticket_id)
    if job is None:
        logger.warning(f"[status] unknown ticket: {ticket_id}")
        raise HTTPException(status_code=404, detail="Unknown ticket_id")
    logger.info(f"[status] {ticket_id} -> {job.status} ({job.progress}%)")
    return job.to_dict()
