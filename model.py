import glob
import io
import json
import logging
import os
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from prompt import PROMPTS
import httpx
import pymupdf
from api import (
    CONTRAST_CUTOFF,
    INVOICE_DIR,
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
    SHARPENED_MAX_AGE_SECONDS,
    get_client,
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


def cleanup_old_output() -> None:
    """Delete sharpened debug page PDFs in SHARPENED_DIR older than
    SHARPENED_MAX_AGE_SECONDS - keeps output/ from growing unbounded
    across CLI runs and API requests."""
    if not os.path.isdir(SHARPENED_DIR):
        return
    cutoff = time.time() - SHARPENED_MAX_AGE_SECONDS
    for name in os.listdir(SHARPENED_DIR):
        path = os.path.join(SHARPENED_DIR, name)
        try:
            if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                os.remove(path)
        except OSError:
            logger.exception(f"failed to clean up {path}")


def extract_receipt(image) -> dict:
    response = get_client().chat(
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
        parsed = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r'\{.*\}', text, re.DOTALL)
        if not match:
            raise ValueError(f"Model did not return valid JSON:\n{text}")
        parsed = json.loads(match.group(0))

    # The model sometimes wraps the single page object in a list (echoing
    # the prompt's "[{...}, ...]" example literally) instead of returning
    # it bare - normalize to a plain dict either way.
    if isinstance(parsed, list):
        if not parsed or not isinstance(parsed[0], dict):
            raise ValueError(f"Model returned an unexpected JSON shape:\n{text}")
        parsed = parsed[0]
    return parsed


def extract_receipt_with_retry(image: bytes) -> dict:
    last_exc = None
    for attempt in range(1, MAX_RETRIES + 2):
        try:
            return extract_receipt(image)
        except ResponseError as e:
            if e.status_code not in RETRYABLE_STATUS_CODES:
                raise  # e.g. 404 model-not-found, 403 needs-subscription: never succeeds
            last_exc = e
            logger.warning(f"attempt {attempt} failed (status {e.status_code}): {e}")
        except (ConnectionError, httpx.RequestError, ValueError) as e:
            last_exc = e
            logger.warning(f"attempt {attempt} failed: {type(e).__name__}: {e}")

        if attempt <= MAX_RETRIES:
            delay = min(RETRY_BACKOFF_BASE * 2 ** (attempt - 1), RETRY_BACKOFF_CAP)
            time.sleep(delay + random.uniform(0, delay * 0.25))

    raise last_exc


def is_blank_result(result: dict) -> bool:
    """True if extraction found nothing usable - e.g. the page isn't a Tax
    Invoice/PO (blank page, cover sheet, etc.) or the model echoed the empty
    template back."""
    is_target = str(result.get("METADATA", {}).get("IS_TARGET_DOCUMENT", "")).strip().lower()
    if is_target and is_target != "true":
        return True  # model explicitly marked this page as not a target document

    def _blank(value):
        if isinstance(value, dict):
            return all(_blank(v) for v in value.values())
        if isinstance(value, list):
            return all(_blank(v) for v in value)
        if isinstance(value, str):
            return not value.strip()
        return value is None
    return _blank(result)


def _to_float(value) -> float:
    """Parse an extracted amount/quantity string into a real number for
    SAP - "", None or anything unparseable becomes 0 (SAP's DEC/NUMC fields
    expect an actual number, never a missing key or null)."""
    if value in ("", None):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    cleaned = str(value).replace(",", "").strip()
    if not cleaned:
        return 0.0
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


@dataclass
class InvoiceItem:
    """One SAP-mapped line item, built from an entry in the extracted
    "ITEMS" array."""
    DESCRIPTION: str = ""
    HSN: str = ""
    QTY: float = 0.0
    UNIT_PRICE: float = 0.0
    AMOUNT: float = 0.0

    @classmethod
    def from_dict(cls, item: dict) -> "InvoiceItem":
        return cls(
            DESCRIPTION=item.get("DESCRIPTION", ""),
            HSN=item.get("HSN_CODE", ""),
            QTY=_to_float(item.get("QUANTITY", "")),
            UNIT_PRICE=_to_float(item.get("UNIT_PRICE", "")),
            AMOUNT=_to_float(item.get("LINE_TOTAL", "")),
        )


@dataclass
class Invoice:
    """SAP-mapped invoice, built from one extracted page dict (the
    "DOCUMENT"/"SUPPLIER"/"TAX"/... sections returned by extract_receipt)."""
    IRN_NO: str = ""
    IRN_DATE: str = ""
    INVOICE_NUMBER: str = ""
    ORDER_NUMBER: str = ""
    VENDOR_GST_NO: str = ""
    BASE_VALUE: float = 0.0
    IGST: float = 0.0
    CGST: float = 0.0
    SGST: float = 0.0
    GROSS_TOTAL: float = 0.0
    INVOICE_DATE: str = ""
    ORDER_DATE: str = ""
    VENDOR_PAN_NO: str = ""
    CUSTOMER_PAN_NO: str = ""
    ITEM_LIST: list[InvoiceItem] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> "Invoice":
        document = data.get("DOCUMENT", {})
        supplier = data.get("SUPPLIER", {})
        customer = data.get("CUSTOMER", {})
        purchase_order = data.get("PURCHASE_ORDER", {})
        tax = data.get("TAX", {})
        amounts = data.get("AMOUNTS", {})
        gst_compliance = data.get("GST_COMPLIANCE", {})
        return cls(
            IRN_NO=gst_compliance.get("IRN", ""),
            IRN_DATE=gst_compliance.get("ACKNOWLEDGEMENT_DATE", ""),
            INVOICE_NUMBER=document.get("INVOICE_NUMBER", ""),
            ORDER_NUMBER=purchase_order.get("PO_NUMBER", ""),
            VENDOR_GST_NO=supplier.get("GSTIN", ""),
            BASE_VALUE=_to_float(tax.get("TAXABLE_AMOUNT", "")),
            IGST=_to_float(tax.get("IGST_AMOUNT", "")),
            CGST=_to_float(tax.get("CGST_AMOUNT", "")),
            SGST=_to_float(tax.get("SGST_AMOUNT", "")),
            GROSS_TOTAL=_to_float(amounts.get("TOTAL_AMOUNT", "")),
            INVOICE_DATE=document.get("INVOICE_DATE", ""),
            ORDER_DATE=purchase_order.get("PO_DATE", ""),
            VENDOR_PAN_NO=supplier.get("PAN", ""),
            CUSTOMER_PAN_NO=customer.get("PAN", ""),
            ITEM_LIST=[InvoiceItem.from_dict(item) for item in data.get("ITEMS", [])],
        )

    def to_dict(self) -> dict:
        """Every field is always present - SAP expects a fixed set of keys
        on every record, so missing text stays "" and missing numbers stay
        0 rather than being dropped."""
        return asdict(self)

    def to_json(self, **kwargs) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, **kwargs)


def group_into_invoices(page_results: list[dict]) -> list[Invoice]:
    """Turn a flat list of per-page extraction dicts into one Invoice per
    distinct INVOICE_NUMBER - pages that share an invoice number (e.g. a
    multi-page invoice) are merged into a single Invoice with combined
    ITEM_LIST; distinct invoice numbers each get their own Invoice."""
    invoices_by_number: dict[str, Invoice] = {}
    order: list[str] = []
    for i, page in enumerate(page_results):
        invoice = Invoice.from_dict(page)
        key = invoice.INVOICE_NUMBER or f"__unknown_{i}"
        if key in invoices_by_number:
            invoices_by_number[key].ITEM_LIST.extend(invoice.ITEM_LIST)
        else:
            invoices_by_number[key] = invoice
            order.append(key)
    return [invoices_by_number[k] for k in order]


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
        if is_blank_result(result):
            logger.info(f"skipped (blank): {key}")
            continue
        if "error" in result:
            logger.info(f"skipped (failed): {key}")
            continue
        results.append(result)
        logger.info(f"done: {key}")

    invoices = group_into_invoices(results)
    output = [invoice.to_dict() for invoice in invoices]

    try:
        with open(OUTPUT_PATH, 'w', encoding='utf-8') as f:
            json.dump(output, f, indent=2, ensure_ascii=False)
    except OSError:
        logger.exception(f"failed to write {OUTPUT_PATH}")
        raise

    print(json.dumps(output, indent=2, ensure_ascii=False))
    cleanup_old_output()


if __name__ == '__main__':
    try:
        main()
    except Exception:
        # Catch-all so any crash (bad INVOICE_DIR, disk full, etc.) is
        # recorded in process.log with a full traceback, not just printed
        # to the console and lost.
        logger.exception("main() crashed")
        raise
