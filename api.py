import itertools
import os
import threading

from ollama import Client

HOST = 'https://ollama.com'
KEY_ENV_PREFIX = 'OLLAMA_API_KEY_'

MODEL = 'minimax-m3'  

INVOICE_DIR = 'invoice'
PDF_ZOOM = 2
OUTPUT_PATH = 'out.json'
LOG_PATH = 'process.log'
SHARPENED_DIR = 'output'
SAVE_DEBUG_PAGES = False


API_KEYS = [
            'a02fc9d9ca954e8f8e801031d741a9f4.8YkE0poV42BUpJM5aSdcpERx',
            '5c142e3dddbe4c00af461beb686cfd36.qA5pDYNm0V79DCnjL7mXV6Gy',
            '08cd689726cb40169543db1ac28fe0e2.silxzK-S0NjozIFgWon0Gyx9',
            'bfb4bf4cdef8469c8841ba02e1d30b0b.dzIH64NMy-F0m6AxZ1h9mB_q',
            'b3e6113f430b40adb69af4250e92013e.V3erJlkPIvn5J_Hnh_whesfJ',
            'cc76568fb2e040988cf13671da7b9d19.RcuniqYYf7DpS8xqgeOP1gRt',
            ]

MAX_WORKERS = 10 
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
            "No Ollama API keys found. Add at least one to API_KEYS in api.py, "
            f"or set {KEY_ENV_PREFIX}1, {KEY_ENV_PREFIX}2, ... in the environment."
        )
    return keys


_clients = [Client(host=HOST, headers={'Authorization': f"Bearer {key}"}) for key in _load_keys()]
_clients_cycle = itertools.cycle(range(len(_clients)))


_dead_clients: set[int] = set()


def get_client() -> Client:
    with _lock:
        for _ in range(len(_clients)):
            index = next(_clients_cycle)
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
