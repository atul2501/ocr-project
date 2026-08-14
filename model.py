import glob
import io
import json
import logging
import os
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor
from prompt import PROMPTS
import httpx
import pymupdf
from api import (
    CONTRAST_CUTOFF,
    INVOICE_DIR,
    LIMIT_STATUS_CODES,
    LOG_PATH,
    MAX_RETRIES,
    MAX_WORKERS,
    MODEL,
    OUTPUT_PATH,
    PDF_ZOOM,
    RETRY_BACKOFF_BASE,
    RETRY_BACKOFF_CAP,
    RETRYABLE_STATUS_CODES,
    SHARPEN_PERCENT,
    SHARPEN_RADIUS,
    SHARPEN_THRESHOLD,
    SHARPENED_DIR,
    get_client,
    mark_limited,
)
from ollama import ResponseError
from PIL import Image, ImageFilter, ImageOps


logging.basicConfig(
    filename=LOG_PATH,
    filemode='a',
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
)
logger = logging.getLogger(__name__)

PROMPT = (PROMPTS)

# Single process-wide executor. Reused across the CLI batch run (main()) and
# every API request (api.py) so concurrent OCR calls are always capped at
# MAX_WORKERS instead of each caller spinning up its own pool.
executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)


def sharpen_image(png_bytes: bytes) -> bytes:
    with Image.open(io.BytesIO(png_bytes)) as img:
        gray = ImageOps.autocontrast(img.convert('L'), cutoff=CONTRAST_CUTOFF)
        sharpened = gray.filter(
            ImageFilter.UnsharpMask(
                radius=SHARPEN_RADIUS,
                percent=SHARPEN_PERCENT,
                threshold=SHARPEN_THRESHOLD,
            )
        )
        buf = io.BytesIO()
        sharpened.save(buf, format='PNG')
        return buf.getvalue()


def save_sharpened_page(pdf_path: str, page_num: int, image_bytes: bytes) -> str:
    os.makedirs(SHARPENED_DIR, exist_ok=True)
    stem = os.path.splitext(os.path.basename(pdf_path))[0]
    out_path = os.path.join(SHARPENED_DIR, f"{stem}_page{page_num}.pdf")
    with Image.open(io.BytesIO(image_bytes)) as img:
        img.convert('RGB').save(out_path, format='PDF')
    return out_path


def pdf_to_images(pdf_path: str) -> list[bytes]:
    doc = pymupdf.open(pdf_path)
    matrix = pymupdf.Matrix(PDF_ZOOM, PDF_ZOOM)
    images = []
    for page_num, page in enumerate(doc, start=1):
        image_bytes = sharpen_image(page.get_pixmap(matrix=matrix).tobytes('png'))
        save_sharpened_page(pdf_path, page_num, image_bytes)
        images.append(image_bytes)
    doc.close()
    return images


def extract_receipt(client, image) -> dict:
    response = client.chat(
        model=MODEL,
        messages=[
            {
                'role': 'user',
                'content': PROMPT,
                'images': [image],
            }
        ],
        format='json',
    )
    text = response['message']['content']

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r'\{.*\}', text, re.DOTALL)
        if not match:
            raise ValueError(f"Model did not return valid JSON:\n{text}")
        return json.loads(match.group(0))


def extract_receipt_with_retry(image: bytes) -> dict:
    last_exc = None
    for attempt in range(1, MAX_RETRIES + 2):
        client = get_client()
        try:
            return extract_receipt(client, image)
        except ResponseError as e:
            if e.status_code in LIMIT_STATUS_CODES:
                # This key's free-tier limit (or subscription) is exhausted -
                # take it out of rotation and immediately retry on the next
                # available key instead of backing off and hitting it again.
                mark_limited(client)
                last_exc = e
                logger.warning(f"attempt {attempt} failed (status {e.status_code}): key limit hit, switching key")
                continue
            if e.status_code not in RETRYABLE_STATUS_CODES:
                raise  # e.g. 404 model-not-found: never succeeds
            last_exc = e
            logger.warning(f"attempt {attempt} failed (status {e.status_code}): {e}")
        except (ConnectionError, httpx.RequestError, ValueError) as e:
            last_exc = e
            logger.warning(f"attempt {attempt} failed: {type(e).__name__}: {e}")

        if attempt <= MAX_RETRIES:
            delay = min(RETRY_BACKOFF_BASE * 2 ** (attempt - 1), RETRY_BACKOFF_CAP)
            time.sleep(delay + random.uniform(0, delay * 0.25))

    raise last_exc


def process_page(pdf_path: str, page_num: int, image_bytes: bytes) -> tuple[str, dict]:
    key = f"{os.path.basename(pdf_path)}#page{page_num}"
    try:
        return key, extract_receipt_with_retry(image_bytes)
    except Exception as e:
        # logger.exception (not .error) so the full traceback lands in
        # process.log, not just the message - needed to debug anything
        # unexpected later, not just the known retryable cases above.
        logger.exception(f"failed: {key}")
        return key, {"error": f"{type(e).__name__}: {e}"}


def is_target_document(result: dict) -> bool:
    """False only when the model explicitly flagged this page as not a
    Tax Invoice/PO; missing/unparseable values default to included so a
    model hiccup on this one field doesn't silently drop a real invoice."""
    value = result.get('metadata', {}).get('is_target_document', '')
    return str(value).strip().lower() not in ('false', 'no', '0')


def has_required_identifiers(result: dict) -> bool:
    """False when both invoice_number and document_type came back empty -
    i.e. the model couldn't identify enough about the page to be useful.
    Errored pages (no "document" to check) are kept so failures stay
    visible in the output instead of silently vanishing."""
    if 'error' in result:
        return True
    document = result.get('document', {})
    invoice_number = str(document.get('invoice_number', '')).strip()
    document_type = str(document.get('document_type', '')).strip()
    return bool(invoice_number or document_type)


def main():
    pages = []
    for pdf_path in glob.glob(os.path.join(INVOICE_DIR, '*.pdf')):
        try:
            for page_num, image_bytes in enumerate(pdf_to_images(pdf_path), start=1):
                pages.append((pdf_path, page_num, image_bytes))
        except Exception:
            # A corrupt/unreadable PDF shouldn't take the whole batch down;
            # log it and keep going with the rest of the files.
            logger.exception(f"failed to render {os.path.basename(pdf_path)}")

    results = []
    for key, result in executor.map(lambda args: process_page(*args), pages):
        if not is_target_document(result):
            logger.info(f"skipped (not tax invoice/PO): {key}")
            continue
        if not has_required_identifiers(result):
            logger.info(f"skipped (missing invoice number and document type): {key}")
            continue
        results.append(result)
        logger.info(f"done: {key}")

    try:
        with open(OUTPUT_PATH, 'w', encoding='utf-8') as f:
            json.dump(results, f, indent=2, ensure_ascii=False)
    except OSError:
        logger.exception(f"failed to write {OUTPUT_PATH}")
        raise

    print(json.dumps(results, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        # Catch-all so any crash (bad INVOICE_DIR, disk full, etc.) is
        # recorded in process.log with a full traceback, not just printed
        # to the console and lost.
        logger.exception("main() crashed")
        raise
