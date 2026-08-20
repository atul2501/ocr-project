import glob
import json
import logging
import os
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field, fields
from prompt import PROMPT
import httpx
import pymupdf
from api import (
    INVOICE_DIR,
    LOG_PATH,
    MAX_RETRIES,
    MAX_WORKERS,
    OUTPUT_PATH,
    RETRY_BACKOFF_BASE,
    RETRY_BACKOFF_CAP,
    RETRYABLE_STATUS_CODES,
    get_client,
)


logging.basicConfig(
    filename=LOG_PATH,
    filemode='a',
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
)
logger = logging.getLogger(__name__)

# Single process-wide executor. Reused across the CLI batch run (main()) and
# every API request (api.py) so concurrent OCR calls are always capped at
# MAX_WORKERS instead of each caller spinning up its own pool.
executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)


def _delete_source(client, source_id: str) -> None:
    try:
        client.delete_source(source_id)
    except httpx.HTTPError:
        logger.warning(f"failed to delete ChatPDF source {source_id}")


def split_pdf_pages(pdf_path: str) -> list[bytes]:
    """Split a PDF into its individual pages, each as its own small
    single-page PDF (a native page extraction via pymupdf, not a re-rendered
    image - so no quality loss and no risk of the file-size blowup a
    re-rendered/sharpened page caused earlier). Uploading and extracting
    each page independently, rather than the whole document in one ChatPDF
    call, is what makes a page-by-page bundle of unrelated invoices (e.g. a
    transporter's own invoice plus a sub-contractor's separate invoice for
    the same shipment, mixed in with delivery notes/weighbridge tickets)
    reliable: every page gets its own dedicated extraction call instead of
    one call trying to exhaustively enumerate every invoice in a long,
    mixed-content document, which testing showed is unreliable."""
    doc = pymupdf.open(pdf_path)
    pages = []
    for page_num in range(len(doc)):
        single_page = pymupdf.open()
        single_page.insert_pdf(doc, from_page=page_num, to_page=page_num)
        pages.append(single_page.tobytes())
        single_page.close()
    doc.close()
    return pages


def extract_invoices(pdf_bytes: bytes) -> list[dict]:
    """Returns one raw extraction dict per invoice found on this page -
    almost always a single-element list, but a page can genuinely show more
    than one separate invoice (e.g. two small bills photographed together),
    and the prompt asks the model to return one array entry per invoice in
    that case. Every entry is kept - none may be silently dropped.

    Uploads the page as a ChatPDF "source", asks it the extraction prompt
    as a single chat message, and deletes the source again afterwards -
    ChatPDF has no notion of a one-shot "analyze this file" call, only
    persistent sources you chat against, so the upload/delete is done
    around every extraction rather than reusing a source across calls.

    (An earlier version of this function also ran extra "verify which
    party is the vendor" follow-up questions and auto-swapped SUPPLIER/
    CUSTOMER on disagreement. Testing showed that verification wasn't more
    reliable than the main extraction - the same underlying model asked the
    same kind of question again, sometimes 2-out-of-3-majority-voting a
    correct answer into an incorrect one - so it was removed in favor of
    the deterministic keyword-based rule in Invoice.from_dict, which
    doesn't depend on the model's judgment at all.)"""
    client = get_client()
    source_id = client.add_source(pdf_bytes)
    try:
        text = client.chat(source_id, PROMPT)
    finally:
        # Cleanup doesn't need to block the response - fire it in the
        # background so the caller isn't stuck waiting on ChatPDF's delete
        # round-trip on top of the upload+chat call it actually needs.
        threading.Thread(target=_delete_source, args=(client, source_id), daemon=True).start()

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

    # Logged on every call (not just failures) so a result that parses fine
    # but still ends up filtered out downstream as "blank" (e.g. the model
    # set IS_TARGET_DOCUMENT false, or genuinely left every field empty) can
    # be diagnosed from process.log instead of being a silent [].
    logger.info(f"ChatPDF returned {len(parsed)} entrie(s): {json.dumps(parsed, ensure_ascii=False)}")
    return parsed


def extract_invoices_with_retry(pdf_bytes: bytes) -> list[dict]:
    last_exc = None
    for attempt in range(1, MAX_RETRIES + 2):
        try:
            return extract_invoices(pdf_bytes)
        except httpx.HTTPStatusError as e:
            if e.response.status_code not in RETRYABLE_STATUS_CODES:
                raise  # e.g. 404 source-not-found, 403 invalid-key: never succeeds
            last_exc = e
            logger.warning(f"attempt {attempt} failed (status {e.response.status_code}): {e}")
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


_TRUTHY_STRINGS = {"true", "yes", "y", "1"}


def is_blank_result(result: dict) -> bool:
    """True if extraction found nothing usable - e.g. the page isn't a Tax
    Invoice/PO (blank page, cover sheet, etc.) or the model echoed the empty
    template back."""
    is_target = str(result.get("METADATA", {}).get("IS_TARGET_DOCUMENT", "")).strip().lower()
    if is_target and is_target not in _TRUTHY_STRINGS:
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


# Named logistics/freight-related charges that each become their own row in
# ITEM_LIST (e.g. "Weighment Charges") rather than a plain goods/service
# line. Order matters when matching an ADDITIONAL_CHARGES description below:
# most specific phrase first, since "LOADING" is itself a substring of
# "UNLOADING", which is itself a substring of "EMPTY UNLOADING" - checking
# in this order (and stopping at the first match per charge) means a
# genuine "EMPTY UNLOADING" charge can never be misclassified as a plain
# "LOADING" one. FREIGHT_AMOUNT has no field here: freight is the primary
# billed service on a GTA bill, so it's already carried in ITEMS/ITEM_LIST,
# and including it here too would double it up.
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
    """Cross-page merge dedup key for one ITEM_LIST row (see
    group_into_invoices). For a normal goods/service row (from ITEMS),
    DESCRIPTION is excluded - it's the field most likely to come back
    slightly different between two OCR passes of the same printed line, so
    matching is done on the numeric/code fields alone (HSN, QTY, UNIT_PRICE,
    AMOUNT, VEHICLE_NUMBER). A row built by _build_charge_items (a named
    logistics charge like "Weighment Charges") always has blank
    HSN/QTY/UNIT_PRICE/VEHICLE_NUMBER, so without DESCRIPTION two different
    charge types that happen to carry the same AMOUNT (e.g. Parking=500 and
    Loading=500) would fingerprint identically and one would be wrongly
    dropped as a duplicate. DESCRIPTION is safe to include for these rows
    specifically because it's generated by code (a fixed label per charge
    type), not OCR'd, so it carries none of the variance the exclusion
    exists to tolerate."""
    is_charge_row = not item.HSN and item.QTY == 0.0 and item.UNIT_PRICE == 0.0 and not item.VEHICLE_NUMBER
    if is_charge_row:
        return (item.DESCRIPTION, item.AMOUNT)
    return (item.HSN, item.QTY, item.UNIT_PRICE, item.AMOUNT, item.VEHICLE_NUMBER)


# Industry keywords that reliably identify the transport/logistics
# business on a GTA/freight bill - not tied to any specific company, just
# to the nature of a transporter's own trading name.
_VENDOR_KEYWORDS = ("TRANSPORT", "LOGISTICS", "FLEET", "SHIPPING", "CARRIER", "FORWARDING", "CARGO")


def _has_vendor_keyword(name: str) -> bool:
    name = (name or "").upper()
    return any(keyword in name for keyword in _VENDOR_KEYWORDS)


def _resolve_supplier_customer(supplier: dict, customer: dict) -> tuple[dict, dict]:
    """Deterministic override for which raw dict is actually SUPPLIER
    (vendor) vs CUSTOMER: on a GTA/transporter bill, the vendor is reliably
    the party whose name/legal name/trade name contains a transport-industry
    keyword (see _VENDOR_KEYWORDS) - this held across every test invoice
    regardless of which specific companies were involved, and is far more
    reliable than the model's own vendor/customer judgment, which testing
    showed gets this wrong on a large fraction of documents even under a
    2-out-of-3 majority vote across independent re-checks (an AI "verifier"
    is subject to the same confusion as the original extraction, not a more
    authoritative second opinion). Only overrides when exactly one party's
    name matches a keyword - if both or neither do, this can't disambiguate
    and the model's own assignment is left untouched."""
    supplier_name = " ".join(supplier.get(k, "") for k in ("NAME", "LEGAL_NAME", "TRADE_NAME"))
    customer_name = " ".join(customer.get(k, "") for k in ("NAME", "LEGAL_NAME", "TRADE_NAME"))
    if _has_vendor_keyword(customer_name) and not _has_vendor_keyword(supplier_name):
        return customer, supplier
    return supplier, customer


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


def _fix_swapped_gstins(supplier: dict, customer: dict) -> tuple[dict, dict]:
    """Deterministic cross-check, distinct from _resolve_supplier_customer:
    a GSTIN mechanically embeds its own PAN (see _pan_from_gstin), so if a
    party's own extracted PAN doesn't match the PAN embedded in that same
    party's extracted GSTIN, but DOES match the PAN embedded in the OTHER
    party's GSTIN, the two GSTIN values were swapped between blocks even
    though the party NAMES were already assigned correctly - a real
    failure mode observed even after the name-based fix (e.g. the model
    correctly wrote "Mahalaxmi Transport" as SUPPLIER and correctly read
    its PAN, but filed the customer's GSTIN under SUPPLIER.GSTIN instead
    of its own). Swaps just the two GSTIN values back when this is
    unambiguous; leaves both untouched otherwise - it can only fire when at
    least one party's PAN was independently extracted and is a clean match
    for the other party's GSTIN, so it never guesses."""
    supplier_gstin = supplier.get("GSTIN", "")
    customer_gstin = customer.get("GSTIN", "")
    if not supplier_gstin or not customer_gstin or supplier_gstin == customer_gstin:
        return supplier, customer

    supplier_pan = supplier.get("PAN", "")
    customer_pan = customer.get("PAN", "")
    supplier_embedded_pan = _pan_from_gstin(supplier_gstin)
    customer_embedded_pan = _pan_from_gstin(customer_gstin)

    supplier_pan_points_to_customer_gstin = (
        supplier_pan and supplier_pan == customer_embedded_pan and supplier_pan != supplier_embedded_pan
    )
    customer_pan_points_to_supplier_gstin = (
        customer_pan and customer_pan == supplier_embedded_pan and customer_pan != customer_embedded_pan
    )

    if supplier_pan_points_to_customer_gstin or customer_pan_points_to_supplier_gstin:
        supplier = {**supplier, "GSTIN": customer_gstin}
        customer = {**customer, "GSTIN": supplier_gstin}
        logger.info(
            f"GSTIN swap corrected via embedded-PAN cross-check: supplier GSTIN is now {customer_gstin}, "
            f"customer GSTIN is now {supplier_gstin}"
        )

    return supplier, customer


_PLACE_OF_SUPPLY_PATTERN = re.compile(r"^\s*(\d{1,2})\s*[-–:]?\s*(.*)$")

# Official India GST state/UT codes - fixed by law as the first two digits
# of every GSTIN, so this table never changes. Used only as a last-resort
# fallback in _split_place_of_supply when the document never prints a
# "Place of Supply" label at all: for ordinary domestic B2B supply, Place
# of Supply defaults to the recipient's own registered state, which the
# customer's own GSTIN already encodes mechanically - the same kind of
# deterministic GSTIN-format derivation as _pan_from_gstin, not a guess
# from an address or route field the way the prompt forbids the model
# itself from doing.
_GST_STATE_CODES = {
    "01": "JAMMU AND KASHMIR",
    "02": "HIMACHAL PRADESH",
    "03": "PUNJAB",
    "04": "CHANDIGARH",
    "05": "UTTARAKHAND",
    "06": "HARYANA",
    "07": "DELHI",
    "08": "RAJASTHAN",
    "09": "UTTAR PRADESH",
    "10": "BIHAR",
    "11": "SIKKIM",
    "12": "ARUNACHAL PRADESH",
    "13": "NAGALAND",
    "14": "MANIPUR",
    "15": "MIZORAM",
    "16": "TRIPURA",
    "17": "MEGHALAYA",
    "18": "ASSAM",
    "19": "WEST BENGAL",
    "20": "JHARKHAND",
    "21": "ODISHA",
    "22": "CHHATTISGARH",
    "23": "MADHYA PRADESH",
    "24": "GUJARAT",
    "25": "DAMAN AND DIU",
    "26": "DADRA AND NAGAR HAVELI AND DAMAN AND DIU",
    "27": "MAHARASHTRA",
    "28": "ANDHRA PRADESH (OLD)",
    "29": "KARNATAKA",
    "30": "GOA",
    "31": "LAKSHADWEEP",
    "32": "KERALA",
    "33": "TAMIL NADU",
    "34": "PUDUCHERRY",
    "35": "ANDAMAN AND NICOBAR ISLANDS",
    "36": "TELANGANA",
    "37": "ANDHRA PRADESH",
    "38": "LADAKH",
    "97": "OTHER TERRITORY",
}


def _split_place_of_supply(document: dict, gst_compliance: dict, customer_gstin: str) -> tuple[str, str]:
    """Split the combined "27- MAHARASHTRA"-style Place of Supply value the
    model returns into its numeric GST state code and state name - SAP
    wants these as two separate fields (PLACE_OF_SUPPLY_CODE,
    PLACE_OF_SUPPLY_STATE). GST_COMPLIANCE has its own dedicated
    PLACE_OF_SUPPLY_STATE_CODE raw field - prefer that for the code when the
    model filled it in directly (unambiguous), otherwise fall back to the
    leading digits parsed off the combined text. There's no dedicated raw
    field for the state name alone, so it's always parsed out of the
    combined DOCUMENT.PLACE_OF_SUPPLY / GST_COMPLIANCE.PLACE_OF_SUPPLY text,
    which is almost always printed as "<code>-<state name>" or "<code> -
    <state name>".

    If the page never prints a Place of Supply at all, falls back to the
    customer's own registered state (see _GST_STATE_CODES above) rather
    than leaving both fields blank - this matches GST law's default rule
    for ordinary domestic supply and is safe because it's read off the
    customer's own printed GSTIN, not inferred from an address/route
    field."""
    raw = _first_value(document.get("PLACE_OF_SUPPLY"), gst_compliance.get("PLACE_OF_SUPPLY"))
    match = _PLACE_OF_SUPPLY_PATTERN.match(raw) if raw else None
    parsed_code = match.group(1) if match else ""
    parsed_state = match.group(2).strip() if match else raw.strip() if raw else ""
    code = _first_value(gst_compliance.get("PLACE_OF_SUPPLY_STATE_CODE"), parsed_code)

    if not code:
        gstin_code = (customer_gstin or "").strip()[:2]
        if gstin_code in _GST_STATE_CODES:
            code = gstin_code

    # The state name is uniquely determined by the code under GST law - once
    # a code is known (whether printed directly, parsed off the combined
    # text, or derived from the customer's GSTIN), trust the lookup table
    # over whatever free text happened to be parsed alongside it. That text
    # can be a city/area name the model picked up from a route field rather
    # than the actual state (e.g. "Khopoli" instead of "Maharashtra"), even
    # when the code itself came out correct.
    state = _GST_STATE_CODES.get(code, parsed_state)

    return code, state


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


def _named_charge_amounts(data: dict) -> dict[str, float]:
    """Map each of the fixed named logistics/freight charge types (see
    _NAMED_CHARGE_FIELDS) to its amount. Prefers an explicit LOGISTICS.<field>
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


def _build_charge_items(data: dict) -> list["InvoiceItem"]:
    """Build one ITEM_LIST row per named logistics/freight charge type (see
    _NAMED_CHARGE_FIELDS) that carries a non-zero amount - e.g. a
    "Weighment Charges" row alongside the billed goods/service, matching how
    SAP itself lines up every charge as its own item rather than a
    header-level surcharge field."""
    charges = _named_charge_amounts(data)
    return [
        InvoiceItem(DESCRIPTION=name.replace("_", " ").title(), AMOUNT=amount)
        for name, _ in _NAMED_CHARGE_FIELDS
        if (amount := charges[name])
    ]


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
        description = item.get("DESCRIPTION", "")
        vehicle_number = item.get("VEHICLE_NUMBER", "")
        hsn = item.get("HSN_CODE", "")
        if vehicle_number and not hsn and "freight" not in description.lower():
            # A GTA/transporter bill's primary ITEMS row is the freight
            # charge for that trip, but the document itself very often
            # never prints the word "Freight" - it describes the shipment
            # instead (e.g. "20' Import Fixed", "40 Import / TEMU7220511").
            # A per-row VEHICLE_NUMBER with no HSN is a reliable signal this
            # is a transport row rather than a distinct HSN-coded product,
            # so make the freight nature explicit rather than leaving only
            # the container/movement text the model kept verbatim.
            description = f"Freight Charges - {description}" if description else "Freight Charges"
        return cls(
            DESCRIPTION=description,
            HSN=hsn,
            QTY=_to_float(item.get("QUANTITY", "")),
            UOM=item.get("UOM", ""),
            WEIGHT=_to_float(item.get("WEIGHT", "")),
            WEIGHT_UNIT=item.get("WEIGHT_UNIT", ""),
            UNIT_PRICE=_to_float(item.get("UNIT_PRICE", "")),
            AMOUNT=_to_float(item.get("LINE_TOTAL", "")),
            VEHICLE_NUMBER=vehicle_number,
        )


@dataclass
class Invoice:
    """SAP-mapped invoice, built from one extracted invoice dict (the
    "DOCUMENT"/"SUPPLIER"/"TAX"/... sections returned by extract_invoices)."""
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
        supplier, customer = _resolve_supplier_customer(data.get("SUPPLIER", {}), data.get("CUSTOMER", {}))
        supplier, customer = _fix_swapped_gstins(supplier, customer)
        purchase_order = data.get("PURCHASE_ORDER", {})
        tax = data.get("TAX", {})
        amounts = data.get("AMOUNTS", {})
        gst_compliance = data.get("GST_COMPLIANCE", {})
        logistics = data.get("LOGISTICS", {})
        delivery = data.get("DELIVERY", {})

        item_list = cls._build_item_list(data)
        item_list.extend(_build_charge_items(data))
        customer_gst_no = _first_value(customer.get("GSTIN"), gst_compliance.get("CUSTOMER_GSTIN"))
        place_of_supply_code, place_of_supply_state = _split_place_of_supply(document, gst_compliance, customer_gst_no)
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
            ORDER_NUMBER=_first_value(
                purchase_order.get("PO_NUMBER"),
                purchase_order.get("PURCHASE_ORDER_REFERENCE"),
                document.get("INVOICE_REFERENCE_NUMBER"),
            ),
            VENDOR_NAME=_first_value(supplier.get("NAME"), supplier.get("LEGAL_NAME"), supplier.get("TRADE_NAME")),
            VENDOR_GST_NO=supplier.get("GSTIN", ""),
            CUSTOMER_NAME=_first_value(customer.get("NAME"), customer.get("LEGAL_NAME")),
            CUSTOMER_GST_NO=customer_gst_no,
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
            CUSTOMER_PAN_NO=_first_value(customer.get("PAN"), _pan_from_gstin(customer_gst_no)),
            VEHICLE_NUMBER=_first_value(logistics.get("VEHICLE_NUMBER"), delivery.get("VEHICLE_NUMBER")),
            ITEM_LIST=item_list,
        )

    @staticmethod
    def _build_item_list(data: dict) -> list["InvoiceItem"]:
        """ITEM_LIST is built only from the "ITEMS" array - the actual
        goods/service lines being billed (e.g. the freight/transportation
        charge itself on a GTA bill). Named add-on charges are appended to
        ITEM_LIST separately, as their own rows (see _build_charge_items) -
        the prompt asks the model to route these into ADDITIONAL_CHARGES
        specifically so they never land in ITEMS to begin with, and are
        added back in as ITEM_LIST rows afterwards rather than being read
        from ITEMS here."""
        items = [InvoiceItem.from_dict(item) for item in data.get("ITEMS", [])]
        # The model occasionally echoes the schema's example ITEMS entry
        # (an all-blank template row, shown to illustrate the shape) as a
        # literal extra item alongside the real ones it found - drop any
        # item that carries no actual data, regardless of why it showed up,
        # rather than relying on a prompt instruction alone to prevent it.
        items = [item for item in items if not _is_blank_value(asdict(item))]

        # Not auto-corrected: a fleet-owner bill can legitimately reuse the
        # same vehicle across several trips, so there's no way to tell that
        # apart from row 1's VEHICLE_NUMBER having been wrongly copied onto
        # later rows (an observed OCR failure mode) without re-reading the
        # page - guessing which case this is risks destroying correct data.
        # Logged instead so it surfaces in process.log for manual review.
        vehicle_amounts: dict[str, set] = {}
        for item in items:
            if item.VEHICLE_NUMBER:
                vehicle_amounts.setdefault(item.VEHICLE_NUMBER, set()).add(item.AMOUNT)
        for vehicle, amounts_seen in vehicle_amounts.items():
            if len(amounts_seen) > 1:
                logger.warning(
                    f"VEHICLE_NUMBER '{vehicle}' repeated across ITEMS rows with "
                    f"different amounts {sorted(amounts_seen)} - possible row-binding "
                    f"error (or a genuinely reused vehicle); verify manually"
                )

        return items

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
    per distinct INVOICE_NUMBER - entries that share an invoice number (e.g.
    a multi-page invoice the model didn't fully merge itself) are merged into
    a single Invoice with combined ITEM_LIST; distinct invoice numbers each
    get their own Invoice. A single document can contribute more than one
    entry here (extract_invoices() returns one dict per invoice it finds in
    the document), which this function doesn't need to treat specially - each
    entry is just another item in the list.

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
    text (a misread digit, extra whitespace), so a goods/service item (from
    ITEMS) is fingerprinted by its numeric/code fields only (HSN, QTY,
    UNIT_PRICE, AMOUNT, VEHICLE_NUMBER) and an incoming item whose
    fingerprint repeats one already recorded for that invoice number is
    skipped - those fields are printed numbers/codes, not free text, and
    are far more likely to OCR identically across two scans of the same
    line. VEHICLE_NUMBER also guards the fleet-owner bill case where
    several rows share identical blank HSN/QTY/UNIT_PRICE and only differ
    by vehicle and amount. A genuinely distinct item with a coincidentally
    identical HSN/qty/price/amount/vehicle combination is rare enough to
    accept the risk, same tradeoff already made for the LOGISTICS/
    ADDITIONAL_CHARGES dedup above. See _item_fingerprint() for the exact
    tuple used, including the DESCRIPTION-inclusive variant for
    code-generated charge rows.

    The named logistics/freight charges (weighment, parking, ...) are now
    ITEM_LIST rows rather than header scalars, so they need no special merge
    handling here either - they go through the same item fingerprint dedup
    as any other ITEM_LIST row above (_item_fingerprint), which includes
    DESCRIPTION for these specific rows so that two different charge types
    sharing the same amount don't collide.

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
            # The model can still emit two separate entries for the same
            # invoice (e.g. it didn't fully merge a multi-page invoice
            # itself, or two documents genuinely reference the same invoice
            # number), and whichever entry happened to come first may be the
            # one with blank header fields (e.g. an annexure page with no
            # customer GSTIN of its own) - backfill any still-blank header
            # field on `existing` from this entry rather than assuming the
            # first-seen one has the real data.
            for f in fields(Invoice):
                if f.name == "ITEM_LIST":
                    continue
                if getattr(existing, f.name) in ("", 0.0) and getattr(invoice, f.name) not in ("", 0.0):
                    setattr(existing, f.name, getattr(invoice, f.name))
            # Check every item in this incoming entry against `seen` as it
            # stood *before* this entry started merging, then fold in this
            # entry's own fingerprints only once it's done - otherwise two
            # genuinely distinct rows in the same entry that happen to share
            # identical numbers (e.g. a "Type A" and "Type B" tool set both
            # priced/quantified the same) would collide with each other and
            # the second gets wrongly dropped as a duplicate, when the
            # fingerprint dedup is only meant to catch the same invoice
            # content reported twice.
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


def process_page(pdf_path: str, page_num: int, page_bytes: bytes) -> tuple[str, list[dict] | dict]:
    """Returns (key, list-of-invoice-dicts) on success - usually one dict,
    more if the page shows multiple invoices - or (key, {"error": ...}) on
    failure. Callers distinguish the two by type: a dict means failure, a
    list means success (even an empty one)."""
    key = f"{os.path.basename(pdf_path)}#page{page_num}"
    try:
        return key, extract_invoices_with_retry(page_bytes)
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
            for page_num, page_bytes in enumerate(split_pdf_pages(pdf_path), start=1):
                pages.append((pdf_path, page_num, page_bytes))
        except Exception:
            # A corrupt/unreadable PDF shouldn't take the whole batch down;
            # log it and keep going with the rest of the files.
            logger.exception(f"failed to split {os.path.basename(pdf_path)}")

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


if __name__ == '__main__':
    try:
        main()
    except Exception:
        # Catch-all so any crash (bad INVOICE_DIR, disk full, etc.) is
        # recorded in process.log with a full traceback, not just printed
        # to the console and lost.
        logger.exception("main() crashed")
        raise
