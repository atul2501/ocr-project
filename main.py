import asyncio
import glob
import json
import os
from tempfile import NamedTemporaryFile
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from model import (
    INVOICE_DIR,
    OUTPUT_PATH,
    executor,
    has_required_identifiers,
    is_target_document,
    logger,
    pdf_to_images,
    process_page,
)

app = FastAPI(title="Receipt OCR API")

@app.get("/")
def root():
    return {
        "message": "Receipt OCR API is running. See /docs for interactive testing.",
        "endpoints": ["/health", "POST /extract", "POST /extract/binary"],
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
        pages = await loop.run_in_executor(
            None, lambda: list(enumerate(pdf_to_images(tmp_path), start=1))
        )

        # Submit to the shared process-wide executor (main.executor, capped
        # at MAX_WORKERS) and await the futures instead of blocking on them,
        # so extra pages/requests queue behind the cap without freezing the
        # FastAPI event loop for other concurrent uploads.
        futures = [
            loop.run_in_executor(executor, process_page, tmp_path, page_num, image_bytes)
            for page_num, image_bytes in pages
        ]

        results = []
        for future in asyncio.as_completed(futures):
            key, result = await future
            if not is_target_document(result):
                logger.info(f"skipped (not tax invoice/PO): {key}")
                continue
            if not has_required_identifiers(result):
                logger.info(f"skipped (missing invoice number and document type): {key}")
                continue
            results.append(result)
            logger.info(f"done: {key}")
    finally:
        os.remove(tmp_path)

    return results



@app.post("/extract")
async def extract_binary(request: Request):
    """Upload a single PDF (Postman: Body > binary) and get its extracted JSON back."""
    pdf_bytes = await request.body()
    if not pdf_bytes:
        raise HTTPException(status_code=400, detail="Empty request body")
    if not pdf_bytes.startswith(b"%PDF-"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported")

    return await _process_pdf(pdf_bytes)