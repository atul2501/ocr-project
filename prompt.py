import json

# ChatPDF caps every chat message (and its response) at ~2500 tokens. The
# full ~150-field extraction schema doesn't fit in a single round trip - the
# response gets silently truncated mid-JSON before finishing - so the schema
# is split into chunks sent as separate chats/message calls against the same
# uploaded PDF, then merged back together (see model.py). The fields
# themselves, and the final merged JSON shape, are unchanged - only how many
# API calls it takes to fill them in.

# A lightweight, separate scan (see model.py: _recall_check) that asks only
# for a bare list of invoice numbers/GSTINs anywhere in the document, not a
# full extraction - a long bundled PDF (a main invoice plus several
# supporting pages, one of which happens to be a second, genuinely separate
# invoice from a different vendor) has been observed to make chunk 0's own
# full extraction miss that second invoice as its own array entry, even
# with an explicit "check every page" instruction added directly to chunk
# 0. A narrower, single-purpose question recalls it far more reliably than
# asking for full multi-section extraction and complete invoice enumeration
# in the same pass. Any invoice number this recall turns up that isn't
# already in chunk 0's results triggers a second, targeted extraction pass
# scoped to just that invoice (see model.py: _extract_targeted_invoice).
RECALL_CHECK_PROMPT = """You are auditing this document for completeness, not extracting data yet.

List EVERY distinct invoice or tax invoice anywhere in this document, no
matter how minor or how deep in the file - including one issued by a
vendor OTHER than the main/primary one (e.g. a sub-contractor's own bill
to the primary vendor, bundled in among supporting pages). Check every
page; do not stop after finding the first or most prominent invoice.

Return ONLY a JSON array, one object per distinct invoice found, in this
exact structure - use the invoice number and GSTIN exactly as printed on
that invoice's own page:
[{"INVOICE_NUMBER": "", "SUPPLIER_GSTIN": "", "SUPPLIER_NAME": ""}]

If the document contains no invoices at all, return an empty array [].
Do not return markdown, explanations or any text outside the JSON array."""

_COMMON_HEADER = """You are a professional Invoice OCR and Document Data Extraction system for
SAP-based Accounts Payable, Finance, Procurement, GST and Logistics processing
in India.

Analyze the complete uploaded document carefully, across every page it
contains. Read printed text, tables, small text, GST details, tax details,
handwritten annotations, stamps, signatures, QR/barcode information and
logistics information.

Return a JSON array containing one object per distinct invoice/PO found
anywhere in this document. Most uploaded files contain exactly one invoice,
so the array will usually contain exactly one object - but if this document
genuinely contains more than one separate invoice (e.g. several bills
bundled into one PDF, or a multi-page invoice followed by a second,
unrelated invoice), return one object per invoice, each filled in
independently from only that invoice's own page(s). Check every page, not
just the first few - a long bundled PDF can carry a second, genuinely
separate invoice from a different vendor (e.g. a sub-contractor's own bill
to the primary vendor) buried among supporting pages later in the file; do
not stop scanning after finding the first invoice. Do not merge multiple
invoices into one object, and do not drop any of them.
Do not return markdown, explanations, comments or any text outside JSON.

GENERAL RULES:
- Do not guess or invent any value - every value must correspond to text
  visible in THIS document. If you can't point to where it's printed, leave
  the field "" instead.
- If a value is missing, unreadable or uncertain, return an empty string "".
- Preserve values exactly as printed wherever possible.
- Do not calculate missing tax, totals or amounts.
- Keep dates in YYYY-MM-DD format when unambiguous.
- Every amount/quantity/rate field must be a plain numeric string only -
  digits, at most one decimal point, optional leading minus sign. No
  currency symbols, thousands separators other than what's printed, or unit
  suffixes (kg, pcs, %). If it can't be reduced to a clean number this way,
  return "" rather than including the extra characters.
- Read clear handwriting; do not guess unclear handwriting.
- ALPHANUMERIC IDs - read each character individually (confusable glyphs:
  0/O, 1/I/l, 5/S, 8/B, 6/G, 2/Z, 9/g/q). GSTIN = 15 chars (2-digit state
  code + 10-char PAN [5 letters/4 digits/1 letter] + 1 digit + "Z" + 1
  checksum char). PAN = 10 chars (5 letters/4 digits/1 letter, e.g.
  AAAAA9999A). GST_COMPLIANCE.IRN = 64 lowercase hex chars (0-9, a-f only).
  Re-check before returning; if still uncertain after re-reading, return ""
  rather than a guess.

DOCUMENT TYPE FILTER - governs every field of every invoice object you
return, not only METADATA.IS_TARGET_DOCUMENT. Apply per invoice: a document
can mix a genuine Tax Invoice page with unrelated supporting pages
(annexures, challans, e-way bills), and may contain more than one invoice:
- This system only extracts data from Tax Invoices (GST tax invoices) and
  Purchase Orders (PO).
- A page counts as a Tax Invoice regardless of its literal header wording
  as long as it has ALL of: the issuing party's GSTIN, its own invoice
  number/reference, identification of who it is billed to, one or more
  itemized charges/quantities/amounts, and a total amount payable. This
  includes GTA/transporter/logistics documents headed just "BILL" or
  "FREIGHT BILL" - treat these as target documents too. Many are issued
  under GST reverse charge, so no CGST/SGST/IGST amount appears on the page
  itself - that alone does not disqualify it, as long as it still has its
  own invoice number and identifies the customer.
- A page with no invoice number of its own and no customer/buyer
  identification of its own is NOT a Tax Invoice, even if it shows a
  vendor/issuer GSTIN and a table of itemized costs with a subtotal/total
  (e.g. a tooling/parts/annexure cost breakdown page from later in the same
  invoice booklet). Do not return a separate invoice object for a page like
  this - it isn't its own document. If the whole uploaded file contains no
  page that qualifies as a Tax Invoice or PO, return a single object with
  "METADATA.IS_TARGET_DOCUMENT" set to false and every other field empty.
- If the whole document is some other kind of document (delivery challan,
  LR/weighment/transporter receipt with no amount payable, warehouse/
  storage paperwork, packing list, e-way bill copy, bank statement, or
  anything else that is not a Tax Invoice or PO by the definition above),
  return a single object with "METADATA.IS_TARGET_DOCUMENT" set to false,
  and every other field as an empty string "" - do not attempt to extract
  data from a non-target document.
- Exception: an e-Way Bill's "IRN Details" Ack Date (not its own generation
  date) belongs to the Tax Invoice it references under "Document No"/"Tax
  Invoice" - fold it into that invoice's object if present in this
  document. Only give the e-Way Bill page its own object
  (IS_TARGET_DOCUMENT true) if that Tax Invoice isn't otherwise present,
  filling ONLY "GST_COMPLIANCE.IRN", "GST_COMPLIANCE.ACKNOWLEDGEMENT_NUMBER",
  "GST_COMPLIANCE.ACKNOWLEDGEMENT_DATE" and "DOCUMENT.INVOICE_NUMBER" -
  leave everything else blank.
- If a page IS a Tax Invoice (by the definition above) or a Purchase Order,
  set "METADATA.IS_TARGET_DOCUMENT" to true on its object and extract all
  fields normally.
"""

_CHUNK_A1_RULES = """
RULES FOR THIS PORTION OF THE SCHEMA (parties and references):
- Distinguish Supplier/Vendor, Customer/Buyer, Bill-To and Ship-To.
- "PURCHASE_ORDER.PO_NUMBER" is the customer's own order/purchase reference
  that the vendor is billing against - fill it whenever the invoice header
  cites one, regardless of the exact label used ("PO No.", "Order Ref",
  "Ref. No.", "Customer Ref", "Your Order No.", "P.O. No."). These numbers
  are very often in the buyer's own ERP format (e.g. a 10-digit SAP PO
  number like "4300021506", sometimes with a revision/date suffix such as
  "/ R-01 DT.06-08-2025" - keep that suffix as part of the value). If the
  invoice cites two distinct such numbers under two different labels, put
  the customer's own PO-style reference (the buyer's ERP number format) in
  "PO_NUMBER" and the other in "PURCHASE_ORDER_REFERENCE".
- INVOICE_NUMBER is specifically the number the vendor's own Tax Invoice
  labels as its invoice/bill number - never a "Billing No.", voucher
  number, ERP/accounting document number, or other internal reference
  number that appears on a companion page (goods-receipt note, ledger
  extract, accounts-posting slip) describing the same vendor, items and
  amounts as an invoice you've already seen. Leave INVOICE_NUMBER "" on
  such a page rather than filling it with the internal reference number.
- "PLACE_OF_SUPPLY" is a specific statutory GST field - fill it only from
  text actually labelled "Place of Supply" (or an e-invoice's IRN/QR
  compliance block that states it). Never fill it from a route/destination
  field such as "To", "Ship To" or a freight description's destination,
  and never infer it from an address block's city/state. If never actually
  labelled, leave "" rather than substituting a nearby location.
- Use an empty string "" when the document does not contain a field.

Return exactly this JSON structure - one such object per invoice found on
the page, wrapped in an array:

"""

_CHUNK_A2_RULES = """
RULES FOR THIS PORTION OF THE SCHEMA (line items and additional charges):
- Extract every individual invoice line item and every additional charge -
  one entry per item in "ITEMS", never collapsed.
- Some transporter/logistics bills list several separate trips as rows in
  one table, each with its own printed Amount (e.g. one row per vehicle,
  each with its own LR No., Vehicle No., date and Amount) - treat each such
  row as its own "ITEMS" entry with its own "LR_NUMBER"/"VEHICLE_NUMBER",
  never blank because a value for a different row exists elsewhere, and
  never merged just because rows share the same From/To/product
  description. But if a single printed Amount instead covers two or more
  reference numbers together (e.g. one Freight Amount cell against two
  invoice/shipment/LR numbers listed under it), that is ONE row, not one
  per reference number - do not duplicate the same Amount into multiple
  ITEMS entries just because more than one reference number is printed
  with it.
- VEHICLE_NUMBER must be the full registration exactly as printed,
  including its leading state-code letters (e.g. "MP 09 GG 6787", not
  "09 GG 6787") - never drop that prefix.
- "ITEMS" is only for the actual goods or service being billed (e.g. the
  freight/transportation charge itself on a GTA/transporter bill). Any
  OTHER charge riding alongside that item/shipment belongs in
  "ADDITIONAL_CHARGES", never "ITEMS", even printed in the same block/table,
  under its own HSN/SAC code, or billed as if it were another line:
  weighment/parking/loading/unloading/detention/demurrage/handling charges,
  freight-forwarding/export service fees (usually a SAC code starting 99,
  e.g. 996519), packing, insurance, commission, agency fees. The test:
  is this its own distinct product/primary service, or a fee that exists
  because of the item/shipment above it? Only the former belongs in ITEMS.
- LINE_TOTAL on each item is that line's own printed amount - NEVER the
  invoice's overall Total/Grand Total. This matters most on invoices with
  only one line item.
- If this document also includes a separate E-Way Bill, delivery challan
  or other supporting page for the same shipment, extract ITEMS (HSN,
  QUANTITY, LINE_TOTAL, everything) only from the actual Tax Invoice
  page's own items table - never substitute a quantity or amount from a
  supporting page's own totals, even one describing the same goods (an
  E-Way Bill's Taxable Amount is very often a different, partial figure,
  e.g. for one truckload of a larger multi-vehicle shipment).
- Never add a subtotal/Sub Total/Grand Total/"Total <label>" row to
  "ITEMS" as if it were its own line item - skip these summary rows.
- A "Weight" column is never the same as "QUANTITY" - put it in "WEIGHT"
  instead. If no column is actually labelled Qty/Quantity/Nos/Units for
  that row, leave QUANTITY "" rather than substituting weight or any other
  nearby number. Fill "WEIGHT_UNIT" exactly as printed (e.g. "Metric Tons",
  "MT", "Kg", "Tons") - never abbreviate or substitute a unit that isn't
  itself printed.
- Extract HSN/SAC codes whenever visible.
- Use an empty string "" when the document does not contain a field.

Return exactly this JSON structure - one such object per invoice found on
the page, wrapped in an array:

"""

_CHUNK_B_RULES = """
RULES FOR THIS PORTION OF THE SCHEMA (tax, amounts, payment, logistics,
GST compliance, references, approval):
- Extract GST components separately: CGST, SGST, IGST, UTGST and Cess.
- Extract Indian GSTIN, PAN, CIN and other statutory identifiers when
  visible.
- LOGISTICS.VEHICLE_NUMBER must be the full registration exactly as
  printed, including its leading state-code letters (e.g. "MP 09 GG 6787",
  not "09 GG 6787") - never drop that prefix.
- Use an empty string "" when the document does not contain a field.

Return exactly this JSON structure - one such object per invoice found on
the page, wrapped in an array:

"""

_CHUNK_A1_SCHEMA = {
    "DOCUMENT": {
        "DOCUMENT_TYPE": "",
        "DOCUMENT_TITLE": "",
        "INVOICE_NUMBER": "",
        "INVOICE_DATE": "",
        "INVOICE_REFERENCE_NUMBER": "",
        "ORIGINAL_INVOICE_NUMBER": "",
        "REVISION_NUMBER": "",
        "CURRENCY": "",
        "PLACE_OF_SUPPLY": "",
        "SUPPLY_TYPE": "",
        "REVERSE_CHARGE": "",
    },
    "SUPPLIER": {
        "NAME": "",
        "LEGAL_NAME": "",
        "TRADE_NAME": "",
        "VENDOR_CODE": "",
        "ADDRESS": "",
        "CITY": "",
        "STATE": "",
        "STATE_CODE": "",
        "COUNTRY": "",
        "PINCODE": "",
        "GSTIN": "",
        "PAN": "",
        "CIN": "",
        "TAN": "",
        "EMAIL": "",
        "PHONE": "",
        "WEBSITE": "",
    },
    "CUSTOMER": {
        "NAME": "",
        "LEGAL_NAME": "",
        "CUSTOMER_CODE": "",
        "ADDRESS": "",
        "CITY": "",
        "STATE": "",
        "STATE_CODE": "",
        "COUNTRY": "",
        "PINCODE": "",
        "GSTIN": "",
        "PAN": "",
        "EMAIL": "",
        "PHONE": "",
    },
    "BILL_TO": {
        "NAME": "",
        "ADDRESS": "",
        "CITY": "",
        "STATE": "",
        "STATE_CODE": "",
        "PINCODE": "",
        "GSTIN": "",
        "PAN": "",
    },
    "SHIP_TO": {
        "NAME": "",
        "ADDRESS": "",
        "CITY": "",
        "STATE": "",
        "STATE_CODE": "",
        "PINCODE": "",
        "GSTIN": "",
        "PAN": "",
    },
    "PURCHASE_ORDER": {
        "PO_NUMBER": "",
        "PO_DATE": "",
        "PURCHASE_ORDER_REFERENCE": "",
        "PURCHASE_REQUISITION_NUMBER": "",
        "CONTRACT_NUMBER": "",
        "AGREEMENT_NUMBER": "",
        "WORK_ORDER_NUMBER": "",
    },
    "DELIVERY": {
        "DELIVERY_ORDER_NUMBER": "",
        "DELIVERY_ORDER_DATE": "",
        "DELIVERY_NOTE_NUMBER": "",
        "DELIVERY_NOTE_DATE": "",
        "CHALLAN_NUMBER": "",
        "CHALLAN_DATE": "",
        "DISPATCH_DATE": "",
        "DELIVERY_DATE": "",
        "EWAY_BILL_NUMBER": "",
        "LR_NUMBER": "",
        "LR_DATE": "",
        "TRANSPORTER_NAME": "",
        "VEHICLE_NUMBER": "",
        "VEHICLE_TYPE": "",
        "FROM_LOCATION": "",
        "TO_LOCATION": "",
        "PLACE_OF_DISPATCH": "",
        "PLACE_OF_DELIVERY": "",
    },
    "METADATA": {
        "PAGE_TYPE": "",
        "LANGUAGE": "",
        "HANDWRITTEN_CONTENT_PRESENT": "",
        "STAMP_PRESENT": "",
        "SIGNATURE_PRESENT": "",
        "QR_CODE_PRESENT": "",
        "BARCODE_PRESENT": "",
        "IS_TARGET_DOCUMENT": "",
    },
}

_CHUNK_A2_SCHEMA = {
    "ITEMS": [
        {
            "LINE_NUMBER": "",
            "ITEM_CODE": "",
            "MATERIAL_CODE": "",
            "LR_NUMBER": "",
            "VEHICLE_NUMBER": "",
            "PRODUCT_NAME": "",
            "DESCRIPTION": "",
            "HSN_CODE": "",
            "SAC_CODE": "",
            "QUANTITY": "",
            "UOM": "",
            "WEIGHT": "",
            "WEIGHT_UNIT": "",
            "UNIT_PRICE": "",
            "GROSS_AMOUNT": "",
            "DISCOUNT": "",
            "DISCOUNT_PERCENTAGE": "",
            "TAXABLE_VALUE": "",
            "GST_RATE": "",
            "CGST_RATE": "",
            "CGST_AMOUNT": "",
            "SGST_RATE": "",
            "SGST_AMOUNT": "",
            "IGST_RATE": "",
            "IGST_AMOUNT": "",
            "UTGST_RATE": "",
            "UTGST_AMOUNT": "",
            "CESS_RATE": "",
            "CESS_AMOUNT": "",
            "OTHER_CHARGES": "",
            "LINE_TOTAL": "",
        }
    ],
    "ADDITIONAL_CHARGES": [
        {
            "DESCRIPTION": "",
            "CHARGE_TYPE": "",
            "QUANTITY": "",
            "RATE": "",
            "AMOUNT": "",
            "TAXABLE_VALUE": "",
            "GST_RATE": "",
            "CGST_AMOUNT": "",
            "SGST_AMOUNT": "",
            "IGST_AMOUNT": "",
            "TOTAL_AMOUNT": "",
        }
    ],
}

_CHUNK_B_SCHEMA = {
    "TAX": {
        "TAXABLE_AMOUNT": "",
        "CGST_RATE": "",
        "CGST_AMOUNT": "",
        "SGST_RATE": "",
        "SGST_AMOUNT": "",
        "IGST_RATE": "",
        "IGST_AMOUNT": "",
        "UTGST_RATE": "",
        "UTGST_AMOUNT": "",
        "CESS_RATE": "",
        "CESS_AMOUNT": "",
        "OTHER_TAX": "",
        "TOTAL_TAX": "",
    },
    "AMOUNTS": {
        "SUBTOTAL": "",
        "GROSS_AMOUNT": "",
        "TOTAL_DISCOUNT": "",
        "FREIGHT": "",
        "TRANSPORTATION_CHARGES": "",
        "PACKING_CHARGES": "",
        "LOADING_CHARGES": "",
        "UNLOADING_CHARGES": "",
        "INSURANCE_CHARGES": "",
        "HANDLING_CHARGES": "",
        "OTHER_CHARGES": "",
        "TAXABLE_AMOUNT": "",
        "TOTAL_TAX": "",
        "ROUND_OFF": "",
        "ADVANCE_PAID": "",
        "TOTAL_AMOUNT": "",
        "AMOUNT_PAID": "",
        "BALANCE_DUE": "",
        "AMOUNT_IN_WORDS": "",
    },
    "PAYMENT": {
        "PAYMENT_TERMS": "",
        "DUE_DATE": "",
        "CREDIT_PERIOD_DAYS": "",
        "PAYMENT_METHOD": "",
        "BANK_NAME": "",
        "BANK_ACCOUNT_NUMBER": "",
        "IFSC_CODE": "",
        "UPI_ID": "",
    },
    "LOGISTICS": {
        "VEHICLE_NUMBER": "",
        "VEHICLE_TYPE": "",
        "CONTAINER_NUMBER": "",
        "CONTAINER_TYPE": "",
        "CONTAINER_DETAILS": "",
        "WEIGHT": "",
        "WEIGHT_UNIT": "",
        "FREIGHT_AMOUNT": "",
        "WEIGHMENT_CHARGES": "",
        "DETENTION_CHARGES": "",
        "DEMURRAGE_CHARGES": "",
        "PARKING_CHARGES": "",
        "LOADING_CHARGES": "",
        "UNLOADING_CHARGES": "",
        "EMPTY_UNLOADING_CHARGES": "",
    },
    "GST_COMPLIANCE": {
        "SUPPLIER_GSTIN": "",
        "CUSTOMER_GSTIN": "",
        "PLACE_OF_SUPPLY": "",
        "PLACE_OF_SUPPLY_STATE_CODE": "",
        "REVERSE_CHARGE": "",
        "IRN": "",
        "ACKNOWLEDGEMENT_NUMBER": "",
        "ACKNOWLEDGEMENT_DATE": "",
        "QR_CODE_DATA": "",
        "EWAY_BILL_NUMBER": "",
        "GST_TAXABLE_VALUE": "",
        "CGST": "",
        "SGST": "",
        "IGST": "",
        "UTGST": "",
        "CESS": "",
    },
    "REFERENCES": {
        "PO_NUMBERS": [],
        "SALES_ORDER_NUMBERS": [],
        "DELIVERY_ORDER_NUMBERS": [],
        "DELIVERY_NOTE_NUMBERS": [],
        "CHALLAN_NUMBERS": [],
        "LR_NUMBERS": [],
        "EWAY_BILL_NUMBERS": [],
        "CONTAINER_NUMBERS": [],
        "VEHICLE_NUMBERS": [],
        "OTHER_REFERENCE_NUMBERS": [],
    },
    "APPROVAL": {
        "AUTHORIZED_SIGNATORY_NAME": "",
        "RECEIVER_NAME": "",
        "RECEIVER_SIGNATURE_PRESENT": "",
        "SUPPLIER_SIGNATURE_PRESENT": "",
        "COMPANY_STAMP_PRESENT": "",
    },
}


def _render_schema(schema: dict) -> str:
    return "[" + json.dumps(schema, indent=2) + ",...]"


def _blank_schema(schema: dict) -> dict:
    """Blank version of a chunk schema - top-level keys that are themselves
    arrays (ITEMS, ADDITIONAL_CHARGES) blank to [], keys that are objects
    blank to an all-"" (or all-[] for their own list fields) copy."""
    blank = {}
    for key, value in schema.items():
        if isinstance(value, list):
            blank[key] = []
        else:
            blank[key] = {field: ([] if isinstance(v, list) else "") for field, v in value.items()}
    return blank


# Three prompts sent per page (see model.py: extract_receipt) - ChatPDF caps
# every chat message and its response at ~2500 tokens, and even a two-way
# split still overflowed on the ITEMS-heavy half, so the schema is split
# three ways instead. Merging the three response arrays back together by
# invoice index reconstructs exactly the same combined schema the
# single-call version used to return - CHUNK_KEYS's three lists together
# cover every top-level key, none renamed or dropped. The first chunk
# (header/parties) carries METADATA.IS_TARGET_DOCUMENT, so a page found not
# to be a target document can skip the other two calls entirely and use
# BLANK_CHUNKS instead - see extract_receipt.
PROMPT_CHUNKS = [
    _COMMON_HEADER + _CHUNK_A1_RULES + _render_schema(_CHUNK_A1_SCHEMA),
    _COMMON_HEADER + _CHUNK_A2_RULES + _render_schema(_CHUNK_A2_SCHEMA),
    _COMMON_HEADER + _CHUNK_B_RULES + _render_schema(_CHUNK_B_SCHEMA),
]

CHUNK_KEYS = [
    list(_CHUNK_A1_SCHEMA.keys()),
    list(_CHUNK_A2_SCHEMA.keys()),
    list(_CHUNK_B_SCHEMA.keys()),
]

# Blank fallback for each chunk after the first, used when chunk 0 already
# determined this page is not a target document.
BLANK_CHUNKS = [_blank_schema(_CHUNK_A2_SCHEMA), _blank_schema(_CHUNK_B_SCHEMA)]


def blank_invoice() -> dict:
    """A fresh, fully-blank invoice dict covering every key in the schema -
    used when ChatPDF rejects a page outright (e.g. "Could not read PDF" on
    an almost-blank scanned page) instead of returning an empty extraction,
    so that case is treated the same as any other blank/non-target page."""
    invoice = _blank_schema(_CHUNK_A1_SCHEMA)
    invoice.update(_blank_schema(_CHUNK_A2_SCHEMA))
    invoice.update(_blank_schema(_CHUNK_B_SCHEMA))
    return invoice
