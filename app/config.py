import math
import os
from dataclasses import dataclass
from urllib.parse import quote, urlsplit


class ConfigurationError(ValueError):
    pass


def _integer(name: str, default: int, minimum: int = 1) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if value < minimum:
        raise ConfigurationError(f"{name} must be at least {minimum}")
    return value


def _number(name: str, default: float, minimum: float = 0.0) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a number") from exc
    if not math.isfinite(value) or value < minimum:
        raise ConfigurationError(f"{name} must be at least {minimum}")
    return value


@dataclass(frozen=True)
class Settings:
    database_dsn: str
    redis_url: str
    skip_startup_init: bool
    database_retry_attempts: int
    retry_base_delay: float
    retry_max_delay: float
    cache_ttl_seconds: int
    log_level: str


def get_settings() -> Settings:
    attempts = _integer("DATABASE_RETRY_ATTEMPTS", 5)
    retry_base_delay = _number("RETRY_BASE_DELAY_SECONDS", 0.1)
    retry_max_delay = _number("RETRY_MAX_DELAY_SECONDS", 2.0)
    if retry_max_delay < retry_base_delay:
        raise ConfigurationError(
            "RETRY_MAX_DELAY_SECONDS must be greater than or equal to "
            "RETRY_BASE_DELAY_SECONDS"
        )

    database_url = os.getenv("DATABASE_URL")
    if database_url:
        try:
            parsed_database_url = urlsplit(database_url)
            database_url_valid = (
                parsed_database_url.scheme in {"postgres", "postgresql"}
                and bool(parsed_database_url.hostname)
                and bool(parsed_database_url.path.strip("/"))
            )
        except ValueError:
            database_url_valid = False
        if (
            not database_url_valid
        ):
            raise ConfigurationError("DATABASE_URL must use PostgreSQL")
        database_dsn = database_url
    else:
        host = os.getenv("POSTGRES_HOST", "localhost")
        if not host.strip():
            raise ConfigurationError("POSTGRES_HOST must not be empty")
        port = _integer("POSTGRES_PORT", 5432)
        if port > 65535:
            raise ConfigurationError("POSTGRES_PORT must be between 1 and 65535")
        user_value = os.getenv("POSTGRES_USER", "postgres")
        database_value = os.getenv("POSTGRES_DB", "incident_triage")
        if not user_value.strip() or not database_value.strip():
            raise ConfigurationError("POSTGRES_USER and POSTGRES_DB must not be empty")
        user = quote(user_value, safe="")
        password = quote(os.getenv("POSTGRES_PASSWORD", ""), safe="")
        database = quote(database_value, safe="")
        credentials = user + (f":{password}" if password else "")
        database_dsn = (
            f"postgresql://{credentials}@{host}:{port}/{database}?connect_timeout=5"
        )

    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    if not redis_url.startswith(("redis://", "rediss://", "unix://")):
        raise ConfigurationError("REDIS_URL must use redis://, rediss://, or unix://")

    skip_startup_value = os.getenv("SKIP_STARTUP_INIT", "").lower()
    if skip_startup_value not in {"", "1", "true", "yes", "0", "false", "no"}:
        raise ConfigurationError("SKIP_STARTUP_INIT must be a boolean value")

    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ConfigurationError("LOG_LEVEL must be a valid logging level")

    return Settings(
        database_dsn=database_dsn,
        redis_url=redis_url,
        skip_startup_init=skip_startup_value in {"1", "true", "yes"},
        database_retry_attempts=attempts,
        retry_base_delay=retry_base_delay,
        retry_max_delay=retry_max_delay,
        cache_ttl_seconds=_integer("CACHE_TTL_SECONDS", 3600),
        log_level=log_level,
    )
