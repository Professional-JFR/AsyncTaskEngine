import psycopg
import pytest
import redis

from app.config import Settings
from app.errors import StorageUnavailableError
from app import storage


def test_database_retry_uses_bounded_attempts_and_jitter(monkeypatch) -> None:
    settings = Settings(
        database_dsn="",
        redis_url="redis://localhost",
        skip_startup_init=True,
        database_retry_attempts=3,
        retry_base_delay=0.2,
        retry_max_delay=0.3,
        cache_ttl_seconds=60,
        log_level="INFO",
    )
    monkeypatch.setattr(storage, "get_settings", lambda: settings)
    monkeypatch.setattr(storage.random, "uniform", lambda low, high: high)
    delays = []
    monkeypatch.setattr(storage.time, "sleep", delays.append)
    calls = 0

    def flaky_operation() -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise psycopg.OperationalError("temporary failure")
        return "ok"

    assert storage._run_db(flaky_operation) == "ok"
    assert calls == 3
    assert delays == [0.2, 0.3]


def test_database_retry_exhaustion_raises_actionable_error(monkeypatch) -> None:
    settings = Settings(
        database_dsn="",
        redis_url="redis://localhost",
        skip_startup_init=True,
        database_retry_attempts=2,
        retry_base_delay=0,
        retry_max_delay=0,
        cache_ttl_seconds=60,
        log_level="INFO",
    )
    monkeypatch.setattr(storage, "get_settings", lambda: settings)
    monkeypatch.setattr(storage.random, "uniform", lambda low, high: high)
    monkeypatch.setattr(storage.time, "sleep", lambda delay: None)

    with pytest.raises(StorageUnavailableError, match="failed after 2 attempts"):
        storage._run_db(
            lambda: (_ for _ in ()).throw(psycopg.OperationalError("offline"))
        )


def test_cache_write_failure_does_not_fail_persisted_incident(monkeypatch) -> None:
    class BrokenCache:
        def setex(self, *args) -> None:
            raise redis.exceptions.ConnectionError("cache offline")

    monkeypatch.setattr(storage, "_get_redis_client", lambda: BrokenCache())

    storage._cache_incident({"incident_id": "INC-12345678"})


def test_cache_read_failure_falls_back_to_database(monkeypatch) -> None:
    class BrokenCache:
        def get(self, key: str) -> None:
            raise redis.exceptions.ConnectionError("cache offline")

    record = {
        "incident_id": "INC-12345678",
        "title": "Database down",
        "description": "Database unavailable",
        "service": None,
        "source": None,
        "timestamp": None,
        "predicted_severity": "critical",
        "duplicate_of": None,
        "runbook_suggestion": "RB-001",
        "status": "triaged",
    }
    monkeypatch.setattr(storage, "_get_redis_client", lambda: BrokenCache())
    monkeypatch.setattr(storage, "_run_db", lambda operation: record)
    monkeypatch.setattr(storage, "_cache_incident", lambda incident: None)

    assert storage.get_incident("INC-12345678") == record


def test_request_fingerprint_is_stable_for_same_client_input() -> None:
    first = storage._request_fingerprint(
        "Database down", "Connection errors", "database", "monitoring", None
    )
    second = storage._request_fingerprint(
        "Database down", "Connection errors", "database", "monitoring", None
    )

    assert first == second
    assert first != storage._request_fingerprint(
        "Database down", "Connection errors", "database", "webhook", None
    )
