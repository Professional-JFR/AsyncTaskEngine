import json
import logging
import random
import time
import uuid
from datetime import datetime
from hashlib import sha256
from typing import Callable, Optional, TypeVar

import psycopg
import redis

from app.config import get_settings
from app.errors import IdempotencyConflictError, StorageError, StorageUnavailableError

logger = logging.getLogger(__name__)
T = TypeVar("T")
_redis_client: Optional[redis.Redis] = None


def _db_connect():
    return psycopg.connect(get_settings().database_dsn)


def _run_db(operation: Callable[[], T]) -> T:
    settings = get_settings()
    for attempt in range(1, settings.database_retry_attempts + 1):
        try:
            return operation()
        except (psycopg.OperationalError, psycopg.InterfaceError) as exc:
            if attempt == settings.database_retry_attempts:
                logger.error(
                    "Database operation exhausted retries",
                    extra={
                        "event": "database.retry_exhausted",
                        "attempt": attempt,
                        "error_type": type(exc).__name__,
                    },
                )
                raise StorageUnavailableError(
                    f"Database operation failed after {attempt} attempts"
                ) from exc
            delay = min(
                settings.retry_base_delay * 2 ** (attempt - 1),
                settings.retry_max_delay,
            )
            delay = random.uniform(delay / 2, delay)
            logger.warning(
                "Retrying transient database operation",
                extra={
                    "event": "database.retry",
                    "attempt": attempt,
                    "delay_seconds": round(delay, 3),
                    "error_type": type(exc).__name__,
                },
            )
            time.sleep(delay)
        except psycopg.Error as exc:
            raise StorageError("Database operation failed") from exc
    raise AssertionError("Database retry loop exited unexpectedly")


def init_storage() -> None:
    def initialize() -> None:
        with _db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS incidents (
                        incident_id TEXT PRIMARY KEY,
                        title TEXT NOT NULL,
                        description TEXT NOT NULL,
                        service TEXT,
                        source TEXT,
                        occurred_at TIMESTAMPTZ,
                        predicted_severity TEXT NOT NULL,
                        duplicate_of TEXT,
                        runbook_suggestion TEXT NOT NULL,
                        status TEXT NOT NULL,
                        idempotency_key TEXT,
                        request_fingerprint TEXT,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """
                )
                cur.execute(
                    "ALTER TABLE incidents ADD COLUMN IF NOT EXISTS idempotency_key TEXT"
                )
                cur.execute(
                    "ALTER TABLE incidents ADD COLUMN IF NOT EXISTS request_fingerprint TEXT"
                )
                cur.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS incidents_idempotency_key_idx
                    ON incidents (idempotency_key)
                    """
                )
            conn.commit()

    _run_db(initialize)


def ping_db() -> bool:
    try:
        def ping() -> None:
            with _db_connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                    cur.fetchone()

        _run_db(ping)
        return True
    except StorageError:
        logger.warning("Database health check failed", extra={"event": "database.health_failed"})
        return False


def _get_redis_client() -> redis.Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.Redis.from_url(
            get_settings().redis_url, decode_responses=True
        )
    return _redis_client


def ping_redis() -> bool:
    try:
        return bool(_get_redis_client().ping())
    except redis.exceptions.RedisError:
        logger.warning("Redis health check failed", extra={"event": "redis.health_failed"})
        return False


def _cache_incident(record: dict) -> None:
    try:
        client = _get_redis_client()
        client.setex(
            f"incident:{record['incident_id']}",
            get_settings().cache_ttl_seconds,
            json.dumps(record, default=str),
        )
        client.lpush("recent_incidents", record["incident_id"])
        client.ltrim("recent_incidents", 0, 49)
    except redis.exceptions.RedisError:
        logger.warning(
            "Incident cache write failed",
            extra={"event": "incident.cache_write_failed", "incident_id": record["incident_id"]},
        )


def _record_from_row(row: tuple) -> dict:
    return {
        "incident_id": row[0],
        "title": row[1],
        "description": row[2],
        "service": row[3],
        "source": row[4],
        "timestamp": row[5].isoformat() if row[5] else None,
        "predicted_severity": row[6],
        "duplicate_of": row[7],
        "runbook_suggestion": row[8],
        "status": row[9],
    }


def _request_fingerprint(
    title: str,
    description: str,
    service: Optional[str],
    source: Optional[str],
    timestamp: Optional[str],
) -> str:
    payload = {
        "title": title,
        "description": description,
        "service": service,
        "source": source,
        "timestamp": timestamp,
    }
    return sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def create_incident_record(
    title: str,
    description: str,
    service: Optional[str],
    source: Optional[str],
    occurred_at: Optional[datetime],
    predicted_severity: str,
    duplicate_of: Optional[str],
    runbook_suggestion: str,
    idempotency_key: Optional[str] = None,
) -> dict:
    incident_id = f"INC-{uuid.uuid4().hex[:8].upper()}"
    record = {
        "incident_id": incident_id,
        "title": title,
        "description": description,
        "service": service,
        "source": source,
        "timestamp": occurred_at.isoformat() if occurred_at else None,
        "predicted_severity": predicted_severity,
        "duplicate_of": duplicate_of,
        "runbook_suggestion": runbook_suggestion,
        "status": "triaged",
    }
    fingerprint = _request_fingerprint(
        title, description, service, source, record["timestamp"]
    )

    def insert() -> dict:
        with _db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO incidents (
                        incident_id, title, description, service, source, occurred_at,
                        predicted_severity, duplicate_of, runbook_suggestion, status,
                        idempotency_key, request_fingerprint
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (idempotency_key) DO NOTHING
                    RETURNING incident_id
                    """,
                    (
                        incident_id,
                        title,
                        description,
                        service,
                        source,
                        occurred_at,
                        predicted_severity,
                        duplicate_of,
                        runbook_suggestion,
                        "triaged",
                        idempotency_key,
                        fingerprint,
                    ),
                )
                inserted = cur.fetchone()
                if inserted is None and idempotency_key:
                    cur.execute(
                        """
                        SELECT incident_id, title, description, service, source,
                               occurred_at, predicted_severity, duplicate_of,
                               runbook_suggestion, status, request_fingerprint
                        FROM incidents WHERE idempotency_key = %s
                        """,
                        (idempotency_key,),
                    )
                    existing = cur.fetchone()
                    if existing is None:
                        raise StorageError("Unable to retrieve idempotent incident")
                    if existing[10] != fingerprint:
                        raise IdempotencyConflictError(
                            "Idempotency-Key was already used for a different request"
                        )
                    result = _record_from_row(existing)
                    conn.commit()
                    return result
            conn.commit()
        return record

    created_record = _run_db(insert)
    _cache_incident(created_record)
    return created_record


def fetch_recent_incidents(limit: int = 20) -> list[dict]:
    def fetch() -> list[dict]:
        with _db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT incident_id, title, description
                    FROM incidents
                    ORDER BY created_at DESC
                    LIMIT %s
                    """,
                    (limit,),
                )
                rows = cur.fetchall()
        return [
            {"incident_id": row[0], "title": row[1], "description": row[2]}
            for row in rows
        ]

    return _run_db(fetch)


def get_incident(incident_id: str) -> Optional[dict]:
    try:
        cached = _get_redis_client().get(f"incident:{incident_id}")
        if cached:
            return json.loads(cached)
    except (redis.exceptions.RedisError, json.JSONDecodeError):
        logger.warning(
            "Incident cache read failed",
            extra={"event": "incident.cache_read_failed", "incident_id": incident_id},
        )

    def fetch() -> Optional[dict]:
        with _db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT incident_id, title, description, service, source, occurred_at,
                           predicted_severity, duplicate_of, runbook_suggestion, status
                    FROM incidents
                    WHERE incident_id = %s
                    """,
                    (incident_id,),
                )
                row = cur.fetchone()
        return _record_from_row(row) if row else None

    record = _run_db(fetch)
    if record:
        _cache_incident(record)
    return record
