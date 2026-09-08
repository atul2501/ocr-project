import asyncio
import glob
import json
import os
from tempfile import NamedTemporaryFile
from fastapi import BackgroundTasks, FastAPI, File, HTTPException, Request, UploadFile
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

app = FastAPI(title="Receipt OCR API")

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
    with NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(pdf_bytes)
        tmp_path = tmp.name
    source_id = os.path.basename(tmp_path)
    logger.info(f"[extract] saved upload to temp file: {source_id} ({len(pdf_bytes)} bytes)")

    try:
        loop = asyncio.get_running_loop()
        logger.info(f"[extract] rendering PDF pages: {source_id}")
        rendered = await loop.run_in_executor(
            None, lambda: list(enumerate(pdf_to_images(tmp_path), start=1))
        )
        logger.info(f"[extract] rendered {len(rendered)} page(s): {source_id}")
        pages = []
        for page_num, (image_bytes, blank) in rendered:
            if blank:
                logger.info(f"skipped (blank page, no OCR call): {source_id}#page{page_num}")
                continue
            pages.append((page_num, image_bytes))

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
        logger.info(f"[extract] OCR complete, {len(results)} invoice page result(s): {source_id}")
    finally:
        os.remove(tmp_path)
        logger.info(f"[extract] removed temp file: {source_id}")

    logger.info(f"[extract] grouping pages into invoices: {source_id}")
    invoices = [inv for inv in group_into_invoices(results) if not is_blank_invoice(inv)]
    output = [invoice.to_dict() for invoice in invoices]
    logger.info(f"[extract] finished: {source_id} -> {len(output)} invoice(s) returned")
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


async def _process_pdf_job(ticket_id: str, pdf_bytes: bytes) -> None:
    """Background counterpart to _process_pdf() that reports progress onto
    the ticket instead of returning the result directly - runs after the
    /upload response has already been sent to the client."""
    with NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(pdf_bytes)
        tmp_path = tmp.name
    source_id = os.path.basename(tmp_path)
    logger.info(f"[job {ticket_id}] saved upload to temp file: {source_id} ({len(pdf_bytes)} bytes)")

    try:
        jobs.update(ticket_id, status=jobs.OCR_PROCESSING, progress=10, message="Running OCR on the document...")

        loop = asyncio.get_running_loop()
        logger.info(f"[job {ticket_id}] rendering PDF pages: {source_id}")
        rendered = await loop.run_in_executor(
            None, lambda: list(enumerate(pdf_to_images(tmp_path), start=1))
        )
        logger.info(f"[job {ticket_id}] rendered {len(rendered)} page(s): {source_id}")
        pages = []
        for page_num, (image_bytes, blank) in rendered:
            if blank:
                logger.info(f"skipped (blank page, no OCR call): {source_id}#page{page_num}")
                continue
            pages.append((page_num, image_bytes))

        total_pages = len(pages)
        jobs.update(ticket_id, total_pages=total_pages)
        logger.info(f"[job {ticket_id}] dispatching OCR for {total_pages} page(s): {source_id}")

        futures = [
            loop.run_in_executor(executor, process_page, tmp_path, page_num, image_bytes)
            for page_num, image_bytes in pages
        ]

        results = []
        completed = 0
        for future in asyncio.as_completed(futures):
            key, result = await future
            completed += 1
            if total_pages:
                jobs.update(
                    ticket_id,
                    progress=10 + int(70 * completed / total_pages),
                    message=f"OCR processing: {completed}/{total_pages} page(s)",
                )
            if isinstance(result, dict):  # process_page's {"error": ...} sentinel
                logger.info(f"skipped (failed): {key}")
                continue
            kept = [invoice for invoice in result if not is_blank_result(invoice)]
            if not kept:
                logger.info(f"skipped (blank): {key}")
                continue
            results.extend((source_id, invoice) for invoice in kept)
            logger.info(f"done: {key} ({len(kept)} invoice(s))")
        logger.info(f"[job {ticket_id}] OCR complete, {len(results)} invoice page result(s): {source_id}")

        jobs.update(ticket_id, status=jobs.VALIDATING, progress=90, message="Validating and grouping extracted invoices...")
        logger.info(f"[job {ticket_id}] grouping pages into invoices: {source_id}")
        invoices = [inv for inv in group_into_invoices(results) if not is_blank_invoice(inv)]
        output = [invoice.to_dict() for invoice in invoices]

        jobs.update(
            ticket_id,
            status=jobs.COMPLETED,
            progress=100,
            message="Invoice processed successfully",
            result=output,
        )
        logger.info(f"[job {ticket_id}] completed: {source_id} -> {len(output)} invoice(s)")
    except Exception as e:
        logger.exception(f"[job {ticket_id}] failed: {source_id}")
        jobs.update(
            ticket_id,
            status=jobs.FAILED,
            message="Processing failed",
            error=f"{type(e).__name__}: {e}",
        )
    finally:
        os.remove(tmp_path)
        logger.info(f"[job {ticket_id}] removed temp file: {source_id}")


@app.post("/upload")
async def upload_pdf(request: Request, background_tasks: BackgroundTasks):
    """Accept a PDF (Postman: Body > binary), immediately return a ticket ID,
    and run the actual OCR/extraction in the background. Poll GET /status/{ticket_id}
    for progress and the final result."""
    pdf_bytes = await request.body()
    logger.info(f"[upload] upload received: {len(pdf_bytes)} bytes")
    if not pdf_bytes:
        logger.warning("[upload] rejected upload: empty request body")
        raise HTTPException(status_code=400, detail="Empty request body")
    if not pdf_bytes.startswith(b"%PDF-"):
        logger.warning("[upload] rejected upload: not a PDF")
        raise HTTPException(status_code=400, detail="Only PDF files are supported")

    content_hash = jobs.hash_pdf(pdf_bytes)
    logger.info(f"[upload] content hash: {content_hash[:12]}...")
    existing = jobs.find_existing(content_hash)
    if existing is not None:
        logger.info(f"[upload] deduped to existing ticket: {existing.ticket_id}")
        return {
            "success": True,
            "ticket_id": existing.ticket_id,
            "status": existing.status,
            "message": "This PDF was already submitted - returning its existing ticket.",
        }

    job = jobs.create_job(content_hash)
    background_tasks.add_task(_process_pdf_job, job.ticket_id, pdf_bytes)
    logger.info(f"[upload] queued background job for ticket: {job.ticket_id}")

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
