import os
import threading

from dotenv import load_dotenv
from ollama import Client

load_dotenv()  # reads .env into the process environment (if present) before
                # _load_keys() below checks os.environ - lets OLLAMA_API_KEY_1
                # etc. be set via .env locally instead of real env vars

HOST = 'https://ollama.com'
KEY_ENV_PREFIX = 'OLLAMA_API_KEY_'

MODEL = 'minimax-m3'  

INVOICE_DIR = 'invoice'
PDF_ZOOM = 2
OUTPUT_PATH = 'out.json'
LOG_PATH = 'process.log'
SHARPENED_DIR = 'output'
SAVE_DEBUG_PAGES = False
LOG_TO_CONSOLE = os.environ.get('LOG_TO_CONSOLE', 'true').strip().lower() not in ('0', 'false', 'no')  # run.sh
                            # turns this off - in the background stdout goes
                            # to run.log, which unlike process.log is never
                            # wiped, so it would grow for as long as it runs


API_KEYS: list[str] = []  # never hardcode a real key here - set
                          # OLLAMA_API_KEY_1, OLLAMA_API_KEY_2, ... instead


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


MAX_WORKERS = _int_env('MAX_WORKERS', 10)
PDF_WORKER_COUNT = _int_env('PDF_WORKER_COUNT', 5)  # how many PDFs (from
                      # /upload) can be rendered/processed concurrently -
                      # page-level OCR calls within those PDFs still share
                      # the MAX_WORKERS-sized executor above, so this mainly
                      # bounds concurrent PDF rendering (pymupdf get_pixmap
                      # is memory-heavy) rather than OCR throughput
UPLOAD_QUEUE_MAXSIZE = _int_env('UPLOAD_QUEUE_MAXSIZE', 2000)  # /upload
                            # replies 503 once this many tickets are queued
                            # and not yet picked up by a worker - cheap to
                            # size generously since queued entries are just
                            # ticket IDs, not PDF bytes (see jobs.PENDING_DIR)
MAX_UPLOAD_MB = _int_env('MAX_UPLOAD_MB', 25)  # /upload rejects (413) any
                            # PDF larger than this, streamed check - keeps a
                            # single oversized file from exhausting memory
                            # or disk when many uploads arrive at once
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024

IMAP_HOST = os.environ.get('IMAP_HOST', '').strip()
IMAP_PORT = _int_env('IMAP_PORT', 993)
IMAP_USE_SSL = os.environ.get('IMAP_USE_SSL', 'true').strip().lower() not in ('0', 'false', 'no')
IMAP_USERNAME = os.environ.get('IMAP_USERNAME', '').strip()
IMAP_PASSWORD = os.environ.get('IMAP_PASSWORD', '')  # with 2FA (Gmail etc.)
                            # this must be an app password, not the account
                            # password - IMAP logins with the latter fail
if IMAP_HOST.endswith('gmail.com'):
    IMAP_PASSWORD = IMAP_PASSWORD.replace(' ', '')  # Google shows app
                            # passwords in 4 groups of 4 - drop the spaces
IMAP_FOLDER = os.environ.get('IMAP_FOLDER', 'INBOX')
IMAP_PROCESSED_FOLDER = os.environ.get('IMAP_PROCESSED_FOLDER', '').strip()  # if
                            # set, fully-ingested emails are moved here
                            # instead of just being marked read
IMAP_POLL_INTERVAL_SECONDS = _int_env('IMAP_POLL_INTERVAL_SECONDS', 60)
IMAP_TIMEOUT_SECONDS = _int_env('IMAP_TIMEOUT_SECONDS', 30)
EMAIL_MAX_ATTEMPTS = max(1, _int_env('EMAIL_MAX_ATTEMPTS', 3))
EMAIL_ENABLED = bool(IMAP_HOST and IMAP_USERNAME and IMAP_PASSWORD)  # the
                            # email watcher and /api/v1/invoices/new only run
                            # when all three are set

MAX_RETRIES = 2
RETRY_BACKOFF_BASE = 2
RETRY_BACKOFF_CAP = 30
RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}

SHARPEN_RADIUS = 2
SHARPEN_PERCENT = 200
SHARPEN_THRESHOLD = 1
CONTRAST_CUTOFF = 1  


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
                continue
            break
        keys.append(key)
        i += 1
    if not keys:
        raise RuntimeError(
            f"No Ollama API keys found. Set {KEY_ENV_PREFIX}1, {KEY_ENV_PREFIX}2, ... "
            "in the environment (do not hardcode keys in source)."
        )
    return keys


_clients = [Client(host=HOST, headers={'Authorization': f"Bearer {key}"}) for key in _load_keys()]


_dead_clients: set[int] = set()


def get_client() -> Client:
    with _lock:
        for index in range(len(_clients)):
            if index not in _dead_clients:
                return _clients[index]
    raise RuntimeError("All configured Ollama API keys have hit their weekly usage limit.")


def mark_exhausted(client: Client) -> None:
    with _lock:
        try:
            index = _clients.index(client)
        except ValueError:
            return
        _dead_clients.add(index)
