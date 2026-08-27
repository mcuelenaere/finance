from datetime import date
from decimal import Decimal

import pytest

from finance.parsers.base import (
    UnknownAccountError,
    excel_serial_to_date,
    parse_date,
    parse_decimal,
    quantize_money,
    resolve_account,
)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("-9,68", Decimal("-9.68")),
        ("1.234,56", Decimal("1234.56")),  # the case that broke the original parsers
        ("-1.234,56", Decimal("-1234.56")),
        ("1.234.567,89", Decimal("1234567.89")),
        ("1234.56", Decimal("1234.56")),  # already dot-decimal
        ("250", Decimal("250")),
        ("+12,50", Decimal("12.50")),
        (" -8,00 ", Decimal("-8.00")),
        ("\xa01 000,00", Decimal("1000.00")),  # non-breaking space thousands separator
    ],
)
def test_parse_decimal(text, expected):
    assert parse_decimal(text) == expected


def test_parse_decimal_rejects_empty():
    with pytest.raises(ValueError, match="empty amount"):
        parse_decimal("   ")


def test_parse_date():
    assert parse_date("07/01/2020") == date(2020, 1, 7)


@pytest.mark.parametrize(
    "serial,expected",
    [
        ("44817.3800547801", date(2022, 9, 13)),
        ("45036.519849536999", date(2023, 4, 20)),
        (44431.536261574103, date(2021, 8, 23)),
    ],
)
def test_excel_serial_to_date(serial, expected):
    """Edenred stores dates as Excel serials with a time fraction, which is dropped."""
    assert excel_serial_to_date(serial) == expected


def test_quantize_money_strips_float_noise():
    """Edenred writes -4.4000000000000004 for what is really -4.40."""
    assert quantize_money(-4.4000000000000004) == Decimal("-4.40")
    assert quantize_money(250) == Decimal("250.00")


def test_resolve_account_normalizes_the_key():
    accounts = {"BE12345678901234": "Assets:Belfius:Checking"}

    assert resolve_account(accounts, "BE12 3456 7890 1234") == "Assets:Belfius:Checking"


def test_resolve_account_error_names_the_identifier():
    """The message must be paste-ready — it is the main way own_accounts gets populated."""
    with pytest.raises(UnknownAccountError) as excinfo:
        resolve_account({}, "BE99 0000 0000 0000", source="statement.csv")

    message = str(excinfo.value)
    assert "BE99 0000 0000 0000" in message
    assert "BE99000000000000" in message  # the normalized key, ready to paste
    assert "statement.csv" in message
    assert "own_accounts" in message
