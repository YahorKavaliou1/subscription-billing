from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.domain.errors import InvalidValueError
from app.domain.value_objects import Money, require_aware_utc, require_id


class TestMoney:
    def test_normalizes_currency(self) -> None:
        money = Money(Decimal("9.99"), " eur ")

        assert money.currency == "EUR"
        assert money.amount == Decimal("9.99")

    def test_accepts_string_amount(self) -> None:
        assert Money("10.50", "USD").amount == Decimal("10.50")  # type: ignore[arg-type]

    def test_equal_regardless_of_trailing_zeros(self) -> None:
        assert Money(Decimal("10.0"), "EUR") == Money(Decimal("10.00"), "EUR")

    @pytest.mark.parametrize(
        "amount",
        [
            Decimal(0),
            Decimal("-1"),
            Decimal("NaN"),
            Decimal("Infinity"),
            Decimal("0.00001"),  # more than 4 decimal places
            Decimal("1e15"),  # does not fit NUMERIC(19, 4)
            "abc",
        ],
    )
    def test_rejects_invalid_amount(self, amount: object) -> None:
        with pytest.raises(InvalidValueError):
            Money(amount, "EUR")  # type: ignore[arg-type]

    def test_rejects_float(self) -> None:
        with pytest.raises(InvalidValueError, match="float"):
            Money(9.99, "EUR")  # type: ignore[arg-type]

    @pytest.mark.parametrize("currency", ["", "EU", "EURO", "E1R", "€"])
    def test_rejects_invalid_currency(self, currency: str) -> None:
        with pytest.raises(InvalidValueError, match="currency"):
            Money(Decimal(1), currency)

    def test_is_immutable(self) -> None:
        money = Money(Decimal(1), "EUR")

        with pytest.raises(AttributeError):
            money.amount = Decimal(2)  # type: ignore[misc]


class TestHelpers:
    def test_require_aware_utc_converts_to_utc(self) -> None:
        berlin = timezone(timedelta(hours=2))

        result = require_aware_utc(datetime(2026, 10, 8, 18, 0, tzinfo=berlin), "at")

        assert result == datetime(2026, 10, 8, 16, 0, tzinfo=UTC)
        assert result.tzinfo is UTC

    def test_require_aware_utc_rejects_naive(self) -> None:
        with pytest.raises(InvalidValueError, match="timezone-aware"):
            require_aware_utc(datetime(2026, 10, 8, 18, 0), "at")  # noqa: DTZ001

    def test_require_id_strips_whitespace(self) -> None:
        assert require_id("  sub-1 ", "id") == "sub-1"

    @pytest.mark.parametrize("value", ["", "   ", "x" * 65])
    def test_require_id_rejects_invalid(self, value: str) -> None:
        with pytest.raises(InvalidValueError):
            require_id(value, "id")
