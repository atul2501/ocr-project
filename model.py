import glob
import io
import json
import logging
import os
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field, fields
from prompt import PROMPTS
import httpx
import pymupdf
from api import (
    BLANK_PAGE_INK_FRACTION,
    BLANK_PAGE_INK_THRESHOLD,
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
    SAVE_DEBUG_PAGES,
    SHARPEN_PERCENT,
    SHARPEN_RADIUS,
    SHARPEN_THRESHOLD,
    SHARPENED_DIR,
    get_client,
    mark_exhausted,
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

executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)


def sharpen_image(img: Image.Image) -> bytes:
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


def _is_blank_page(img: Image.Image) -> bool:
    histogram = img.convert('L').histogram()
    total = sum(histogram)
    ink_pixels = sum(histogram[:BLANK_PAGE_INK_THRESHOLD])
    return total > 0 and (ink_pixels / total) < BLANK_PAGE_INK_FRACTION


def pdf_to_images(pdf_path: str) -> list[tuple[bytes, bool]]:
    doc = pymupdf.open(pdf_path)
    matrix = pymupdf.Matrix(PDF_ZOOM, PDF_ZOOM)
    pages = []
    for page_num, page in enumerate(doc, start=1):
        pix = page.get_pixmap(matrix=matrix)
        raw_image = Image.frombytes('RGB', (pix.width, pix.height), pix.samples)
        blank = _is_blank_page(raw_image)
        image_bytes = sharpen_image(raw_image)
        if SAVE_DEBUG_PAGES:
            save_sharpened_page(pdf_path, page_num, image_bytes)
        pages.append((image_bytes, blank))
    doc.close()
    return pages


def extract_receipt(image) -> list[dict]:
    client = get_client()
    try:
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
            think=False,  
            options={'temperature': 0},
            keep_alive='30m',
        )
    except ResponseError as e:
        if e.status_code == 429 and "weekly usage limit" in e.error.lower():
            mark_exhausted(client)
        raise
    text = response['message']['content']

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r'\[.*\]|\{.*\}', text, re.DOTALL)
        if not match:
            raise ValueError(f"Model did not return valid JSON:\n{text}")
        parsed = json.loads(match.group(0))


    if isinstance(parsed, dict):
        parsed = [parsed]
    if not isinstance(parsed, list) or not parsed or not all(isinstance(p, dict) for p in parsed):
        raise ValueError(f"Model returned an unexpected JSON shape:\n{text}")
    return parsed


def extract_receipt_with_retry(image: bytes) -> list[dict]:
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


def _is_blank_value(value) -> bool:
    if isinstance(value, dict):
        return all(_is_blank_value(v) for v in value.values())
    if isinstance(value, list):
        return all(_is_blank_value(v) for v in value)
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (int, float)):
        return value == 0
    return value is None


def is_blank_result(result: dict) -> bool:
    is_target = str(result.get("METADATA", {}).get("IS_TARGET_DOCUMENT", "")).strip().lower()
    if is_target and is_target != "true":
        return True

    return _is_blank_value(result)


def _first_value(*values):
    for v in values:
        if v not in ("", None):
            return v
    return ""


_NAMED_CHARGE_FIELDS = [
    ("EMPTY_UNLOADING_CHARGES", ("EMPTY UNLOADING", "EMPTY UNLOAD")),
    ("UNLOADING_CHARGES", ("UNLOADING",)),
    ("LOADING_CHARGES", ("LOADING",)),
    ("DETENTION_CHARGES", ("DETENTION",)),
    ("DEMURRAGE_CHARGES", ("DEMURRAGE",)),
    ("PARKING_CHARGES", ("PARKING",)),
    ("WEIGHMENT_CHARGES", ("WEIGHMENT", "WEIGHTMENT", "WEIGHBRIDGE")),
]


def _item_fingerprint(item: "InvoiceItem") -> tuple:
    is_charge_row = not item.HSN and item.QTY == 0.0 and item.UNIT_PRICE == 0.0 and not item.VEHICLE_NUMBER
    if is_charge_row:
        return (item.DESCRIPTION, item.AMOUNT)
    return (item.HSN, item.QTY, item.UNIT_PRICE, item.AMOUNT, item.VEHICLE_NUMBER)


_PAN_PATTERN = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")


def _pan_from_gstin(gstin: str) -> str:
    gstin = (gstin or "").strip().upper()
    if len(gstin) != 15 or gstin[13] != "Z":
        return ""
    candidate = gstin[2:12]
    return candidate if _PAN_PATTERN.match(candidate) else ""


_PLACE_OF_SUPPLY_PATTERN = re.compile(r"^\s*(\d{1,2})\s*[-–:]?\s*(.*)$")


def _split_place_of_supply(document: dict, gst_compliance: dict) -> tuple[str, str]:
    raw = _first_value(document.get("PLACE_OF_SUPPLY"), gst_compliance.get("PLACE_OF_SUPPLY"))
    match = _PLACE_OF_SUPPLY_PATTERN.match(raw) if raw else None
    parsed_code = match.group(1) if match else ""
    parsed_state = match.group(2).strip() if match else raw.strip() if raw else ""
    code = _first_value(gst_compliance.get("PLACE_OF_SUPPLY_STATE_CODE"), parsed_code)
    return code, parsed_state


def _to_float(value) -> float:
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


def _named_charge_amounts(data: dict) -> dict[str, float]:
    logistics = data.get("LOGISTICS", {})
    charges = {name: _to_float(logistics.get(name, "")) for name, _ in _NAMED_CHARGE_FIELDS}

    for charge in data.get("ADDITIONAL_CHARGES", []):
        description = (charge.get("DESCRIPTION", "") or charge.get("CHARGE_TYPE", "")).upper()
        for name, keywords in _NAMED_CHARGE_FIELDS:
            if charges[name]:
                continue  # already have a value for this field from LOGISTICS
            if any(keyword in description for keyword in keywords):
                charges[name] = _to_float(_first_value(charge.get("AMOUNT"), charge.get("TOTAL_AMOUNT")))
                break

    loading_family = ["EMPTY_UNLOADING_CHARGES", "UNLOADING_CHARGES", "LOADING_CHARGES"]
    for i, name in enumerate(loading_family):
        if not charges[name]:
            continue
        for other in loading_family[i + 1:]:
            if charges[other] == charges[name]:
                charges[other] = 0.0

    return charges


def _build_charge_items(data: dict) -> list["InvoiceItem"]:
    charges = _named_charge_amounts(data)
    return [
        InvoiceItem(DESCRIPTION=name.replace("_", " ").title(), AMOUNT=amount)
        for name, _ in _NAMED_CHARGE_FIELDS
        if (amount := charges[name])
    ]


_FREIGHT_VENDOR_KEYWORDS = (
    "LOGISTICS", "TRANSPORT", "SHIPPING", "FREIGHT", "CARGO",
    "FORWARDING", "CARRIER", "FLEET", "COURIER",
)


def _label_freight_items(
    item_list: list["InvoiceItem"], primary_item_count: int, vehicle_number: str, vendor_name: str) -> None:
    primary_items = item_list[:primary_item_count]
    if not primary_items:
        return
    is_freight_bill = (
        bool(vehicle_number)
        or len(item_list) > primary_item_count  # a named logistics charge was found (see _build_charge_items)
        or any(item.VEHICLE_NUMBER or item.WEIGHT for item in primary_items)
        or any(keyword in vendor_name.upper() for keyword in _FREIGHT_VENDOR_KEYWORDS)
    )
    if not is_freight_bill:
        return
    for item in primary_items:
        if not re.search(r"\bfreight\b", item.DESCRIPTION, re.IGNORECASE):
            item.DESCRIPTION = f"Freight Charges - {item.DESCRIPTION}" if item.DESCRIPTION else "Freight Charges"


@dataclass
class InvoiceItem:
    """One SAP-mapped line item, built from an entry in the extracted
    "ITEMS" array."""
    DESCRIPTION: str = ""
    HSN: str = ""
    QTY: float = 0.0
    UOM: str = ""
    WEIGHT: float = 0.0
    WEIGHT_UNIT: str = ""
    UNIT_PRICE: float = 0.0
    AMOUNT: float = 0.0
    VEHICLE_NUMBER: str = ""

    @classmethod
    def from_dict(cls, item: dict) -> "InvoiceItem":
        return cls(
            DESCRIPTION=item.get("DESCRIPTION", ""),
            HSN=item.get("HSN_CODE", ""),
            QTY=_to_float(item.get("QUANTITY", "")),
            UOM=item.get("UOM", ""),
            WEIGHT=_to_float(item.get("WEIGHT", "")),
            WEIGHT_UNIT=item.get("WEIGHT_UNIT", ""),
            UNIT_PRICE=_to_float(item.get("UNIT_PRICE", "")),
            AMOUNT=_to_float(item.get("LINE_TOTAL", "")),
            VEHICLE_NUMBER=item.get("VEHICLE_NUMBER", ""),
        )


@dataclass
class Invoice:
    """SAP-mapped invoice, built from one extracted page dict (the
    "DOCUMENT"/"SUPPLIER"/"TAX"/... sections returned by extract_receipt)."""
    IRN_NO: str = ""
    IRN_DATE: str = ""
    INVOICE_NUMBER: str = ""
    ORDER_NUMBER: str = ""
    VENDOR_NAME: str = ""
    VENDOR_GST_NO: str = ""
    CUSTOMER_NAME: str = ""
    CUSTOMER_GST_NO: str = ""
    PLACE_OF_SUPPLY_CODE: str = ""
    PLACE_OF_SUPPLY_STATE: str = ""
    BASE_VALUE: float = 0.0
    IGST: float = 0.0
    CGST: float = 0.0
    SGST: float = 0.0
    GROSS_TOTAL: float = 0.0
    INVOICE_DATE: str = ""
    ORDER_DATE: str = ""
    VENDOR_PAN_NO: str = ""
    CUSTOMER_PAN_NO: str = ""
    VEHICLE_NUMBER: str = ""
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
        logistics = data.get("LOGISTICS", {})
        delivery = data.get("DELIVERY", {})

        vehicle_number = _first_value(logistics.get("VEHICLE_NUMBER"), delivery.get("VEHICLE_NUMBER"))
        vendor_name = _first_value(supplier.get("NAME"), supplier.get("LEGAL_NAME"), supplier.get("TRADE_NAME"))
        item_list = cls._build_item_list(data)
        primary_item_count = len(item_list)
        item_list.extend(_build_charge_items(data))
        _label_freight_items(item_list, primary_item_count, vehicle_number, vendor_name)
        place_of_supply_code, place_of_supply_state = _split_place_of_supply(document, gst_compliance)
        gross_total = _to_float(amounts.get("TOTAL_AMOUNT", ""))
        total_tax = (
            _to_float(tax.get("IGST_AMOUNT", ""))
            + _to_float(tax.get("CGST_AMOUNT", ""))
            + _to_float(tax.get("SGST_AMOUNT", ""))
        )

        if total_tax == 0 and gross_total:
            base_value = gross_total
        else:
            base_value_raw = _first_value(
                tax.get("TAXABLE_AMOUNT"),
                amounts.get("TAXABLE_AMOUNT"),
                amounts.get("SUBTOTAL"),
            )
            base_value = (
                sum(item.AMOUNT for item in item_list)
                if not base_value_raw and item_list
                else _to_float(base_value_raw)
            )

        return cls(
            IRN_NO=gst_compliance.get("IRN", ""),
            IRN_DATE=gst_compliance.get("ACKNOWLEDGEMENT_DATE", ""),
            INVOICE_NUMBER=document.get("INVOICE_NUMBER", ""),
            ORDER_NUMBER=_first_value(
                purchase_order.get("PO_NUMBER"),
                purchase_order.get("PURCHASE_ORDER_REFERENCE"),
                document.get("INVOICE_REFERENCE_NUMBER"),
            ),
            VENDOR_NAME=vendor_name,
            VENDOR_GST_NO=supplier.get("GSTIN", ""),
            CUSTOMER_NAME=_first_value(customer.get("NAME"), customer.get("LEGAL_NAME")),
            CUSTOMER_GST_NO=_first_value(customer.get("GSTIN"), gst_compliance.get("CUSTOMER_GSTIN")),
            PLACE_OF_SUPPLY_CODE=place_of_supply_code,
            PLACE_OF_SUPPLY_STATE=place_of_supply_state,
            BASE_VALUE=base_value,
            IGST=_to_float(tax.get("IGST_AMOUNT", "")),
            CGST=_to_float(tax.get("CGST_AMOUNT", "")),
            SGST=_to_float(tax.get("SGST_AMOUNT", "")),
            GROSS_TOTAL=_to_float(amounts.get("TOTAL_AMOUNT", "")),
            INVOICE_DATE=document.get("INVOICE_DATE", ""),
            ORDER_DATE=purchase_order.get("PO_DATE", ""),
            VENDOR_PAN_NO=_first_value(supplier.get("PAN"), _pan_from_gstin(supplier.get("GSTIN", ""))),
            CUSTOMER_PAN_NO=_first_value(
                customer.get("PAN"),
                _pan_from_gstin(_first_value(customer.get("GSTIN"), gst_compliance.get("CUSTOMER_GSTIN"))),
            ),
            VEHICLE_NUMBER=vehicle_number,
            ITEM_LIST=item_list,
        )

    @staticmethod
    def _build_item_list(data: dict) -> list["InvoiceItem"]:
        items = [InvoiceItem.from_dict(item) for item in data.get("ITEMS", [])]
        return [item for item in items if not _is_blank_value(asdict(item))]

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self, **kwargs) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, **kwargs)


def is_blank_invoice(invoice: Invoice) -> bool:
    return _is_blank_value(invoice.to_dict())


def group_into_invoices(page_results: list[tuple[str, dict]]) -> list[Invoice]:
    invoice_number_by_source: dict[str, str] = {}
    for source_id, page in page_results:
        number = page.get("DOCUMENT", {}).get("INVOICE_NUMBER", "")
        if number and source_id not in invoice_number_by_source:
            invoice_number_by_source[source_id] = number

    invoices_by_number: dict[str, Invoice] = {}
    seen_items_by_number: dict[str, set] = {}
    source_ids_by_number: dict[str, set[str]] = {}
    order: list[str] = []
    for i, (source_id, page) in enumerate(page_results):
        invoice = Invoice.from_dict(page)
        key = invoice.INVOICE_NUMBER or invoice_number_by_source.get(source_id) or f"__unknown_{i}"
        source_ids_by_number.setdefault(key, set()).add(source_id)
        if key in invoices_by_number:
            existing = invoices_by_number[key]
            for f in fields(Invoice):
                if f.name == "ITEM_LIST":
                    continue
                if getattr(existing, f.name) in ("", 0.0) and getattr(invoice, f.name) not in ("", 0.0):
                    setattr(existing, f.name, getattr(invoice, f.name))
            seen = seen_items_by_number[key]
            incoming_fingerprints = set()
            for item in invoice.ITEM_LIST:
                fingerprint = _item_fingerprint(item)
                if fingerprint not in seen:
                    existing.ITEM_LIST.append(item)
                    incoming_fingerprints.add(fingerprint)
            seen.update(incoming_fingerprints)
        else:
            seen_items_by_number[key] = {_item_fingerprint(item) for item in invoice.ITEM_LIST}
            invoices_by_number[key] = invoice
            order.append(key)
    return _merge_duplicate_totals(
        [invoices_by_number[k] for k in order],
        [source_ids_by_number[k] for k in order],
    )


def _merge_duplicate_totals(invoices: list[Invoice], source_ids: list[set[str]]) -> list[Invoice]:
    def populated_count(inv: Invoice) -> int:
        return sum(
            1 for f in fields(Invoice)
            if f.name != "ITEM_LIST" and getattr(inv, f.name) not in ("", 0.0)
        )

    kept: list[Invoice] = []
    kept_sources: list[set[str]] = []
    for invoice, srcs in zip(invoices, source_ids):
        for i, existing in enumerate(kept):
            if (
                srcs & kept_sources[i]
                and invoice.GROSS_TOTAL != 0
                and round(invoice.GROSS_TOTAL, 2) == round(existing.GROSS_TOTAL, 2)
                and round(invoice.BASE_VALUE, 2) == round(existing.BASE_VALUE, 2)
            ):
                primary, other = (
                    (existing, invoice)
                    if populated_count(existing) >= populated_count(invoice)
                    else (invoice, existing)
                )
                for f in fields(Invoice):
                    if f.name == "ITEM_LIST":
                        continue
                    if getattr(primary, f.name) in ("", 0.0) and getattr(other, f.name) not in ("", 0.0):
                        setattr(primary, f.name, getattr(other, f.name))
                kept[i] = primary
                kept_sources[i] |= srcs
                break
        else:
            kept.append(invoice)
            kept_sources.append(srcs)
    return kept


def process_page(pdf_path: str, page_num: int, image_bytes: bytes) -> tuple[str, list[dict] | dict]:
    key = f"{os.path.basename(pdf_path)}#page{page_num}"
    try:
        return key, extract_receipt_with_retry(image_bytes)
    except Exception as e:
        logger.exception(f"failed: {key}")
        return key, {"error": f"{type(e).__name__}: {e}"}


def main():
    pages = []
    for pdf_path in glob.glob(os.path.join(INVOICE_DIR, '*.pdf')):
        try:
            for page_num, (image_bytes, blank) in enumerate(pdf_to_images(pdf_path), start=1):
                if blank:
                    logger.info(f"skipped (blank page, no OCR call): {os.path.basename(pdf_path)}#page{page_num}")
                    continue
                pages.append((pdf_path, page_num, image_bytes))
        except Exception:
            logger.exception(f"failed to render {os.path.basename(pdf_path)}")

    results = []
    for (pdf_path, _, _), (key, result) in zip(pages, executor.map(lambda args: process_page(*args), pages)):
        if isinstance(result, dict):  # process_page's {"error": ...} sentinel
            logger.info(f"skipped (failed): {key}")
            continue
        kept = [invoice for invoice in result if not is_blank_result(invoice)]
        if not kept:
            logger.info(f"skipped (blank): {key}")
            continue
        source = os.path.basename(pdf_path)
        results.extend((source, invoice) for invoice in kept)
        logger.info(f"done: {key} ({len(kept)} invoice(s))")

    invoices = [inv for inv in group_into_invoices(results) if not is_blank_invoice(inv)]
    output = [invoice.to_dict() for invoice in invoices]

    try:
        with open(OUTPUT_PATH, 'w', encoding='utf-8') as f:
            json.dump(output, f, indent=2, ensure_ascii=False)
    except OSError:
        logger.exception(f"failed to write {OUTPUT_PATH}")
        raise

    print(json.dumps(output, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        logger.exception("main() crashed")
        raise
