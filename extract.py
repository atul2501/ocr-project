#!/usr/bin/env python3
"""Convert PDF(s) to JSON using opendataloader-pdf.

Usage:
    python extract.py file1.pdf [file2.pdf ...] [-o output_dir]
    python extract.py some_folder/ -o output/
"""

import argparse
import sys

import opendataloader_pdf


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="+", help="PDF file(s) and/or folder(s) to convert")
    parser.add_argument("-o", "--output-dir", default="output", help="Directory to write JSON into (default: output/)")
    args = parser.parse_args()

    try:
        opendataloader_pdf.convert(
            input_path=args.inputs,
            output_dir=args.output_dir,
            format="json",
        )
    except Exception as exc:
        print(f"Conversion failed: {exc}", file=sys.stderr)
        return 1

    print(f"Done. JSON written to {args.output_dir}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
