CHATPDF_API_KEY = 'sec_Y4MuNK5nCn6zR1tLHBBZllXDRtHefciu'

CHATPDF_BASE_URL = 'https://api.chatpdf.com/v1'
CHATPDF_ADD_FILE_URL = f"{CHATPDF_BASE_URL}/sources/add-file"
CHATPDF_MESSAGE_URL = f"{CHATPDF_BASE_URL}/chats/message"
CHATPDF_DELETE_URL = f"{CHATPDF_BASE_URL}/sources/delete"

INVOICE_DIR = 'invoice'
OUTPUT_PATH = 'out.json'
LOG_PATH = 'process.log'

MAX_WORKERS = 10  # concurrent ChatPDF requests, shared by every caller in this process (CLI batch run + all API requests) so load never exceeds this regardless of how many PDFs come in at once; extras simply wait in the executor's internal queue
MAX_RETRIES = 2  # retries after the first attempt (3 attempts total per PDF)
RETRY_BACKOFF_BASE = 2  # seconds
RETRY_BACKOFF_CAP = 30  # seconds
RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}
