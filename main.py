import asyncio
import glob
import json
import os
from tempfile import NamedTemporaryFile
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from model import (
    INVOICE_DIR,
    OUTPUT_PATH,
    cleanup_old_output,
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
        # Rendering/sharpening is quick CPU work; run it off the event loop
        # on the default executor so it doesn't steal an OCR queue slot.
        rendered = await loop.run_in_executor(
            None, lambda: list(enumerate(pdf_to_images(tmp_path), start=1))
        )
        pages = []
        for page_num, (image_bytes, blank) in rendered:
            if blank:
                # A blank/near-blank page sent to the model anyway has been
                # observed to make it hallucinate a whole fabricated invoice
                # rather than correctly report no content - skip the OCR
                # call entirely instead of risking that (see model.py:
                # _is_blank_page).
                logger.info(f"skipped (blank page, no OCR call): {os.path.basename(tmp_path)}#page{page_num}")
                continue
            pages.append((page_num, image_bytes))

        # Submit to the shared process-wide executor (main.executor, capped
        # at MAX_WORKERS) and await the futures instead of blocking on them,
        # so extra pages/requests queue behind the cap without freezing the
        # FastAPI event loop for other concurrent uploads.
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
    cleanup_old_output()
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