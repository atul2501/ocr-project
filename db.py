"""Postgres connection pool for jobs.py's durable job store.

Render wipes local disk on every redeploy, so anything that needs to
survive a redeploy (not just an in-process crash) has to live in a real
database instead of a local file. DATABASE_URL is provided automatically
when a Postgres instance is linked to the service on Render; locally, set
it in .env the same way as the OLLAMA_API_KEY_* variables.
"""

import json
import logging
import os
from typing import Optional

import asyncpg
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

DATABASE_URL = os.environ.get('DATABASE_URL')

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    ticket_id TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    progress INTEGER NOT NULL,
    message TEXT NOT NULL,
    total_pages INTEGER NOT NULL DEFAULT 0,
    result JSONB,
    error TEXT,
    sap_status TEXT NOT NULL,
    pdf_bytes BYTEA,
    created_at DOUBLE PRECISION NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_content_hash_idx ON jobs (content_hash);
CREATE INDEX IF NOT EXISTS jobs_created_at_idx ON jobs (created_at);
"""

_pool: Optional[asyncpg.Pool] = None


async def _init_connection(conn: asyncpg.Connection) -> None:
    # lets asyncpg hand back/accept Python lists/dicts for the jsonb
    # `result` column directly, instead of raw JSON text
    await conn.set_type_codec(
        'jsonb', encoder=json.dumps, decoder=json.loads, schema='pg_catalog',
    )


async def connect() -> None:
    global _pool
    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL is not set - add a Postgres database and set DATABASE_URL "
            "in the environment (on Render: create a Postgres instance and add its "
            "connection string as an env var on this service)."
        )
    _pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10, init=_init_connection)
    async with _pool.acquire() as conn:
        await conn.execute(_SCHEMA)
    logger.info("connected to database and ensured jobs table exists")


async def disconnect() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
        logger.info("closed database pool")


def pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Database pool not initialized - call db.connect() at startup")
    return _pool
