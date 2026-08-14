import itertools
import os
import threading

from ollama import Client

HOST = 'https://ollama.com'
KEY_ENV_PREFIX = 'OLLAMA_API_KEY_'

MODEL = 'minimax-m3'  # vision-capable, free tier; tested more accurate than gemma4:31b on GSTIN extraction

INVOICE_DIR = 'invoice'
PDF_ZOOM = 5  # render scale; higher = sharper but slower/bigger
OUTPUT_PATH = 'out.json'
LOG_PATH = 'process.log'
SHARPENED_DIR = 'output'

# Fill in as many keys as you have - 1 is fine, so is 8. Requests rotate
# across whichever ones are non-empty here. An OLLAMA_API_KEY_<n> environment
# variable, if set, overrides the entry at that position (1-indexed).
API_KEYS = [
    'cc76568fb2e040988cf13671da7b9d19.RcuniqYYf7DpS8xqgeOP1gRt',
]

MAX_WORKERS = 10  # concurrent Ollama requests, shared by every caller in this process (CLI batch run + all API requests) so load never exceeds this regardless of how many PDFs come in at once; extra pages simply wait in the executor's internal queue
MAX_RETRIES = 2  # retries after the first attempt (3 attempts total per page)
RETRY_BACKOFF_BASE = 2  # seconds
RETRY_BACKOFF_CAP = 30  # seconds
RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}

SHARPEN_RADIUS = 2  # UnsharpMask: pixel radius of the blur used to detect edges
SHARPEN_PERCENT = 200  # UnsharpMask: strength of the sharpening effect
SHARPEN_THRESHOLD = 1  # UnsharpMask: minimum brightness change to be sharpened (avoids amplifying noise)
CONTRAST_CUTOFF = 1  # autocontrast: percent of darkest/lightest pixels clipped before stretching the range

_lock = threading.Lock()


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
            "No Ollama API keys found. Add at least one to API_KEYS in api.py, "
            f"or set {KEY_ENV_PREFIX}1, {KEY_ENV_PREFIX}2, ... in the environment."
        )
    return keys


_clients = [Client(host=HOST, headers={'Authorization': f"Bearer {key}"}) for key in _load_keys()]
_clients_cycle = itertools.cycle(_clients)


def get_client() -> Client:
    """Return the next client in round-robin rotation across all configured API keys,
    so requests are spread across accounts instead of exhausting one free-tier limit."""
    with _lock:
        return next(_clients_cycle)
