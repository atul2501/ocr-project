import glob
import json
import os
from concurrent.futures import ThreadPoolExecutor
from tempfile import NamedTemporaryFile

from fastapi import FastAPI, File, HTTPException, Request, UploadFile

from main import (
    INVOICE_DIR,
    MAX_WORKERS,
    OUTPUT_PATH,
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
    return {"status": "ok"}


async def _process_pdf(pdf_bytes: bytes):
    with NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(pdf_bytes)
        tmp_path = tmp.name

    try:
        pages = list(enumerate(pdf_to_images(tmp_path), start=1))
        results = []
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = executor.map(
                lambda p: process_page(tmp_path, p[0], p[1]), pages
            )
            for key, result in futures:
                results.append(result)
                logger.info(f"done: {key}")
    finally:
        os.remove(tmp_path)

    return results


@app.post("/extract")
async def extract(file: UploadFile = File(...)):
    """Upload a single PDF (Postman: form-data, key 'file', type File) and get its extracted JSON back."""
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported")

    pdf_bytes = await file.read()
    return await _process_pdf(pdf_bytes)


@app.post("/extract/binary")
async def extract_binary(request: Request):
    """Upload a single PDF (Postman: Body > binary) and get its extracted JSON back."""
    pdf_bytes = await request.body()
    if not pdf_bytes:
        raise HTTPException(status_code=400, detail="Empty request body")
    if not pdf_bytes.startswith(b"%PDF-"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported")

    return await _process_pdf(pdf_bytes)
