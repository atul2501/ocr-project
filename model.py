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


def extract_receipt(image) -> list[dict]:
    """Returns one raw extraction dict per invoice found on this page image -
    almost always a single-element list, but a page can genuinely show more
    than one separate invoice (e.g. two small bills photographed together),
    and the prompt asks the model to return one array entry per invoice in
    that case. Every entry is kept - none may be silently dropped."""
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
        match = re.search(r'\[.*\]|\{.*\}', text, re.DOTALL)
        if not match:
            raise ValueError(f"Model did not return valid JSON:\n{text}")
        parsed = json.loads(match.group(0))

    # The model is asked for an array (one object per invoice on the page)
    # but occasionally still returns a single bare object - normalize to a
    # list either way, without ever discarding extra array entries.
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
    """True if extraction found nothing usable - e.g. the page isn't a Tax
    Invoice/PO (blank page, cover sheet, etc.) or the model echoed the empty
    template back."""
    is_target = str(result.get("METADATA", {}).get("IS_TARGET_DOCUMENT", "")).strip().lower()
    if is_target and is_target != "true":
        return True  # model explicitly marked this page as not a target document

    return _is_blank_value(result)


def _first_value(*values):
    """Return the first non-blank value from a sequence of extracted
    fields - several sections of the schema carry the same fact under
    different keys (e.g. CUSTOMER.GSTIN vs GST_COMPLIANCE.CUSTOMER_GSTIN),
    and the model doesn't always fill in the same one."""
    for v in values:
        if v not in ("", None):
            return v
    return ""


# Named logistics/freight-related charges that each get their own header
# field on Invoice (WEIGHMENT_CHARGES, PARKING_CHARGES, ...) rather than
# being folded into ITEM_LIST. Order matters when matching an
# ADDITIONAL_CHARGES description below: most specific phrase first, since
# "LOADING" is itself a substring of "UNLOADING", which is itself a
# substring of "EMPTY UNLOADING" - checking in this order (and stopping at
# the first match per charge) means a genuine "EMPTY UNLOADING" charge can
# never be misclassified as a plain "LOADING" one. FREIGHT_AMOUNT has no
# field here: freight is the primary billed service on a GTA bill, so it's
# already carried in ITEMS/ITEM_LIST, and including it here too would
# double it up.
_NAMED_CHARGE_FIELDS = [
    ("EMPTY_UNLOADING_CHARGES", ("EMPTY UNLOADING", "EMPTY UNLOAD")),
    ("UNLOADING_CHARGES", ("UNLOADING",)),
    ("LOADING_CHARGES", ("LOADING",)),
    ("DETENTION_CHARGES", ("DETENTION",)),
    ("DEMURRAGE_CHARGES", ("DEMURRAGE",)),
    ("PARKING_CHARGES", ("PARKING",)),
    ("WEIGHMENT_CHARGES", ("WEIGHMENT", "WEIGHTMENT", "WEIGHBRIDGE")),
]


_PAN_PATTERN = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")


def _pan_from_gstin(gstin: str) -> str:
    """Derive the 10-character PAN embedded in a GSTIN's characters 3-12 -
    this is a mechanical substring of a value already fully printed on the
    page (GSTIN = 2-digit state code + PAN + 1-digit entity code + "Z" + 1
    checksum char), not a guess, so it's safe to fill in even on invoices
    that print the GSTIN but never print the PAN by itself. Relying on this
    instead of whatever the model happened to extract for PAN also avoids
    the inconsistency of the model deriving it on some pages but not others."""
    gstin = (gstin or "").strip().upper()
    if len(gstin) != 15 or gstin[13] != "Z":
        return ""
    candidate = gstin[2:12]
    return candidate if _PAN_PATTERN.match(candidate) else ""


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


def _build_named_charges(data: dict) -> dict[str, float]:
    """Map each of the fixed named logistics/freight charge types (see
    _NAMED_CHARGE_FIELDS) to its amount, for Invoice's WEIGHMENT_CHARGES/
    PARKING_CHARGES/... header fields. Prefers an explicit LOGISTICS.<field>
    value (unambiguous), falling back to scanning ADDITIONAL_CHARGES by
    description keyword - the model sometimes reports the same charge as a
    described ADDITIONAL_CHARGES row instead of filling the matching
    LOGISTICS field. Each ADDITIONAL_CHARGES entry is classified into at
    most one of these fields (first match wins, in _NAMED_CHARGE_FIELDS
    order), so one charge can't double-count into two fields that way."""
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

    # LOGISTICS itself can carry the same duplication risk the classifier
    # above guards against: EMPTY_UNLOADING_CHARGES, UNLOADING_CHARGES and
    # LOADING_CHARGES are separate raw keys, so the model can (and does)
    # fill more than one of them with the same amount for what is really a
    # single printed "EMPTY UNLOADING CHARGES" line - the substring overlap
    # between these three names ("LOADING" sits inside "UNLOADING" sits
    # inside "EMPTY UNLOADING") makes them the specific fields prone to this,
    # not a generic risk across all seven. If two of them carry the exact
    # same non-zero amount, keep only the most specific one (this list's
    # order) and zero the rest, rather than trusting a coincidence.
    loading_family = ["EMPTY_UNLOADING_CHARGES", "UNLOADING_CHARGES", "LOADING_CHARGES"]
    for i, name in enumerate(loading_family):
        if not charges[name]:
            continue
        for other in loading_family[i + 1:]:
            if charges[other] == charges[name]:
                charges[other] = 0.0

    return charges


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
    PLACE_OF_SUPPLY: str = ""
    BASE_VALUE: float = 0.0
    IGST: float = 0.0
    CGST: float = 0.0
    SGST: float = 0.0
    GROSS_TOTAL: float = 0.0
    INVOICE_DATE: str = ""
    ORDER_DATE: str = ""
    VENDOR_PAN_NO: str = ""
    CUSTOMER_PAN_NO: str = ""
    WEIGHMENT_CHARGES: float = 0.0
    PARKING_CHARGES: float = 0.0
    LOADING_CHARGES: float = 0.0
    UNLOADING_CHARGES: float = 0.0
    EMPTY_UNLOADING_CHARGES: float = 0.0
    DETENTION_CHARGES: float = 0.0
    DEMURRAGE_CHARGES: float = 0.0
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

        item_list = cls._build_item_list(data)
        named_charges = _build_named_charges(data)
        gross_total = _to_float(amounts.get("TOTAL_AMOUNT", ""))
        total_tax = (
            _to_float(tax.get("IGST_AMOUNT", ""))
            + _to_float(tax.get("CGST_AMOUNT", ""))
            + _to_float(tax.get("SGST_AMOUNT", ""))
        )

        if total_tax == 0 and gross_total:
            # No GST is charged on this invoice, so the taxable value is by
            # definition the same as the total - trust the printed total
            # over TAX.TAXABLE_AMOUNT, which the model sometimes fills with
            # a single line item's value instead of a real taxable-amount
            # field when the document prints no such field at all.
            base_value = gross_total
        else:
            # Some invoices never print a "Taxable Amount"/"Subtotal" line -
            # fall back through the duplicate AMOUNTS field, then to the sum
            # of the line items, rather than reporting 0.0.
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
            ORDER_NUMBER=purchase_order.get("PO_NUMBER", ""),
            VENDOR_NAME=_first_value(supplier.get("NAME"), supplier.get("LEGAL_NAME"), supplier.get("TRADE_NAME")),
            VENDOR_GST_NO=supplier.get("GSTIN", ""),
            CUSTOMER_NAME=_first_value(customer.get("NAME"), customer.get("LEGAL_NAME")),
            CUSTOMER_GST_NO=_first_value(customer.get("GSTIN"), gst_compliance.get("CUSTOMER_GSTIN")),
            PLACE_OF_SUPPLY=_first_value(document.get("PLACE_OF_SUPPLY"), gst_compliance.get("PLACE_OF_SUPPLY")),
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
            WEIGHMENT_CHARGES=named_charges["WEIGHMENT_CHARGES"],
            PARKING_CHARGES=named_charges["PARKING_CHARGES"],
            LOADING_CHARGES=named_charges["LOADING_CHARGES"],
            UNLOADING_CHARGES=named_charges["UNLOADING_CHARGES"],
            EMPTY_UNLOADING_CHARGES=named_charges["EMPTY_UNLOADING_CHARGES"],
            DETENTION_CHARGES=named_charges["DETENTION_CHARGES"],
            DEMURRAGE_CHARGES=named_charges["DEMURRAGE_CHARGES"],
            VEHICLE_NUMBER=_first_value(logistics.get("VEHICLE_NUMBER"), delivery.get("VEHICLE_NUMBER")),
            ITEM_LIST=item_list,
        )

    @staticmethod
    def _build_item_list(data: dict) -> list["InvoiceItem"]:
        """ITEM_LIST is built only from the "ITEMS" array - the actual
        goods/service lines being billed (e.g. the freight/transportation
        charge itself on a GTA bill). Named add-on charges belong in the
        WEIGHMENT_CHARGES/PARKING_CHARGES/... header fields instead (see
        _build_named_charges) - the prompt asks the model to route these
        into ADDITIONAL_CHARGES specifically so they never land in ITEMS to
        begin with."""
        items = [InvoiceItem.from_dict(item) for item in data.get("ITEMS", [])]
        # The model occasionally echoes the schema's example ITEMS entry
        # (an all-blank template row, shown to illustrate the shape) as a
        # literal extra item alongside the real ones it found - drop any
        # item that carries no actual data, regardless of why it showed up,
        # rather than relying on a prompt instruction alone to prevent it.
        return [item for item in items if not _is_blank_value(asdict(item))]

    def to_dict(self) -> dict:
        """Every field is always present - SAP expects a fixed set of keys
        on every record, so missing text stays "" and missing numbers stay
        0 rather than being dropped."""
        return asdict(self)

    def to_json(self, **kwargs) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, **kwargs)


def is_blank_invoice(invoice: Invoice) -> bool:
    """True if none of the SAP-mapped fields carry a real value - the
    is_blank_result() check runs on the raw ~150-field extraction schema and
    can miss a page where the model fills in some unrelated field (e.g.
    METADATA.LANGUAGE, a DELIVERY date) while every field Invoice.from_dict()
    actually reads stays empty, producing a blank-but-not-flagged row here."""
    return _is_blank_value(invoice.to_dict())


def group_into_invoices(page_results: list[tuple[str, dict]]) -> list[Invoice]:
    """Turn a flat list of (source_id, extracted_dict) pairs into one Invoice
    per distinct INVOICE_NUMBER - pages that share an invoice number (e.g. a
    multi-page invoice) are merged into a single Invoice with combined
    ITEM_LIST; distinct invoice numbers each get their own Invoice. A single
    page can contribute more than one entry here (extract_receipt() returns
    one dict per invoice it finds on that page), which this function doesn't
    need to treat specially - each entry is just another item in the list.

    source_id identifies the originating PDF (e.g. its basename) and exists
    to resolve pages that never print their own invoice number - such as a
    tooling/annexure page appended after the main Tax Invoice page, which
    would otherwise become its own orphaned "__unknown_i" Invoice missing
    every header field (customer GSTIN, dates, PO number, ...). Any page
    with a blank INVOICE_NUMBER is folded into whichever invoice number was
    found elsewhere in the same source_id, resolved up front in a first
    pass so this works regardless of the order pages complete in (page
    processing runs concurrently, so a blank-numbered page can finish
    before the page carrying the real invoice number). Caveat: if a single
    source_id genuinely contains two or more distinct real invoices (not
    just one invoice plus its own blank-numbered annexure), a blank-numbered
    page from that source could be folded into the wrong one of them, since
    this fallback has no way to tell which invoice an unlabeled page belongs
    to beyond "first one found for this source" - a rare case, and no worse
    than leaving it an unmerged orphan.

    Some documents get scanned/photographed as more than one page for the
    same invoice number - e.g. both the government e-Invoice/IRP printout
    and the supplier's full letterhead Tax Invoice for the same sale - each
    showing the identical line items. DESCRIPTION is the field most likely
    to come back slightly different between two OCR passes of the same
    text (a misread digit, extra whitespace), so fingerprint each item by
    its numeric/code fields only (HSN, QTY, UNIT_PRICE, AMOUNT,
    VEHICLE_NUMBER) and skip an incoming item whose fingerprint repeats one
    already recorded for that invoice number - those fields are printed
    numbers/codes, not free text, and are far more likely to OCR
    identically across two scans of the same line. VEHICLE_NUMBER also
    guards the fleet-owner bill case where several rows share identical
    blank HSN/QTY/UNIT_PRICE and only differ by vehicle and amount. A
    genuinely distinct item with a coincidentally identical
    HSN/qty/price/amount/vehicle combination is rare enough to accept the
    risk,
    same tradeoff already made for the LOGISTICS/ADDITIONAL_CHARGES
    dedup above.

    The named charge fields (WEIGHMENT_CHARGES, PARKING_CHARGES, ...) are
    plain scalars, like IGST/CGST/SGST, so they need no special merge
    handling here - the generic backfill loop below already fills any of
    them still blank (0.0) on `existing` from a later page that has a value.

    A final pass then folds together any two invoices that share a
    source_id and have byte-identical BASE_VALUE and GROSS_TOTAL - see
    _merge_duplicate_totals() for why this is a reliable signal that the
    prompt-level "don't confuse invoice number with an internal reference
    number" instruction didn't catch (e.g. a vendor's real Tax Invoice
    Number on one page vs. a Billing No./voucher number describing the same
    sale on a companion accounting page)."""
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
            # Pages for the same invoice can finish in any order (page
            # processing runs concurrently), so whichever page happened to
            # be seen first may be the one with blank header fields (e.g.
            # an annexure page with no customer GSTIN of its own) - backfill
            # any still-blank header field on `existing` from this page
            # rather than assuming the first-seen page has the real data.
            for f in fields(Invoice):
                if f.name == "ITEM_LIST":
                    continue
                if getattr(existing, f.name) in ("", 0.0) and getattr(invoice, f.name) not in ("", 0.0):
                    setattr(existing, f.name, getattr(invoice, f.name))
            # Check every item in this incoming page against `seen` as it
            # stood *before* this page started merging, then fold in this
            # page's own fingerprints only once the whole page is done -
            # otherwise two genuinely distinct rows on the same page that
            # happen to share identical numbers (e.g. a "Type A" and "Type
            # B" tool set both priced/quantified the same) would collide
            # with each other and the second gets wrongly dropped as a
            # same-page "duplicate", when the fingerprint dedup is only
            # meant to catch a whole page re-scanned twice.
            seen = seen_items_by_number[key]
            incoming_fingerprints = set()
            for item in invoice.ITEM_LIST:
                fingerprint = (item.HSN, item.QTY, item.UNIT_PRICE, item.AMOUNT, item.VEHICLE_NUMBER)
                if fingerprint not in seen:
                    existing.ITEM_LIST.append(item)
                    incoming_fingerprints.add(fingerprint)
            seen.update(incoming_fingerprints)
        else:
            seen_items_by_number[key] = {
                (item.HSN, item.QTY, item.UNIT_PRICE, item.AMOUNT, item.VEHICLE_NUMBER) for item in invoice.ITEM_LIST
            }
            invoices_by_number[key] = invoice
            order.append(key)
    return _merge_duplicate_totals(
        [invoices_by_number[k] for k in order],
        [source_ids_by_number[k] for k in order],
    )


def _merge_duplicate_totals(invoices: list[Invoice], source_ids: list[set[str]]) -> list[Invoice]:
    """Fold together invoices that share a source PDF and have byte-identical
    BASE_VALUE and GROSS_TOTAL - two genuinely different invoices in the same
    PDF would not coincidentally match both totals to the cent, so a match
    here means the same underlying transaction was extracted twice under two
    different "invoice number" values (e.g. the vendor's real Tax Invoice
    Number on one page, and an internal Billing No./voucher number on a
    companion accounting page describing the same sale - a case the prompt
    asks the model to avoid, but can't guarantee against every time).

    Keeps whichever copy has more populated header fields (a proxy for which
    extraction is more reliable - in practice the wrong copy tends to be
    missing fields like IRN_NO/CUSTOMER_GST_NO, or to have VENDOR_GST_NO and
    CUSTOMER_GST_NO swapped), backfilling any field still blank on the kept
    copy from the discarded one. The discarded copy's ITEM_LIST is dropped
    entirely rather than merged in - its items are near-certainly the same
    line items already present on the kept copy under its own extraction."""
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
    """Returns (key, list-of-invoice-dicts) on success - usually one dict,
    more if the page shows multiple invoices - or (key, {"error": ...}) on
    failure. Callers distinguish the two by type: a dict means failure, a
    list means success (even an empty one, if extraction somehow yields no
    invoices)."""
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
    # ThreadPoolExecutor.map yields results in the same order as `pages`
    # (despite running concurrently), so zipping the two together safely
    # recovers which source PDF each result came from - needed by
    # group_into_invoices() to fold blank-invoice-number pages into the
    # right invoice.
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
