import pytest
from pydantic import ValidationError

from app.config import Settings

REQUIRED_ENV = {
    "APP_DATABASE_URL": "postgresql+asyncpg://user:secret@localhost:5432/billing",
    "APP_RABBITMQ_URL": "amqp://user:secret@localhost:5672/",
    "APP_API_KEY": "test-key",
}


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Environment with only the required variables set."""
    for name, value in REQUIRED_ENV.items():
        monkeypatch.setenv(name, value)
    return monkeypatch


def make_settings() -> Settings:
    # Ignore the developer's local .env so tests depend only on the environment
    return Settings(_env_file=None)


def test_defaults_are_applied(env: pytest.MonkeyPatch) -> None:
    settings = make_settings()

    assert settings.env == "local"
    assert settings.log_level == "INFO"
    assert settings.notification_offsets_days == (3, 1, 0)
    assert settings.notification_sender == "log"


def test_values_are_read_from_environment(env: pytest.MonkeyPatch) -> None:
    env.setenv("APP_LOG_LEVEL", "debug")
    env.setenv("APP_LOG_JSON", "false")
    env.setenv("APP_RELAY_BATCH_SIZE", "50")
    env.setenv("APP_NOTIFICATION_OFFSETS_DAYS", "[0, 7, 1, 7]")

    settings = make_settings()

    assert settings.log_level == "DEBUG"
    assert settings.log_json is False
    assert settings.relay_batch_size == 50
    assert settings.notification_offsets_days == (7, 1, 0)


@pytest.mark.parametrize("missing", sorted(REQUIRED_ENV))
def test_required_variable_missing(env: pytest.MonkeyPatch, missing: str) -> None:
    env.delenv(missing)

    with pytest.raises(ValidationError, match=missing.removeprefix("APP_").lower()):
        make_settings()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("APP_DATABASE_URL", "postgresql://user:secret@localhost/billing"),
        ("APP_RABBITMQ_URL", "http://localhost:5672/"),
        ("APP_LOG_LEVEL", "VERBOSE"),
        ("APP_RELAY_BATCH_SIZE", "0"),
        ("APP_NOTIFICATION_OFFSETS_DAYS", "[]"),
        ("APP_NOTIFICATION_OFFSETS_DAYS", "[3, -1]"),
        ("APP_NOTIFICATION_SENDER", "sms"),
    ],
)
def test_invalid_values_are_rejected(env: pytest.MonkeyPatch, name: str, value: str) -> None:
    env.setenv(name, value)

    with pytest.raises(ValidationError):
        make_settings()


def test_secrets_are_not_exposed_in_repr(env: pytest.MonkeyPatch) -> None:
    settings = make_settings()

    assert "secret" not in repr(settings)
    assert "test-key" not in repr(settings)
    assert settings.api_key.get_secret_value() == "test-key"
