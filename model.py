import glob
import json
import logging
import os
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field, fields
from prompt import BLANK_CHUNKS, CHUNK_KEYS, PROMPT_CHUNKS, RECALL_CHECK_PROMPT, blank_invoice
import httpx
from api import (
    CHATPDF_ADD_FILE_URL,
    CHATPDF_API_KEY,
    CHATPDF_DELETE_URL,
    CHATPDF_MESSAGE_URL,
    INVOICE_DIR,
    LOG_PATH,
    MAX_RETRIES,
    MAX_WORKERS,
    OUTPUT_PATH,
    RETRY_BACKOFF_BASE,
    RETRY_BACKOFF_CAP,
    RETRYABLE_STATUS_CODES,
)


logging.basicConfig(
    filename=LOG_PATH,
    filemode='a',
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
)
logger = logging.getLogger(__name__)

# Single process-wide executor. Reused across the CLI batch run (main()) and
# every API request (api.py) so concurrent ChatPDF calls are always capped at
# MAX_WORKERS instead of each caller spinning up its own pool.
executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)

_CHATPDF_HEADERS = {'x-api-key': CHATPDF_API_KEY}


def _chatpdf_ask(source_id: str, prompt_text: str) -> list[dict]:
    response = httpx.post(
        CHATPDF_MESSAGE_URL,
        headers=_CHATPDF_HEADERS,
        json={'sourceId': source_id, 'messages': [{'role': 'user', 'content': prompt_text}]},
        timeout=120,
    )
    response.raise_for_status()
    text = response.json()['content']

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


def _is_target_document(value) -> bool | None:
    """Interpret METADATA.IS_TARGET_DOCUMENT loosely - despite the prompt
    asking for the literal string "true"/"false", the model sometimes
    answers "Yes"/"No" instead. Returns True/False when the value clearly
    means yes/no, None when blank or unrecognized (treated as "unknown",
    not as "no" - callers fall back to inspecting the extracted fields)."""
    normalized = str(value).strip().lower()
    if normalized in ("true", "yes", "y", "1"):
        return True
    if normalized in ("false", "no", "n", "0"):
        return False
    return None


def _chunk_marks_not_target(invoice: dict) -> bool:
    return _is_target_document(invoice.get("METADATA", {}).get("IS_TARGET_DOCUMENT", "")) is False


def _recall_check(source_id: str) -> list[dict]:
    """Lightweight scan for every distinct invoice number/GSTIN anywhere in
    the document (see RECALL_CHECK_PROMPT) - used to catch a secondary
    invoice (e.g. a bundled sub-contractor's own bill) that chunk 0's full
    extraction pass missed as its own array entry, a demonstrated gap on
    long bundled PDFs. Best-effort: any failure here just means no
    recall-driven follow-up happens - it never fails the whole request,
    since the primary extraction already succeeded without it."""
    try:
        response = httpx.post(
            CHATPDF_MESSAGE_URL,
            headers=_CHATPDF_HEADERS,
            json={'sourceId': source_id, 'messages': [{'role': 'user', 'content': RECALL_CHECK_PROMPT}]},
            timeout=120,
        )
        response.raise_for_status()
        text = response.json()['content']
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r'\[.*\]', text, re.DOTALL)
            if not match:
                return []
            parsed = json.loads(match.group(0))
        return [p for p in parsed if isinstance(p, dict)] if isinstance(parsed, list) else []
    except (httpx.HTTPStatusError, httpx.RequestError, ValueError, json.JSONDecodeError) as e:
        logger.warning(f"recall check failed: {e}")
        return []


def _normalize_invoice_number(value) -> str:
    """Punctuation/case-insensitive comparison key for an invoice number -
    two separate ChatPDF calls reading the same printed number (e.g. chunk 0
    and the recall check) can differ on incidental formatting like a
    leading "#" or a "/" vs "-" without disagreeing on the number itself."""
    return re.sub(r'[^A-Z0-9]', '', str(value).upper())


def _find_missing_invoices(invoices: list[dict], candidates: list[dict]) -> list[dict]:
    """Recall candidates (see _recall_check) not already covered by
    `invoices`, matched by invoice number alone (normalized). Matching also
    on supplier GSTIN was tried as an extra guard against invoice-number
    transcription noise, but the recall check has been observed to
    mis-transcribe a second invoice's own GSTIN as the first invoice's
    instead - matching on that field risked silently swallowing a genuinely
    different invoice the recall check DID correctly notice, which is
    exactly the failure this whole check exists to catch."""
    known_numbers = {
        _normalize_invoice_number(inv.get("DOCUMENT", {}).get("INVOICE_NUMBER", "")) for inv in invoices
    }
    known_numbers.discard("")

    missing = []
    seen = set()
    for candidate in candidates:
        number = str(candidate.get("INVOICE_NUMBER", "")).strip()
        normalized = _normalize_invoice_number(number)
        if not normalized or normalized in known_numbers or normalized in seen:
            continue
        seen.add(normalized)
        missing.append(candidate)
    return missing


def _targeted_prompts(invoice_number: str) -> list[str]:
    # Deliberately doesn't include a GSTIN hint alongside invoice_number -
    # the recall check has been observed to mis-transcribe a secondary
    # invoice's own GSTIN as the primary invoice's instead (see
    # _find_missing_invoices), and passing that wrong value here as
    # targeting context measurably misled the targeted chunk 0 call into
    # filling both VENDOR_GST_NO and CUSTOMER_GST_NO with it. invoice_number
    # alone has been reliable across every observed case.
    prefix = (
        f'TARGET INVOICE ONLY: extract just the invoice numbered "{invoice_number}" - '
        'ignore every other invoice or document in this file.\n\n'
    )
    return [prefix + chunk for chunk in PROMPT_CHUNKS]


# Index into PROMPT_CHUNKS/CHUNK_KEYS/BLANK_CHUNKS for the ITEMS/
# ADDITIONAL_CHARGES chunk - skipped in a targeted secondary-invoice pass
# (see _extract_targeted_invoice) since it has been observed to
# consistently cross-contaminate a secondary invoice with the PRIMARY
# invoice's own item rows instead of recognizing the secondary invoice
# simply has none of its own (e.g. a simpler receipt-style invoice with
# just a subtotal and no itemized breakdown at all).
_ITEMS_CHUNK_INDEX = 1


def _extract_targeted_invoice(source_id: str, invoice_number: str) -> dict | None:
    """Runs the same chunked extraction as the main pass (see
    extract_receipt), scoped to one specific invoice the recall check found
    and chunk 0 missed. Returns None if the targeted pass itself can't find
    or confirm that invoice - best-effort, doesn't fail the whole request.

    Deliberately skips the ITEMS chunk (see _ITEMS_CHUNK_INDEX) - leaving
    ITEM_LIST blank is safer than confidently wrong data borrowed from a
    different invoice on the same PDF."""
    prompts = _targeted_prompts(invoice_number)
    try:
        invoices = _chatpdf_ask(source_id, prompts[0])
    except (httpx.HTTPStatusError, httpx.RequestError, ValueError) as e:
        logger.warning(f"targeted extraction for invoice {invoice_number!r} failed: {e}")
        return None
    if all(_chunk_marks_not_target(inv) for inv in invoices):
        return None

    invoice = invoices[0]
    invoice.update(BLANK_CHUNKS[_ITEMS_CHUNK_INDEX - 1])
    remaining_indices = [i for i in range(1, len(prompts)) if i != _ITEMS_CHUNK_INDEX]
    with ThreadPoolExecutor(max_workers=len(remaining_indices)) as pool:
        futures = {i: pool.submit(_chatpdf_ask, source_id, prompts[i]) for i in remaining_indices}
        for chunk_index, future in futures.items():
            try:
                chunk_result = future.result()
            except (httpx.HTTPStatusError, httpx.RequestError, ValueError):
                chunk_result = []
            keys = CHUNK_KEYS[chunk_index]
            blank = BLANK_CHUNKS[chunk_index - 1]
            source = chunk_result[0] if chunk_result else blank
            for key in keys:
                invoice[key] = source.get(key, blank[key])
    return invoice


def _fix_swapped_party_gstin(invoice: dict, known_invoices: list[dict]) -> None:
    """A targeted secondary-invoice extraction (see _extract_targeted_invoice)
    has been observed to swap SUPPLIER.GSTIN and CUSTOMER.GSTIN when a
    party appears as the SUPPLIER on one invoice in this PDF and the
    CUSTOMER on another (e.g. an intermediary logistics company that both
    bills the primary customer and is itself billed by a sub-contractor) -
    the model gets the party NAMEs right but attaches the wrong GSTIN to
    each. Detectable and correctable using data already reliably extracted
    from another invoice in the same PDF, not a guess: if this invoice's
    own SUPPLIER.GSTIN exactly matches a DIFFERENT already-known invoice's
    own supplier GSTIN, but that known invoice's supplier NAME matches
    THIS invoice's CUSTOMER (not its SUPPLIER), the two GSTINs were
    swapped - put them back. PAN, which is derived from GSTIN downstream in
    Invoice.from_dict whenever the raw PAN field is blank, self-corrects
    once this runs before that derivation."""
    supplier = invoice.get("SUPPLIER", {})
    customer = invoice.get("CUSTOMER", {})
    supplier_gstin = str(supplier.get("GSTIN", "")).strip().upper()
    customer_name = str(customer.get("NAME", "")).strip().upper()
    if not supplier_gstin or not customer_name:
        return

    for known in known_invoices:
        known_supplier = known.get("SUPPLIER", {})
        known_gstin = str(known_supplier.get("GSTIN", "")).strip().upper()
        known_name = str(known_supplier.get("NAME", "")).strip().upper()
        if known_gstin and known_gstin == supplier_gstin and known_name and known_name == customer_name:
            supplier["GSTIN"], customer["GSTIN"] = customer.get("GSTIN", ""), supplier["GSTIN"]
            return


def _fix_swapped_gstin_by_pan(invoice: dict) -> None:
    """SUPPLIER.GSTIN and CUSTOMER.GSTIN have been observed swapped even on
    a plain, single-invoice extraction with no second invoice to cross-
    check against (see _fix_swapped_party_gstin for that case) - e.g. a
    document that prints the billed-to party's GSTIN prominently near the
    top (under an "M/S:"-style header) and the vendor's own GSTIN
    separately near the bottom signature block, a layout that has misled
    the model into pairing the first GSTIN it reads with whichever company
    name comes first, rather than by actual role.

    A GSTIN deterministically embeds its holder's 10-character PAN in
    characters 3-12 (see _pan_from_gstin). If a directly-read SUPPLIER.PAN
    doesn't match the PAN embedded in SUPPLIER.GSTIN, but DOES match the
    PAN embedded in CUSTOMER.GSTIN (or the same the other way around for
    CUSTOMER.PAN), the two GSTINs were swapped - correct by swapping them
    back. This is a self-contained cross-check against already-extracted
    data, not a guess, and only fires when the evidence is unambiguous."""
    supplier = invoice.get("SUPPLIER", {})
    customer = invoice.get("CUSTOMER", {})
    supplier_gstin = str(supplier.get("GSTIN", "")).strip()
    customer_gstin = str(customer.get("GSTIN", "")).strip()
    if not supplier_gstin or not customer_gstin or supplier_gstin.upper() == customer_gstin.upper():
        return

    supplier_gstin_pan = _pan_from_gstin(supplier_gstin)
    customer_gstin_pan = _pan_from_gstin(customer_gstin)
    if not supplier_gstin_pan or not customer_gstin_pan:
        return

    supplier_pan = str(supplier.get("PAN", "")).strip().upper()
    customer_pan = str(customer.get("PAN", "")).strip().upper()
    swap_evidence = (
        (supplier_pan and supplier_pan == customer_gstin_pan and supplier_pan != supplier_gstin_pan)
        or (customer_pan and customer_pan == supplier_gstin_pan and customer_pan != customer_gstin_pan)
    )
    if swap_evidence:
        supplier["GSTIN"], customer["GSTIN"] = customer_gstin, supplier_gstin


def extract_receipt(pdf_path: str) -> list[dict]:
    """Returns one raw extraction dict per invoice found in this PDF -
    almost always a single-element list, but a multi-page or bundled PDF can
    genuinely contain more than one separate invoice, and the prompt asks
    the model to return one array entry per invoice in that case. Every
    entry is kept - none may be silently dropped.

    ChatPDF reads the PDF itself (no local rendering needed), but caps every
    chat message (and its response) at ~2500 tokens, and the full ~150-field
    extraction schema doesn't fit in one round trip - so PROMPT_CHUNKS
    splits it into several chats/message calls against the same uploaded
    file, merged back together by invoice index. This keeps the final JSON
    shape identical to a single-call extraction, it just takes more than
    one request to fill in.

    A lightweight recall check (_recall_check) runs alongside the main
    chunks and catches a secondary invoice from a different vendor buried
    deep in a long bundled PDF that chunk 0's own full extraction can miss
    entirely - any invoice it finds that isn't already accounted for gets a
    targeted follow-up pass (_extract_targeted_invoice) rather than being
    silently dropped."""
    with open(pdf_path, 'rb') as f:
        add_response = httpx.post(
            CHATPDF_ADD_FILE_URL,
            headers=_CHATPDF_HEADERS,
            files={'file': (os.path.basename(pdf_path), f, 'application/pdf')},
            timeout=60,
        )
    if add_response.status_code == 400:
        # ChatPDF rejects some PDFs outright ("Could not read PDF", e.g. a
        # scanned page that's almost entirely blank) instead of returning an
        # empty extraction - treat that the same as a document with nothing
        # readable in it rather than failing the whole request.
        logger.warning(f"ChatPDF could not read {pdf_path}: {add_response.text}")
        return [blank_invoice()]
    add_response.raise_for_status()
    source_id = add_response.json()['sourceId']

    try:
        # All chunks (plus the recall check) are independent questions
        # against the same already-uploaded, immutable source (none builds
        # on another's answer), so fire them all concurrently rather than
        # waiting on chunk 0 before sending the rest - each chats/message
        # call alone can take 10-20s, and this is the biggest single lever
        # on end-to-end latency. The tradeoff: a document chunk 0 turns out
        # to mark not a target document still costs the other calls (they
        # were already in flight), where waiting on chunk 0 first would
        # have skipped them - accepted in exchange for consistently fast
        # extraction on real invoices, the common case. A small ad-hoc pool
        # (not the shared process-wide `executor`) avoids any risk of
        # deadlocking against that pool's own MAX_WORKERS cap.
        with ThreadPoolExecutor(max_workers=len(PROMPT_CHUNKS) + 1) as pool:
            futures = [pool.submit(_chatpdf_ask, source_id, prompt) for prompt in PROMPT_CHUNKS]
            recall_future = pool.submit(_recall_check, source_id)
            invoices = futures[0].result()

            if all(_chunk_marks_not_target(inv) for inv in invoices):
                for inv in invoices:
                    for blank in BLANK_CHUNKS:
                        inv.update(blank)
                return invoices

            for chunk_index in range(1, len(PROMPT_CHUNKS)):
                chunk_result = futures[chunk_index].result()
                keys = CHUNK_KEYS[chunk_index]
                blank = BLANK_CHUNKS[chunk_index - 1]
                for i, inv in enumerate(invoices):
                    # A chunk occasionally disagrees with chunk 0 on how many
                    # invoices are in the document (rare, since it's the same
                    # file) - fall back to blank for this chunk's own keys on
                    # any invoice it didn't return an entry for, rather than
                    # crashing the whole request over a chunk-count mismatch.
                    source = chunk_result[i] if i < len(chunk_result) else blank
                    for key in keys:
                        inv[key] = source.get(key, blank[key])

            for inv in invoices:
                _fix_swapped_gstin_by_pan(inv)

            # A secondary invoice the recall check found that none of the
            # invoices above account for (by invoice number) - ChatPDF's
            # single whole-document extraction in chunk 0 can miss one
            # buried in a long bundled PDF (see RECALL_CHECK_PROMPT). Run a
            # targeted follow-up pass scoped to just that invoice rather
            # than leaving it silently dropped.
            missing = _find_missing_invoices(invoices, recall_future.result())
            for candidate in missing:
                extra = _extract_targeted_invoice(source_id, str(candidate.get("INVOICE_NUMBER", "")).strip())
                if extra is not None:
                    _fix_swapped_party_gstin(extra, invoices)
                    _fix_swapped_gstin_by_pan(extra)
                    invoices.append(extra)
    finally:
        httpx.post(CHATPDF_DELETE_URL, headers=_CHATPDF_HEADERS, json={'sources': [source_id]}, timeout=30)

    return invoices


def _looks_suspicious(invoices: list[dict]) -> bool:
    """Crude data-quality signal used to trigger one extra retry (see
    extract_receipt_with_retry) even on an extraction that returned
    successfully with no error. On a genuine multi-trip fleet bill, each
    ITEMS row represents a separate shipment and should carry its own
    distinct VEHICLE_NUMBER (see the ITEMS rules in prompt.py) - the model
    has been observed to instead copy the first row's vehicle number onto
    every row. Only flags when 2+ rows actually HAVE a non-blank vehicle
    number and they're all identical - a single main item plus blank-
    vehicle named-charge rows (freight + weighment/detention/... charges),
    the far more common invoice shape, is never flagged, since those rows
    are legitimately blank rather than sharing a value."""
    for invoice in invoices:
        non_blank = [str(item.get("VEHICLE_NUMBER", "")).strip() for item in invoice.get("ITEMS", [])]
        non_blank = [v for v in non_blank if v]
        if len(non_blank) >= 2 and len(set(non_blank)) == 1:
            return True
    return False


def extract_receipt_with_retry(pdf_path: str) -> list[dict]:
    last_exc = None
    last_result = None
    for attempt in range(1, MAX_RETRIES + 2):
        try:
            result = extract_receipt(pdf_path)
        except httpx.HTTPStatusError as e:
            if e.response.status_code not in RETRYABLE_STATUS_CODES:
                raise  # e.g. 400 bad request, 402 quota exceeded: never succeeds
            last_exc = e
            logger.warning(f"attempt {attempt} failed (status {e.response.status_code}): {e}")
        except (httpx.RequestError, ValueError) as e:
            last_exc = e
            logger.warning(f"attempt {attempt} failed: {type(e).__name__}: {e}")
        else:
            if not _looks_suspicious(result):
                return result
            last_exc = None
            last_result = result
            logger.warning(f"attempt {attempt} returned suspicious data (identical VEHICLE_NUMBER across multiple ITEMS rows) - retrying")

        if attempt <= MAX_RETRIES:
            delay = min(RETRY_BACKOFF_BASE * 2 ** (attempt - 1), RETRY_BACKOFF_CAP)
            time.sleep(delay + random.uniform(0, delay * 0.25))

    if last_result is not None:
        return last_result  # every attempt looked suspicious - the last one is still a real, successful extraction
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
    """True if extraction found nothing usable - e.g. the document isn't a
    Tax Invoice/PO (blank page, cover sheet, etc.) or the model echoed the
    empty template back."""
    if _is_target_document(result.get("METADATA", {}).get("IS_TARGET_DOCUMENT", "")) is False:
        return True  # model explicitly marked this as not a target document

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


_PLACE_OF_SUPPLY_PATTERN = re.compile(r"^\s*(\d{1,2})\s*[-–:]?\s*(.*)$")


def _split_place_of_supply(document: dict, gst_compliance: dict) -> tuple[str, str]:
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
    <state name>"."""
    raw = _first_value(document.get("PLACE_OF_SUPPLY"), gst_compliance.get("PLACE_OF_SUPPLY"))
    match = _PLACE_OF_SUPPLY_PATTERN.match(raw) if raw else None
    parsed_code = match.group(1) if match else ""
    parsed_state = match.group(2).strip() if match else raw.strip() if raw else ""
    code = _first_value(gst_compliance.get("PLACE_OF_SUPPLY_STATE_CODE"), parsed_code)
    return code, parsed_state


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
    _NAMED_CHARGE_FIELDS) to its amount, by classifying each ADDITIONAL_CHARGES
    row by description keyword. ADDITIONAL_CHARGES is a direct per-row
    transcription of the document's own charges table, and is the sole
    source here - LOGISTICS.<field> (e.g. LOGISTICS.WEIGHMENT_CHARGES) is
    deliberately not consulted: ChatPDF extracts LOGISTICS and
    ADDITIONAL_CHARGES in separate chat messages (see PROMPT_CHUNKS in
    prompt.py), so LOGISTICS's own named-charge fields are filled without
    the ADDITIONAL_CHARGES table in view and have shown up empty or
    duplicating/misattributing an ADDITIONAL_CHARGES amount rather than
    adding a genuinely independent one. Each ADDITIONAL_CHARGES entry is
    classified into at most one of these fields (first match wins, in
    _NAMED_CHARGE_FIELDS order), so one charge can't double-count into two
    fields that way."""
    charges = {name: 0.0 for name, _ in _NAMED_CHARGE_FIELDS}

    for charge in data.get("ADDITIONAL_CHARGES", []):
        description = (charge.get("DESCRIPTION", "") or charge.get("CHARGE_TYPE", "")).upper()
        for name, keywords in _NAMED_CHARGE_FIELDS:
            if charges[name]:
                continue  # already classified from an earlier ADDITIONAL_CHARGES row
            if any(keyword in description for keyword in keywords):
                charges[name] = _to_float(_first_value(charge.get("AMOUNT"), charge.get("TOTAL_AMOUNT")))
                break

    # EMPTY_UNLOADING_CHARGES, UNLOADING_CHARGES and LOADING_CHARGES overlap
    # as substrings ("LOADING" sits inside "UNLOADING" sits inside "EMPTY
    # UNLOADING"), so a loosely-worded ADDITIONAL_CHARGES description could
    # in principle satisfy more than one of these keyword sets. Each entry
    # only ever fills one field (first match wins above), but as a second
    # guard: if two of them carry the exact same non-zero amount, keep only
    # the most specific one (this list's order) and zero the rest, rather
    # than trusting a coincidence.
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
        qty = _to_float(item.get("QUANTITY", ""))
        unit_price = _to_float(item.get("UNIT_PRICE", ""))
        amount = _to_float(item.get("LINE_TOTAL", ""))
        if not amount and qty and unit_price:
            # LINE_TOTAL is occasionally left blank on a row where QUANTITY
            # and UNIT_PRICE were both still captured - rather than report
            # an amount of 0 for a real line, compute it the same way the
            # printed row itself would (Qty x Rate), since both factors are
            # already on this exact row rather than guessed from elsewhere.
            amount = qty * unit_price
        return cls(
            DESCRIPTION=item.get("DESCRIPTION", ""),
            HSN=item.get("HSN_CODE", ""),
            QTY=qty,
            UOM=item.get("UOM", ""),
            WEIGHT=_to_float(item.get("WEIGHT", "")),
            WEIGHT_UNIT=item.get("WEIGHT_UNIT", ""),
            UNIT_PRICE=unit_price,
            AMOUNT=amount,
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

        place_of_supply_code, place_of_supply_state = _split_place_of_supply(document, gst_compliance)
        gross_total = _to_float(amounts.get("TOTAL_AMOUNT", ""))
        total_tax = (
            _to_float(tax.get("IGST_AMOUNT", ""))
            + _to_float(tax.get("CGST_AMOUNT", ""))
            + _to_float(tax.get("SGST_AMOUNT", ""))
        )
        base_value_raw = _first_value(
            tax.get("TAXABLE_AMOUNT"),
            amounts.get("TAXABLE_AMOUNT"),
            amounts.get("SUBTOTAL"),
        )
        # An independently-extracted base value (not derived from ITEMS
        # itself) that _resolve_items uses to sanity-check the ITEMS array -
        # None when no such independent figure exists (only then does
        # _resolve_items fall back to always deduplicating exact matches).
        base_value_hint = (
            gross_total if (total_tax == 0 and gross_total)
            else (_to_float(base_value_raw) if base_value_raw else None)
        )

        item_list = cls._resolve_items(data, base_value_hint, gross_total)
        item_list.extend(_build_charge_items(data))

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
            VEHICLE_NUMBER=_first_value(logistics.get("VEHICLE_NUMBER"), delivery.get("VEHICLE_NUMBER")),
            ITEM_LIST=item_list,
        )

    @staticmethod
    def _resolve_items(data: dict, base_value_hint: float | None, gross_total: float) -> list["InvoiceItem"]:
        """ITEM_LIST is built only from the "ITEMS" array - the actual
        goods/service lines being billed (e.g. the freight/transportation
        charge itself on a GTA bill). Named add-on charges are appended to
        ITEM_LIST separately, as their own rows (see _build_charge_items) -
        the prompt asks the model to route these into ADDITIONAL_CHARGES
        specifically so they never land in ITEMS to begin with, and are
        added back in as ITEM_LIST rows afterwards rather than being read
        from ITEMS here.

        Two independent failure modes get corrected here, both using
        base_value_hint - a base/taxable value extracted independently (not
        derived from summing ITEMS itself; see from_dict) wherever the
        document prints one:

        1. A lone item's own LINE_TOTAL sometimes gets filled with the
           invoice's overall (tax-inclusive) Grand/Total Invoice Value
           instead of that line's own printed pre-tax amount - most likely
           on invoices where the item row's own total sits close to several
           summary lines (discounts, tax, grand total) beneath it. Only
           correctable when there's exactly one item, its amount exactly
           equals gross_total, and base_value_hint independently disagrees -
           at that point the true line amount is known (base_value_hint),
           not guessed.

        2. The model sometimes splits one genuinely single billed line into
           duplicate ITEMS rows within its own response - e.g. a single
           freight amount covering a shipment referenced by two invoice/LR
           numbers gets one row per reference number instead of one row for
           the one printed amount. Collapsing exact fingerprint duplicates
           (same dedup key used for the cross-page merge in
           group_into_invoices - see _item_fingerprint) would catch that,
           but the same fingerprint match also fires on a genuinely
           different pair of rows that happen to land on the same amount -
           e.g. two different trucks on a fleet-owner bill charged an
           identical rate for an identical route, where the model also
           (separately) mis-transcribed VEHICLE_NUMBER as the same value
           for both rows. Collapsing that case would silently drop a real,
           separately-billed row - worse than the duplicate this whole
           check exists to catch. There's no way to tell these two cases
           apart from the ITEMS array alone, so this arbitrates by which of
           the raw or deduplicated item list sums closer to
           base_value_hint: a real duplicate inflates the raw sum past the
           true total, while a wrongly-collapsed genuine row would
           undershoot it. With no independent figure to check against,
           default to deduplicating, since an exact fingerprint match is
           still more often a genuine duplicate than a coincidence."""
        items = [InvoiceItem.from_dict(item) for item in data.get("ITEMS", [])]
        # The model occasionally echoes the schema's example ITEMS entry
        # (an all-blank template row, shown to illustrate the shape) as a
        # literal extra item alongside the real ones it found - drop any
        # item that carries no actual data, regardless of why it showed up,
        # rather than relying on a prompt instruction alone to prevent it.
        # A billed line item always has a nonzero amount by definition, so
        # AMOUNT == 0 also disqualifies a row even when its other fields
        # (a description, a vehicle number) aren't blank - observed on a
        # fleet-owner bill where the model split one real row's amount and
        # vehicle number across two separate ITEMS entries instead of one.
        items = [item for item in items if not _is_blank_value(asdict(item)) and item.AMOUNT != 0]

        if (
            len(items) == 1
            and gross_total
            and items[0].AMOUNT == gross_total
            and base_value_hint not in (None, gross_total)
        ):
            items[0].AMOUNT = base_value_hint
            return items  # a single item can't have a duplicate to arbitrate

        seen = set()
        deduped = []
        for item in items:
            fingerprint = _item_fingerprint(item)
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            deduped.append(item)

        if len(deduped) == len(items):
            return items  # nothing was actually a fingerprint duplicate

        if base_value_hint is None:
            return deduped

        raw_sum = sum(item.AMOUNT for item in items)
        deduped_sum = sum(item.AMOUNT for item in deduped)
        return deduped if abs(deduped_sum - base_value_hint) <= abs(raw_sum - base_value_hint) else items

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
    per distinct INVOICE_NUMBER - entries that share an invoice number are
    merged into a single Invoice with combined ITEM_LIST; distinct invoice
    numbers each get their own Invoice. A single source PDF can contribute
    more than one entry here (extract_receipt() returns one dict per invoice
    it finds in that document), which this function doesn't need to treat
    specially - each entry is just another item in the list.

    source_id identifies the originating PDF (e.g. its basename) and exists
    to resolve an entry that never carries its own invoice number - such as
    a tooling/annexure page the model still returned a (blank-numbered)
    entry for, which would otherwise become its own orphaned "__unknown_i"
    Invoice missing every header field (customer GSTIN, dates, PO number,
    ...). Any entry with a blank INVOICE_NUMBER is folded into whichever
    invoice number was found elsewhere in the same source_id, resolved up
    front in a first pass so this works regardless of the order entries
    complete in (PDFs are processed concurrently, so a blank-numbered entry
    can finish before the one carrying the real invoice number). Caveat: if
    a single source_id genuinely contains two or more distinct real
    invoices (not just one invoice plus its own blank-numbered annexure), a
    blank-numbered entry from that source could be folded into the wrong
    one of them, since this fallback has no way to tell which invoice an
    unlabeled entry belongs to beyond "first one found for this source" - a
    rare case, and no worse than leaving it an unmerged orphan.

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
    for source_id, entry in page_results:
        number = entry.get("DOCUMENT", {}).get("INVOICE_NUMBER", "")
        if number and source_id not in invoice_number_by_source:
            invoice_number_by_source[source_id] = number

    invoices_by_number: dict[str, Invoice] = {}
    seen_items_by_number: dict[str, set] = {}
    source_ids_by_number: dict[str, set[str]] = {}
    order: list[str] = []
    for i, (source_id, entry) in enumerate(page_results):
        invoice = Invoice.from_dict(entry)
        key = invoice.INVOICE_NUMBER or invoice_number_by_source.get(source_id) or f"__unknown_{i}"
        source_ids_by_number.setdefault(key, set()).add(source_id)
        if key in invoices_by_number:
            existing = invoices_by_number[key]
            # Entries for the same invoice number aren't guaranteed to
            # arrive main-page-first (the model's array order isn't
            # meaningful, and different source PDFs are processed
            # concurrently), so whichever entry happened to be seen first
            # may be the one with blank header fields (e.g. an annexure
            # entry with no customer GSTIN of its own) - backfill any still-
            # blank header field on `existing` from this entry rather than
            # assuming the first-seen one has the real data.
            for f in fields(Invoice):
                if f.name == "ITEM_LIST":
                    continue
                if getattr(existing, f.name) in ("", 0.0) and getattr(invoice, f.name) not in ("", 0.0):
                    setattr(existing, f.name, getattr(invoice, f.name))
            # Check every item in this incoming entry against `seen` as it
            # stood *before* this entry started merging, then fold in this
            # entry's own fingerprints only once it's done - otherwise two
            # genuinely distinct rows in the same entry that happen to
            # share identical numbers (e.g. a "Type A" and "Type B" tool set
            # both priced/quantified the same) would collide with each
            # other and the second gets wrongly dropped as a same-entry
            # "duplicate", when the fingerprint dedup is only meant to catch
            # the same invoice extracted twice.
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


def process_pdf(pdf_path: str) -> tuple[str, list[dict] | dict]:
    """Returns (key, list-of-invoice-dicts) on success - usually one dict,
    more if the PDF contains multiple invoices - or (key, {"error": ...}) on
    failure. Callers distinguish the two by type: a dict means failure, a
    list means success (even an empty one, if extraction somehow yields no
    invoices)."""
    key = os.path.basename(pdf_path)
    try:
        return key, extract_receipt_with_retry(pdf_path)
    except Exception as e:
        # logger.exception (not .error) so the full traceback lands in
        # process.log, not just the message - needed to debug anything
        # unexpected later, not just the known retryable cases above.
        logger.exception(f"failed: {key}")
        return key, {"error": f"{type(e).__name__}: {e}"}


def main():
    pdf_paths = glob.glob(os.path.join(INVOICE_DIR, '*.pdf'))

    results = []
    # ThreadPoolExecutor.map yields results in the same order as `pdf_paths`
    # (despite running concurrently), so zipping the two together safely
    # recovers which source PDF each result came from - needed by
    # group_into_invoices() to fold blank-invoice-number entries into the
    # right invoice.
    for pdf_path, (key, result) in zip(pdf_paths, executor.map(process_pdf, pdf_paths)):
        if isinstance(result, dict):  # process_pdf's {"error": ...} sentinel
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
