import asyncio
import glob
import json
import os
from tempfile import NamedTemporaryFile
from fastapi import BackgroundTasks, FastAPI, File, HTTPException, Request, UploadFile
import jobs
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

    try:
        loop = asyncio.get_running_loop()
        rendered = await loop.run_in_executor(
            None, lambda: list(enumerate(pdf_to_images(tmp_path), start=1))
        )
        pages = []
        for page_num, (image_bytes, blank) in rendered:
            if blank:
                logger.info(f"skipped (blank page, no OCR call): {os.path.basename(tmp_path)}#page{page_num}")
                continue
            pages.append((page_num, image_bytes))


        futures = [
            loop.run_in_executor(executor, process_page, tmp_path, page_num, image_bytes)
            for page_num, image_bytes in pages
        ]

        source_id = os.path.basename(tmp_path)
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
    finally:
        os.remove(tmp_path)

    invoices = [inv for inv in group_into_invoices(results) if not is_blank_invoice(inv)]
    output = [invoice.to_dict() for invoice in invoices]
    return output



@app.post("/extract")
async def extract_binary(request: Request):
    """Upload a single PDF (Postman: Body > binary) and get its extracted JSON back."""
    pdf_bytes = await request.body()
    if not pdf_bytes:
        raise HTTPException(status_code=400, detail="Empty request body")
    if not pdf_bytes.startswith(b"%PDF-"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported")

    return await _process_pdf(pdf_bytes)


async def _process_pdf_job(ticket_id: str, pdf_bytes: bytes) -> None:
    """Background counterpart to _process_pdf() that reports progress onto
    the ticket instead of returning the result directly - runs after the
    /upload response has already been sent to the client."""
    with NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(pdf_bytes)
        tmp_path = tmp.name

    try:
        jobs.update(ticket_id, status=jobs.OCR_PROCESSING, progress=10, message="Running OCR on the document...")

        loop = asyncio.get_running_loop()
        rendered = await loop.run_in_executor(
            None, lambda: list(enumerate(pdf_to_images(tmp_path), start=1))
        )
        pages = []
        for page_num, (image_bytes, blank) in rendered:
            if blank:
                logger.info(f"skipped (blank page, no OCR call): {os.path.basename(tmp_path)}#page{page_num}")
                continue
            pages.append((page_num, image_bytes))

        total_pages = len(pages)
        jobs.update(ticket_id, total_pages=total_pages)

        futures = [
            loop.run_in_executor(executor, process_page, tmp_path, page_num, image_bytes)
            for page_num, image_bytes in pages
        ]

        source_id = os.path.basename(tmp_path)
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

        jobs.update(ticket_id, status=jobs.VALIDATING, progress=90, message="Validating and grouping extracted invoices...")
        invoices = [inv for inv in group_into_invoices(results) if not is_blank_invoice(inv)]
        output = [invoice.to_dict() for invoice in invoices]

        jobs.update(
            ticket_id,
            status=jobs.COMPLETED,
            progress=100,
            message="Invoice processed successfully",
            result=output,
        )
    except Exception as e:
        logger.exception(f"job failed: {ticket_id}")
        jobs.update(
            ticket_id,
            status=jobs.FAILED,
            message="Processing failed",
            error=f"{type(e).__name__}: {e}",
        )
    finally:
        os.remove(tmp_path)


@app.post("/upload")
async def upload_pdf(request: Request, background_tasks: BackgroundTasks):
    """Accept a PDF (Postman: Body > binary), immediately return a ticket ID,
    and run the actual OCR/extraction in the background. Poll GET /status/{ticket_id}
    for progress and the final result."""
    pdf_bytes = await request.body()
    if not pdf_bytes:
        raise HTTPException(status_code=400, detail="Empty request body")
    if not pdf_bytes.startswith(b"%PDF-"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported")

    content_hash = jobs.hash_pdf(pdf_bytes)
    existing = jobs.find_existing(content_hash)
    if existing is not None:
        return {
            "success": True,
            "ticket_id": existing.ticket_id,
            "status": existing.status,
            "message": "This PDF was already submitted - returning its existing ticket.",
        }

    job = jobs.create_job(content_hash)
    background_tasks.add_task(_process_pdf_job, job.ticket_id, pdf_bytes)

    return {
        "success": True,
        "ticket_id": job.ticket_id,
        "status": job.status,
        "message": job.message,
    }


@app.get("/status/{ticket_id}")
def get_status(ticket_id: str):
    job = jobs.get_job(ticket_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown ticket_id")
    return job.to_dict()
