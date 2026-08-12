import glob
import json
import os
from concurrent.futures import ThreadPoolExecutor
from tempfile import NamedTemporaryFile

from fastapi import FastAPI, File, HTTPException, UploadFile

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
        "endpoints": ["/health", "POST /extract"],
    }


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/extract")
async def extract(file: UploadFile = File(...)):
    """Upload a single PDF (Postman: form-data, key 'file', type File) and get its extracted JSON back."""
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported")

    pdf_bytes = await file.read()
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


# @app.get("/extract-all")
# def extract_all():
#     """Process every PDF in the invoice/ folder (same as running main.py) and return the combined JSON."""
#     pages = []
#     for pdf_path in glob.glob(os.path.join(INVOICE_DIR, "*.pdf")):
#         for page_num, image_bytes in enumerate(pdf_to_images(pdf_path), start=1):
#             pages.append((pdf_path, page_num, image_bytes))

#     results = []
#     with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
#         for key, result in executor.map(lambda args: process_page(*args), pages):
#             results.append(result)
#             logger.info(f"done: {key}")

#     with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
#         json.dump(results, f, indent=2, ensure_ascii=False)

#     return results


# @app.get("/results")
# def get_results():
#     """Fetch the results file written by main.py / the last /extract-all run."""
#     if not os.path.exists(OUTPUT_PATH):
#         raise HTTPException(status_code=404, detail=f"{OUTPUT_PATH} not found yet")
#     with open(OUTPUT_PATH, "r", encoding="utf-8") as f:
#         return json.load(f)




# "Extract the data from this receipt and return ONLY a single JSON object "
#     "with fields: merchant_name, date, items (list of {name, quantity, price}), "
#     "subtotal, tax, total. Use null for anything unreadable. No prose, no markdown."