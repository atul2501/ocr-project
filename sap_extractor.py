#!/usr/bin/env python3
"""Extract scanned PDF invoices into a labeled, table-style JSON structure.

Usage:
    python sap_extractor.py                      # every PDF in input/, writes output/<name>_sap.json
    python sap_extractor.py file.pdf              # a single PDF
    python sap_extractor.py file.pdf -o some_dir  # custom output directory
"""

import argparse
import glob
import json
import os
import re
import subprocess
import time
import urllib.request

import opendataloader_pdf

OCR_PORT = 5002
JAVA17_BIN = "/opt/homebrew/opt/openjdk@17/bin"
LINE_ITEM_HEADER_WORDS = ["sr", "date", "vehicle", "from", "to", "back", "container", "freight"]
MONEY_RE = re.compile(r"^[\d,]+(?:[.\s]\d{1,2})?$")


def _ensure_java17():
    if os.path.isdir(JAVA17_BIN) and JAVA17_BIN not in os.environ.get("PATH", ""):
        os.environ["PATH"] = JAVA17_BIN + os.pathsep + os.environ.get("PATH", "")


class PdfInvoiceExtractor:
    """Runs opendataloader-pdf (with local OCR) on scanned PDFs and reshapes the
    raw layout JSON into invoice-style fields: header, bill-to, a line-item
    table, charges, total, and bank/registration details.

    The field labels this looks for (Invoice No, GST No, Place of supply, PAN
    No, BANK Name, the Sr/Date/Vehicle/.../Freight table header, ...) match the
    ILTS/FB60 transport-invoice template. A differently laid out invoice will
    need its own label set.
    """

    def __init__(self, ocr_port: int = OCR_PORT):
        _ensure_java17()
        self.ocr_port = ocr_port
        self.ocr_url = f"http://127.0.0.1:{ocr_port}"
        self._server_proc = None

    # -- OCR backend lifecycle -----------------------------------------

    def _server_healthy(self) -> bool:
        try:
            urllib.request.urlopen(f"{self.ocr_url}/health", timeout=1)
            return True
        except Exception:
            return False

    def start_ocr_server(self, timeout: float = 180.0):
        if self._server_healthy():
            return
        self._server_proc = subprocess.Popen(
            ["opendataloader-pdf-hybrid", "--port", str(self.ocr_port), "--force-ocr"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._server_healthy():
                return
            time.sleep(1)
        raise RuntimeError("OCR backend did not become healthy in time")

    def stop_ocr_server(self):
        if self._server_proc is not None:
            self._server_proc.terminate()
            self._server_proc.wait(timeout=10)
            self._server_proc = None

    # -- raw layout extraction ------------------------------------------

    def extract_raw(self, pdf_path: str, output_dir: str) -> dict:
        os.makedirs(output_dir, exist_ok=True)
        opendataloader_pdf.convert(
            input_path=[pdf_path],
            output_dir=output_dir,
            format="json",
            hybrid="docling-fast",
            hybrid_url=self.ocr_url,
            hybrid_mode="full",
        )
        stem = os.path.splitext(os.path.basename(pdf_path))[0]
        with open(os.path.join(output_dir, f"{stem}.json")) as f:
            return json.load(f)

    # -- geometry helpers -------------------------------------------------

    @staticmethod
    def _text(el) -> str:
        return (el.get("content") or "").strip()

    @staticmethod
    def _xmid(el) -> float:
        x0, _, x1, _ = el["bounding box"]
        return (x0 + x1) / 2

    @staticmethod
    def _group_rows(elements, y_tol: float = 5.0):
        """Cluster elements into visual rows by walking top-to-bottom and
        starting a new row whenever the gap to the previous baseline exceeds
        y_tol; chaining (vs. comparing to the row's first element) tolerates
        the slight baseline drift a scanned/skewed page produces."""
        by_page = {}
        for el in elements:
            by_page.setdefault(el.get("page number"), []).append(el)

        rows = []
        for _, els in sorted(by_page.items()):
            els = sorted(els, key=lambda e: -e["bounding box"][1])
            current, prev_y = [], None
            for el in els:
                y0 = el["bounding box"][1]
                if prev_y is not None and prev_y - y0 > y_tol:
                    rows.append(sorted(current, key=lambda e: e["bounding box"][0]))
                    current = []
                current.append(el)
                prev_y = y0
            if current:
                rows.append(sorted(current, key=lambda e: e["bounding box"][0]))
        return rows

    def _find_row_index(self, rows, label):
        label = label.lower().rstrip(":")
        for i, row in enumerate(rows):
            for el in row:
                if self._text(el).lower().rstrip(":") == label:
                    return i
        return None

    def _find_field(self, rows, label):
        idx = self._find_row_index(rows, label)
        if idx is None:
            return None
        label = label.lower().rstrip(":")
        row = rows[idx]
        for i, el in enumerate(row):
            if self._text(el).lower().rstrip(":") == label and i + 1 < len(row):
                return self._text(row[i + 1])
        return None

    @staticmethod
    def _match_columns(headers, cells):
        """Greedy nearest-first assignment of cells to header columns by
        x-center distance: process all (header, cell) pairs smallest-distance
        first so a well-aligned pair wins even if a farther column would
        otherwise have claimed that cell first in reading order."""
        pairs = sorted(
            ((abs(PdfInvoiceExtractor._xmid(h) - PdfInvoiceExtractor._xmid(c)), h, c) for h in headers for c in cells),
            key=lambda p: p[0],
        )
        used_h, used_c, result = set(), set(), {}
        for _, h, c in pairs:
            hid, cid = id(h), id(c)
            if hid in used_h or cid in used_c:
                continue
            key = PdfInvoiceExtractor._text(h).lower().replace(" ", "_")
            result[key] = PdfInvoiceExtractor._text(c)
            used_h.add(hid)
            used_c.add(cid)
        leftover = [PdfInvoiceExtractor._text(c) for c in cells if id(c) not in used_c]
        return result, leftover

    # -- structuring --------------------------------------------------------

    def structure(self, raw: dict) -> dict:
        elements = list(raw.get("kids", []))
        rows = self._group_rows(elements)

        header_idx = next(
            (i for i, row in enumerate(rows)
             if sum(any(w in self._text(e).lower() for w in LINE_ITEM_HEADER_WORDS) for e in row) >= 4),
            None,
        )

        line_items, charges, total_amount = [], [], None
        if header_idx is not None:
            headers = [e for e in rows[header_idx] if e.get("type") != "image"]
            i = header_idx + 1
            while i < len(rows):
                cells = [e for e in rows[i] if e.get("type") != "image"]
                texts = [self._text(c) for c in cells]
                if len(cells) == 2 and MONEY_RE.match(texts[-1] or ""):
                    if texts[0].lower() == "total":
                        total_amount = texts[-1]
                    else:
                        charges.append({"description": texts[0], "amount": texts[-1]})
                elif len(cells) >= 3:
                    item, leftover = self._match_columns(headers, cells)
                    if leftover:
                        item["unmatched"] = leftover
                    line_items.append(item)
                else:
                    break
                i += 1

        to_idx = self._find_row_index(rows, "TO")
        gst_idx = self._find_row_index(rows, "GST No")
        bill_to_name, bill_to_address = None, None
        if to_idx is not None and gst_idx is not None and gst_idx > to_idx:
            lines = [
                " ".join(self._text(e) for e in row if e.get("type") != "image")
                for row in rows[to_idx + 1:gst_idx]
            ]
            lines = [l for l in lines if l]
            bill_to_name = lines[0] if lines else None
            bill_to_address = " ".join(lines[1:]) if len(lines) > 1 else None

        signatory_lines = [
            self._text(e) for e in elements
            if "signatory" in self._text(e).lower() or self._text(e).lower().startswith("for ")
        ]

        return {
            "vendor_name": self._first_heading(elements),
            "invoice_no": self._find_field(rows, "Invoice No"),
            "invoice_date": self._find_field(rows, "Date"),
            "bill_to": {
                "name": bill_to_name,
                "address": bill_to_address,
                "gst_no": self._find_field(rows, "GST No"),
                "place_of_supply": self._find_field(rows, "Place of supply"),
            },
            "line_items": line_items,
            "additional_charges": charges,
            "total_amount": total_amount,
            "vendor_registration": {
                "pan_no": self._find_field(rows, "PAN No"),
                "gstin_no": self._find_field(rows, "GSTIN NO"),
            },
            "bank_details": {
                "bank_name": self._find_field(rows, "BANK Name"),
                "branch_details": self._find_field(rows, "Branch"),
            },
            "signatory_lines": signatory_lines,
            "ocr_notes": (
                "Extracted via local OCR from a scanned PDF; values are not corrected "
                "for OCR errors. Verify against the source PDF before use."
            ),
        }

    @staticmethod
    def _first_heading(elements):
        for el in elements:
            if el.get("type") == "heading":
                return el.get("content")
        return None

    # -- orchestration --------------------------------------------------

    def process(self, pdf_path: str, output_dir: str = "output") -> dict:
        raw = self.extract_raw(pdf_path, output_dir)
        structured = self.structure(raw)
        stem = os.path.splitext(os.path.basename(pdf_path))[0]
        out_path = os.path.join(output_dir, f"{stem}_sap.json")
        with open(out_path, "w") as f:
            json.dump(structured, f, indent=2)
        return structured

    def process_folder(self, input_dir: str = "input", output_dir: str = "output") -> dict:
        pdfs = sorted(glob.glob(os.path.join(input_dir, "*.pdf")))
        if not pdfs:
            print(f"No PDFs found in {input_dir}/")
            return {}
        self.start_ocr_server()
        results = {}
        try:
            for pdf in pdfs:
                try:
                    results[pdf] = self.process(pdf, output_dir)
                except Exception as exc:
                    print(f"Failed on {pdf}: {exc}")
        finally:
            self.stop_ocr_server()
        return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", nargs="?", help="Single PDF to process (default: all PDFs in input/)")
    parser.add_argument("-o", "--output-dir", default="output")
    args = parser.parse_args()

    extractor = PdfInvoiceExtractor()
    if args.pdf:
        extractor.start_ocr_server()
        try:
            result = extractor.process(args.pdf, args.output_dir)
        finally:
            extractor.stop_ocr_server()
        print(json.dumps(result, indent=2))
    else:
        results = extractor.process_folder(output_dir=args.output_dir)
        print(f"Processed {len(results)} PDF(s) into {args.output_dir}/")


if __name__ == "__main__":
    main()
