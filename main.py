import asyncio
import os
from tempfile import NamedTemporaryFile
from fastapi import FastAPI, HTTPException, Request
from model import (
    executor,
    group_into_invoices,
    is_blank_invoice,
    is_blank_result,
    logger,
    process_pdf,
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
        # Submit to the shared process-wide executor (model.executor, capped
        # at MAX_WORKERS) and await the future instead of blocking on it, so
        # extra requests queue behind the cap without freezing the FastAPI
        # event loop for other concurrent uploads.
        key, result = await loop.run_in_executor(executor, process_pdf, tmp_path)
        results = []
        if isinstance(result, dict):  # process_pdf's {"error": ...} sentinel
            logger.info(f"skipped (failed): {key}")
        else:
            kept = [invoice for invoice in result if not is_blank_result(invoice)]
            if kept:
                results.extend((key, invoice) for invoice in kept)
                logger.info(f"done: {key} ({len(kept)} invoice(s))")
            else:
                logger.info(f"skipped (blank): {key}")
    finally:
        os.remove(tmp_path)

    invoices = [inv for inv in group_into_invoices(results) if not is_blank_invoice(inv)]
    return [invoice.to_dict() for invoice in invoices]


@app.post("/extract")
async def extract_binary(request: Request):
    """Upload a single PDF (Postman: Body > binary) and get its extracted JSON back."""
    pdf_bytes = await request.body()
    if not pdf_bytes:
        raise HTTPException(status_code=400, detail="Empty request body")
    if not pdf_bytes.startswith(b"%PDF-"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported")

    return await _process_pdf(pdf_bytes)
