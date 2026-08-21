import itertools
import os
import threading

from ollama import Client

HOST = 'https://ollama.com'
KEY_ENV_PREFIX = 'OLLAMA_API_KEY_'

MODEL = 'minimax-m3'  # vision-capable, free tier; qwen3-vl:32b returned 404 "model not found" against this Ollama cloud account (process.log, 2026-08-21 12:41) - confirm the correct cloud-side tag before switching back

INVOICE_DIR = 'invoice'
PDF_ZOOM = 2  # render scale; higher = sharper but slower/bigger
OUTPUT_PATH = 'out.json'
LOG_PATH = 'process.log'
SHARPENED_DIR = 'output'
SAVE_DEBUG_PAGES = False  # write each rendered page to SHARPENED_DIR as a debug PDF - turn back on if you need to eyeball what the model actually saw

# Fill in as many keys as you have - 1 is fine, so is 8. Requests rotate
# across whichever ones are non-empty here. An OLLAMA_API_KEY_<n> environment
# variable, if set, overrides the entry at that position (1-indexed).
API_KEYS = [
            'a02fc9d9ca954e8f8e801031d741a9f4.8YkE0poV42BUpJM5aSdcpERx',
            '5c142e3dddbe4c00af461beb686cfd36.qA5pDYNm0V79DCnjL7mXV6Gy',
            '08cd689726cb40169543db1ac28fe0e2.silxzK-S0NjozIFgWon0Gyx9',
            'bfb4bf4cdef8469c8841ba02e1d30b0b.dzIH64NMy-F0m6AxZ1h9mB_q',
            'b3e6113f430b40adb69af4250e92013e.V3erJlkPIvn5J_Hnh_whesfJ',
            'cc76568fb2e040988cf13671da7b9d19.RcuniqYYf7DpS8xqgeOP1gRt',
            ]

MAX_WORKERS = 6  # concurrent Ollama requests, shared by every caller in this process (CLI batch run + all API requests) so load never exceeds this regardless of how many PDFs come in at once; extra pages simply wait in the executor's internal queue. Matches len(API_KEYS) so round-robin never puts two in-flight requests on the same key at once - raise this only if testing shows the free tier tolerates >1 concurrent request per key.
MAX_RETRIES = 2  # retries after the first attempt (3 attempts total per page)
RETRY_BACKOFF_BASE = 2  # seconds
RETRY_BACKOFF_CAP = 30  # seconds
RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}

SHARPEN_RADIUS = 2  # UnsharpMask: pixel radius of the blur used to detect edges
SHARPEN_PERCENT = 200  # UnsharpMask: strength of the sharpening effect
SHARPEN_THRESHOLD = 1  # UnsharpMask: minimum brightness change to be sharpened (avoids amplifying noise)
CONTRAST_CUTOFF = 1  # autocontrast: percent of darkest/lightest pixels clipped before stretching the range

# A page counts as blank (and never gets sent to the model at all - see
# model.py: _is_blank_page) when the fraction of pixels darker than
# BLANK_PAGE_INK_THRESHOLD is below BLANK_PAGE_INK_FRACTION. Checked on the
# raw render before autocontrast/sharpening: autocontrast stretches
# whatever faint noise exists on a blank scanned page across the full
# 0-255 range, which would make it look artificially content-rich if
# checked afterwards instead. Raise BLANK_PAGE_INK_FRACTION if a real,
# very sparse invoice page starts getting skipped; lower it if a blank
# page with heavier scanner noise/artifacts still reaches the model.
BLANK_PAGE_INK_THRESHOLD = 200
BLANK_PAGE_INK_FRACTION = 0.0005

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
_clients_cycle = itertools.cycle(range(len(_clients)))

# Indices into _clients that have reported a weekly-usage-limit 429 (see
# mark_exhausted) - that quota doesn't reset until Ollama's next weekly
# window, so a dead key stays skipped for the rest of this process instead
# of being retried into the same error over and over.
_dead_clients: set[int] = set()


def get_client() -> Client:
    """Return the next live client in round-robin rotation across all
    configured API keys not yet marked exhausted, so requests are spread
    across accounts instead of exhausting one free-tier limit."""
    with _lock:
        for _ in range(len(_clients)):
            index = next(_clients_cycle)
            if index not in _dead_clients:
                return _clients[index]
    raise RuntimeError("All configured Ollama API keys have hit their weekly usage limit.")


def mark_exhausted(client: Client) -> None:
    """Mark a client's API key as exhausted for the rest of this process -
    call this when a request against it fails with a weekly-usage-limit 429
    (see model.py: extract_receipt). Retrying that same key again is pure
    waste since the quota won't clear until next week."""
    with _lock:
        try:
            index = _clients.index(client)
        except ValueError:
            return
        _dead_clients.add(index)
