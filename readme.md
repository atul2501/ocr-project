# Receipt OCR API

FastAPI service that OCRs invoice/receipt PDFs via Ollama's cloud API.

## Required environment variables

| Variable | Purpose |
|---|---|
| `OLLAMA_API_KEY_1`, `OLLAMA_API_KEY_2`, ... | Ollama Cloud API key(s) - at least one required. Multiple keys are rotated automatically if one hits its weekly usage limit. |
| `DATABASE_URL` | Postgres connection string. Tickets, their status/result, and in-flight PDF bytes are stored here so they survive a redeploy, not just an in-process crash (see `db.py`, `jobs.py`). |

Optional tuning vars (`MAX_WORKERS`, `PDF_WORKER_COUNT`, `UPLOAD_QUEUE_MAXSIZE`, `MAX_UPLOAD_MB`) are documented in `api.py`.

## Deploying on Render

1. Create a Postgres instance on Render (or use any managed Postgres) and copy its connection string into `DATABASE_URL` on this service. The `jobs` table is created automatically on startup if it doesn't exist - no manual migration step.
2. Set at least `OLLAMA_API_KEY_1` to a real Ollama Cloud API key.
3. Deploy - `uvicorn main:app` is the start command.

## Known limitations

- No authentication on `/upload`, `/extract`, or `/status/{ticket_id}` - anyone with the URL can call them. Fine for a trusted/internal deployment; add an API key or similar before exposing this publicly.
- The upload queue (`asyncio.Queue` in `main.py`) lives in a single process, so this only scales to one running instance/worker. Job *records* are safe in Postgres regardless, but horizontal scaling would need a shared queue (e.g. Redis) instead.
