import itertools
import os
import threading

import httpx

HOST = 'https://api.chatpdf.com/v1'
KEY_ENV_PREFIX = 'CHATPDF_API_KEY_'

INVOICE_DIR = 'invoice'
OUTPUT_PATH = 'out.json'
LOG_PATH = 'process.log'

# Fill in as many keys as you have - 1 is fine, so is 8. Requests rotate
# across whichever ones are non-empty here. A CHATPDF_API_KEY_<n> environment
# variable, if set, overrides the entry at that position (1-indexed).
API_KEYS = [
            'sec_O6RW3TxuDTTIj7O2dfFbuPOy4eaxErlE',
            ]

MAX_WORKERS = 10  # concurrent ChatPDF requests (one per document), shared by every caller in this process (CLI batch run + all API requests) so load never exceeds this regardless of how many PDFs come in at once; extra documents simply wait in the executor's internal queue
MAX_RETRIES = 2  # retries after the first attempt (3 attempts total per document)
RETRY_BACKOFF_BASE = 2  # seconds
RETRY_BACKOFF_CAP = 30  # seconds
RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}
REQUEST_TIMEOUT = 120  # seconds; uploading + getting a first answer on a large multi-page PDF can be slow

_lock = threading.Lock()


class ChatPDFClient:
    """Thin wrapper over the ChatPDF REST API (https://www.chatpdf.com/docs/api) -
    there's no official Python SDK, so this covers just the 3 calls this
    project needs: upload a PDF as a "source", ask one chat question against
    that source, and delete the source again once we're done with it."""

    def __init__(self, api_key: str, host: str = HOST):
        self._http = httpx.Client(base_url=host, timeout=REQUEST_TIMEOUT)
        self._headers = {'x-api-key': api_key}

    def add_source(self, pdf_bytes: bytes, filename: str = 'document.pdf') -> str:
        response = self._http.post(
            '/sources/add-file',
            headers=self._headers,
            files={'file': (filename, pdf_bytes, 'application/pdf')},
        )
        response.raise_for_status()
        return response.json()['sourceId']

    def chat(self, source_id: str, prompt: str) -> str:
        response = self._http.post(
            '/chats/message',
            headers=self._headers,
            json={
                'sourceId': source_id,
                'messages': [{'role': 'user', 'content': prompt}],
            },
        )
        response.raise_for_status()
        return response.json()['content']

    def delete_source(self, source_id: str) -> None:
        response = self._http.post(
            '/sources/delete',
            headers=self._headers,
            json={'sources': [source_id]},
        )
        response.raise_for_status()


def _load_keys() -> list[str]:
    keys = []
    i = 1
    while True:
        named_key = API_KEYS[i - 1] if i <= len(API_KEYS) else None
        key = os.environ.get(f"{KEY_ENV_PREFIX}{i}") or named_key
        if not key:
            if i <= len(API_KEYS):
                i += 1
                continue  # skip blank slots in API_KEYS, keep checking further ones
            break
        keys.append(key)
        i += 1
    if not keys:
        raise RuntimeError(
            "No ChatPDF API keys found. Add at least one to API_KEYS in api.py, "
            f"or set {KEY_ENV_PREFIX}1, {KEY_ENV_PREFIX}2, ... in the environment."
        )
    return keys


_clients = [ChatPDFClient(key) for key in _load_keys()]
_clients_cycle = itertools.cycle(_clients)


def get_client() -> ChatPDFClient:
    """Return the next client in round-robin rotation across all configured API keys,
    so requests are spread across accounts instead of exhausting one rate limit."""
    with _lock:
        return next(_clients_cycle)
