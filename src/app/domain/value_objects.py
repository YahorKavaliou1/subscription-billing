"""Immutable value objects and shared validation helpers."""

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from app.domain.errors import InvalidValueError

MAX_ID_LENGTH = 64
# Matches payments.amount NUMERIC(19, 4)
AMOUNT_SCALE = 4
AMOUNT_MAX_DIGITS = 19
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")


def require_aware_utc(value: datetime, field: str) -> datetime:
    """Reject naive datetimes and normalize aware ones to UTC."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise InvalidValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def require_id(value: str, field: str, max_length: int = MAX_ID_LENGTH) -> str:
    stripped = value.strip()
    if not stripped:
        raise InvalidValueError(f"{field} must not be empty")
    if len(stripped) > max_length:
        raise InvalidValueError(f"{field} must be at most {max_length} characters")
    return stripped


@dataclass(frozen=True, slots=True)
class Money:
    """Positive amount in an ISO 4217 currency; exact decimal, never float."""

    amount: Decimal
    currency: str

    def __post_init__(self) -> None:
        amount = self._parse_amount(self.amount)
        currency = self.currency.strip().upper()
        if not _CURRENCY_RE.fullmatch(currency):
            raise InvalidValueError("currency must be a 3-letter ISO 4217 code")
        # Frozen dataclass: assign normalized values through object.__setattr__
        object.__setattr__(self, "amount", amount)
        object.__setattr__(self, "currency", currency)

    @staticmethod
    def _parse_amount(value: object) -> Decimal:
        if isinstance(value, float):
            raise InvalidValueError("amount must be a Decimal or string, not float")
        try:
            amount = Decimal(str(value)) if not isinstance(value, Decimal) else value
        except InvalidOperation as exc:
            raise InvalidValueError("amount must be a number") from exc
        if not amount.is_finite():
            raise InvalidValueError("amount must be finite")
        if amount <= 0:
            raise InvalidValueError("amount must be positive")
        exponent = amount.as_tuple().exponent
        if isinstance(exponent, int) and exponent < -AMOUNT_SCALE:
            raise InvalidValueError(f"amount must have at most {AMOUNT_SCALE} decimal places")
        if amount.adjusted() >= AMOUNT_MAX_DIGITS - AMOUNT_SCALE:
            raise InvalidValueError("amount is too large")
        return amount
