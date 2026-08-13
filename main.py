import glob
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
from ollama import Client, ResponseError

client = Client(
    host='https://ollama.com',
    headers={'Authorization': f"Bearer f2fdab4ff99e47d8b975a4db37cbf6d6.uFRcUfigvmEhzHQ8XDXNcyGP"},
)


MODEL = 'gemma4:31b'  # vision-capable, available on this account's free tier

INVOICE_DIR = 'invoice'
PDF_ZOOM = 2.0  # render scale; higher = sharper but slower/bigger
OUTPUT_PATH = 'out.json'
LOG_PATH = 'process.log'

logging.basicConfig(
    filename=LOG_PATH,
    filemode='a',
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
)
logger = logging.getLogger(__name__)

MAX_WORKERS = 4  # concurrent Ollama requests; no documented rate limit to tune against
MAX_RETRIES = 2  # retries after the first attempt (3 attempts total per page)
RETRY_BACKOFF_BASE = 2  # seconds
RETRY_BACKOFF_CAP = 30  # seconds
RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}

PROMPT = (PROMPTS)


def pdf_to_images(pdf_path: str) -> list[bytes]:
    doc = pymupdf.open(pdf_path)
    matrix = pymupdf.Matrix(PDF_ZOOM, PDF_ZOOM)
    images = [page.get_pixmap(matrix=matrix).tobytes('png') for page in doc]
    doc.close()
    return images


def extract_receipt(image) -> dict:
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
        try:
            return extract_receipt(image)
        except ResponseError as e:
            if e.status_code not in RETRYABLE_STATUS_CODES:
                raise  # e.g. 404 model-not-found, 403 needs-subscription: never succeeds
            last_exc = e
        except (ConnectionError, httpx.RequestError, ValueError) as e:
            last_exc = e

        if attempt <= MAX_RETRIES:
            delay = min(RETRY_BACKOFF_BASE * 2 ** (attempt - 1), RETRY_BACKOFF_CAP)
            time.sleep(delay + random.uniform(0, delay * 0.25))

    raise last_exc


def process_page(pdf_path: str, page_num: int, image_bytes: bytes) -> tuple[str, dict]:
    key = f"{os.path.basename(pdf_path)}#page{page_num}"
    try:
        return key, extract_receipt_with_retry(image_bytes)
    except Exception as e:
        logger.error(f"failed: {key}: {type(e).__name__}: {e}")
        return key, {"error": f"{type(e).__name__}: {e}"}


def main():
    pages = []
    for pdf_path in glob.glob(os.path.join(INVOICE_DIR, '*.pdf')):
        for page_num, image_bytes in enumerate(pdf_to_images(pdf_path), start=1):
            pages.append((pdf_path, page_num, image_bytes))

    results = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        for key, result in executor.map(lambda args: process_page(*args), pages):
            results.append(result)
            logger.info(f"done: {key}")

    with open(OUTPUT_PATH, 'w', encoding='utf-8') as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    print(json.dumps(results, indent=2, ensure_ascii=False))
