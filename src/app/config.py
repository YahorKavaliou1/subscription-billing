"""Application settings loaded from environment variables (prefix ``APP_``)."""

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="APP_",
        env_file=".env",
        env_file_encoding="utf-8",
        # .env also holds variables for docker containers (POSTGRES_*, RABBITMQ_*)
        extra="ignore",
        frozen=True,
    )

    env: Literal["local", "test", "prod"] = "local"

    # --- Connections (secrets: never printed in repr / logs)
    database_url: SecretStr
    database_pool_size: int = Field(default=10, ge=1)
    database_max_overflow: int = Field(default=10, ge=0)
    rabbitmq_url: SecretStr
    rabbitmq_exchange: str = "billing.events"

    # --- API
    api_key: SecretStr
    log_level: LogLevel = "INFO"
    log_json: bool = True

    # --- Outbox relay
    relay_batch_size: int = Field(default=100, ge=1)
    relay_poll_interval_seconds: float = Field(default=0.5, gt=0)
    relay_max_attempts: int = Field(default=10, ge=1)
    relay_backoff_base_seconds: float = Field(default=1, gt=0)
    relay_backoff_max_seconds: float = Field(default=300, gt=0)

    # --- Notification scheduler
    scheduler_batch_size: int = Field(default=100, ge=1)
    scheduler_poll_interval_seconds: float = Field(default=5, gt=0)
    notification_offsets_days: tuple[int, ...] = (3, 1, 0)

    # --- Consumers
    consumer_max_delivery_attempts: int = Field(default=5, ge=1)
    consumer_prefetch_count: int = Field(default=20, ge=1)

    # --- Notification sender implementation
    notification_sender: Literal["log", "email"] = "log"

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_log_level(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @field_validator("database_url")
    @classmethod
    def _check_database_scheme(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().startswith("postgresql+asyncpg://"):
            raise ValueError("database_url must use the postgresql+asyncpg:// scheme")
        return value

    @field_validator("rabbitmq_url")
    @classmethod
    def _check_rabbitmq_scheme(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().startswith(("amqp://", "amqps://")):
            raise ValueError("rabbitmq_url must use the amqp:// or amqps:// scheme")
        return value

    @field_validator("notification_offsets_days")
    @classmethod
    def _normalize_offsets(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if not value:
            raise ValueError("at least one notification offset is required")
        if any(days < 0 for days in value):
            raise ValueError("notification offsets must be non-negative")
        # Unique, from the earliest reminder to the expiry day
        return tuple(sorted(set(value), reverse=True))


@lru_cache
def get_settings() -> Settings:
    """Return process-wide settings; read once on first call."""
    # Required fields come from the environment
    return Settings()
