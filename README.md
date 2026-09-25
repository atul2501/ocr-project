# Receipt OCR API

Pulls invoice data out of PDF files and returns it as JSON. Each PDF page is
rendered to an image, cleaned up, and sent to a vision model on Ollama Cloud.
The results are then grouped back into whole invoices.

PDFs can come in two ways:

- **HTTP upload**: send a PDF to `/extract` or `/upload`.
- **Email (optional)**: the server checks a mailbox (IMAP) every 30 seconds.
  Any new email with PDF attachments is processed automatically, and the
  results are collected from `/api/v1/invoices/new`.

---

## 1. Requirements

- **Python 3.10 or newer** (`python3 --version` or `python --version`)
- **Bash**:
  - Linux / macOS: already installed.
  - Windows: use **Git Bash** (comes with [Git for Windows](https://git-scm.com/download/win)).
- **At least one Ollama Cloud API key** (from https://ollama.com).
- For email intake: an IMAP mailbox. For Gmail you need an **app password** (see step 3).

On Ubuntu / Debian, also install the venv module once:

```bash
sudo apt install python3-venv
```

## 2. Get the code

```bash
git clone <repo-url> ocr-project
cd ocr-project
chmod +x run.sh        # Linux / macOS only, if it isn't executable already
```

## 3. Create the `.env` file

All settings and secrets live in `.env`, which git ignores and never commits.
Start from the example:

```bash
cp .env.example .env
```

Then open `.env` and fill in at least:

```ini
OLLAMA_API_KEY_1=your-ollama-key
# optional extra keys, tried in order when one hits its weekly limit:
# OLLAMA_API_KEY_2=...
```

**Email intake (optional).** Fill these in to switch it on. To switch it off,
leave `IMAP_PASSWORD` empty.

```ini
IMAP_HOST=imap.gmail.com
IMAP_USERNAME=you@example.com
IMAP_PASSWORD=your-app-password
IMAP_POLL_INTERVAL_SECONDS=30
```

To get a Gmail app password:

1. Google Account → Security → turn on **2-Step Verification**.
2. Google Account → Security → **App passwords** → create one and paste it
   into `IMAP_PASSWORD`. Spaces are fine.
3. Gmail → Settings → **Forwarding and POP/IMAP** → **Enable IMAP**.

The other settings are explained in `.env.example`; you can leave them at their defaults.

## 4. Run it

```bash
./run.sh
```

The first run takes a minute or two, because it:

1. creates a virtual environment in `.venv/`
2. installs everything in `requirements.txt` into it
3. starts the server **in the background** on port `8000`

Later runs skip steps 1 and 2 and start in about a second. If you change
`requirements.txt`, the next `./run.sh` reinstalls automatically.

Check that it's up:

```bash
curl http://localhost:8000/health
```

Interactive API docs are at http://localhost:8000/docs.

### All commands

| Command             | What it does                                                    |
|---------------------|-----------------------------------------------------------------|
| `./run.sh`          | Start in the background (same as `./run.sh start`)              |
| `./run.sh stop`     | Stop it                                                         |
| `./run.sh restart`  | Stop, then start (use this after editing code or `.env`)        |
| `./run.sh status`   | Show whether it's running                                       |
| `./run.sh logs`     | Show `process.log` live (Ctrl+C only stops watching the log)    |
| `./run.sh fg`       | Run in the foreground in this terminal (Ctrl+C stops it)        |
| `./run.sh setup`    | Only create `.venv` / install requirements, don't start          |

To use a different port or address:

```bash
PORT=9000 ./run.sh
HOST=127.0.0.1 PORT=9000 ./run.sh restart
```

### Keeping it running

Once started, the server:

- **keeps running after you close the terminal** or log out of SSH
- **restarts itself** if it crashes. If it keeps crashing right away (for
  example a bad `.env` or a port already in use), it waits longer between
  tries: 5s, 10s, 20s, and at most 60s.
- stays down only after `./run.sh stop`

It does **not** start again by itself after the machine reboots. On Linux,
add it to cron to handle that:

```bash
crontab -e
# add this line (use the real full path):
@reboot /home/you/ocr-project/run.sh start
```

A laptop that sleeps or installs Windows updates will interrupt it. For
24/7 use, run it on an always-on server.

> Always run it through `run.sh` with **one** worker. Don't add
> `--workers 2` or more. The job queue, tickets and email watcher live in
> memory, so a second worker would check the same mailbox again and process
> every email twice.

## 5. Using the API

Base URL: `http://<server>:8000`

### Quick: one PDF, wait for the result

```bash
curl -X POST http://localhost:8000/extract \
     --data-binary @invoice.pdf \
     -H "Content-Type: application/pdf"
```

Returns the extracted invoices as JSON. The request stays open until
processing is done.

### Recommended: upload, then check the status

```bash
curl -X POST http://localhost:8000/upload \
     --data-binary @invoice.pdf \
     -H "Content-Type: application/pdf"
# -> {"success": true, "ticket_id": "TCK-1a2b3c4d5e6f", "status": "QUEUED", ...}

curl http://localhost:8000/status/TCK-1a2b3c4d5e6f
# -> status goes QUEUED -> OCR_PROCESSING -> VALIDATING -> COMPLETED (or FAILED)
#    and "result" holds the invoices once it's COMPLETED
```

- In Postman: Body → **binary** → pick the PDF.
- Uploading the same PDF again returns its existing ticket and doesn't
  process it twice.
- The size limit is `MAX_UPLOAD_MB` (default 25 MB). If the queue is full,
  the server replies `503`; try again shortly.
- Tickets are kept for 7 days.

### Email: collect the results

```bash
curl http://localhost:8000/api/v1/invoices/new
# -> {"count": 1, "still_processing": 0, "items": [{"ticket_id": ..., "email": {...}, "status": "COMPLETED", "result": [...]}]}
```

- Each result is returned **only once**. The next call returns only what
  finished since the last call. Save what you get back.
- Only emails that arrive **while the server is running** are read. Mail
  already in the inbox at startup, or mail that arrives while it's stopped,
  is skipped (it stays unread).
- An email is marked as read once all its PDFs are done. If
  `IMAP_PROCESSED_FOLDER` is set, it's moved to that folder instead.

### All endpoints

| Method | Path                     | Purpose                                   |
|--------|--------------------------|-------------------------------------------|
| GET    | `/`                      | List of endpoints                         |
| GET    | `/health`                | Liveness check                            |
| POST   | `/extract`               | PDF in, invoices out (waits for result)   |
| POST   | `/upload`                | PDF in, ticket ID out (processes later)   |
| GET    | `/status/{ticket_id}`    | Progress and result of an upload          |
| GET    | `/api/v1/invoices/new`   | New results from emailed PDFs             |
| GET    | `/docs`                  | Interactive API docs (Swagger)            |

## 6. Files and logs

| Path                  | What it is                                                         |
|-----------------------|--------------------------------------------------------------------|
| `process.log`         | Main log: every upload, page, email and error. Wiped every 24 h.    |
| `run.log`             | Server starts/restarts and crash errors. Emptied at 10 MB.          |
| `run.pid`             | Process ID of the running server (used by `stop`/`status`)         |
| `cache/`              | One JSON file per ticket, kept 7 days                              |
| `pending/`            | PDFs waiting to be processed                                       |
| `mailbox_state.json`  | Email results not yet collected from `/api/v1/invoices/new`        |
| `.venv/`              | Python virtual environment, created by `run.sh`                    |

All of these are git-ignored. Stopping and starting again keeps tickets and
uncollected email results, and PDFs that were half-processed get picked up again.

## 7. Troubleshooting

**It says "Started" but nothing answers on the port.** Look at `run.log`:

```bash
tail -50 run.log
```

- `address already in use` / `only one usage of each socket address`:
  another program is using port 8000. Stop that program, or run on another
  port: `PORT=8001 ./run.sh restart`.
- `No Ollama API keys found`: set `OLLAMA_API_KEY_1` in `.env`.

**Email isn't being picked up.** Look in `process.log` for `[email]` lines:

```bash
grep "\[email\]" process.log | tail -20
```

- No `started email watcher` line: `IMAP_HOST`, `IMAP_USERNAME` or
  `IMAP_PASSWORD` is missing from `.env`.
- `AUTHENTICATIONFAILED` / login errors: use an app password (step 3),
  and make sure IMAP is enabled in Gmail.
- Only emails **with a PDF attachment** that arrive **after** the server
  started are processed.

**Installing requirements failed.** Fix the error pip printed (usually no
internet or an old Python), then run `./run.sh` again. It picks up where
it stopped. To rebuild from scratch:

```bash
./run.sh stop
rm -rf .venv
./run.sh
```

**Moved the project between Windows and Linux.** A `.venv` from the other
system can't be used. `run.sh` notices this and rebuilds it automatically.
