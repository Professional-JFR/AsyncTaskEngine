import pytest

from app.config import ConfigurationError, get_settings


def test_settings_use_safe_local_defaults(monkeypatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
    monkeypatch.delenv("REDIS_URL", raising=False)

    settings = get_settings()

    assert settings.database_dsn.startswith("postgresql://postgres@localhost:5432/")
    assert settings.redis_url == "redis://localhost:6379/0"
    assert settings.database_retry_attempts == 5


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("DATABASE_RETRY_ATTEMPTS", "0", "at least 1"),
        ("POSTGRES_PORT", "70000", "between 1 and 65535"),
        ("REDIS_URL", "http://localhost", "must use"),
        ("LOG_LEVEL", "VERBOSE", "valid logging level"),
        ("DATABASE_URL", "postgresql://", "must use PostgreSQL"),
        ("DATABASE_URL", "postgresql://[broken", "must use PostgreSQL"),
        ("RETRY_BASE_DELAY_SECONDS", "nan", "must be at least"),
    ],
)
def test_invalid_configuration_is_rejected(monkeypatch, name, value, message) -> None:
    monkeypatch.setenv(name, value)

    with pytest.raises(ConfigurationError, match=message):
        get_settings()


def test_retry_maximum_must_not_be_lower_than_base(monkeypatch) -> None:
    monkeypatch.setenv("RETRY_BASE_DELAY_SECONDS", "2")
    monkeypatch.setenv("RETRY_MAX_DELAY_SECONDS", "1")

    with pytest.raises(ConfigurationError, match="greater than or equal"):
        get_settings()
