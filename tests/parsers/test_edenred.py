"""Edenred voucher exports.

Fixtures are written as real .xlsx files so the openpyxl path is exercised rather than mocked
— the cell types it hands back (datetime vs serial, float vs str) are the whole reason this
parser looks the way it does.
"""

from datetime import datetime
from decimal import Decimal

import pytest
from openpyxl import Workbook

from finance.models import FLAG_OKAY, FLAG_PENDING
from finance.parsers import edenred
from finance.parsers.base import UnknownAccountError

ACCOUNTS = {
    "EDENRED_MEAL": "Assets:Edenred:Meal",
    "EDENRED_ECO": "Assets:Edenred:Eco",
    "EDENRED_SPORT": "Assets:Edenred:Sport",
}

HEADER = [
    "TransactionDate",
    "TransactionAmount",
    "Product",
    "ECreditStatus",
    "MovementType",
    "Scheme",
    "Outlet",
    "ReasonTxt",
    "OrderNumberEdps",
]


def sheet(tmp_path, name, *rows):
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.append(HEADER)
    for row in rows:
        worksheet.append(list(row))
    path = tmp_path / name
    workbook.save(path)
    return str(path)


def spend(date, amount, outlet, product=None, status="Cleared"):
    return [date, amount, product, status, "Transaction at outlet", "CTAP_BELGIUM", outlet, "", ""]


def funding(date, amount, product):
    return [date, amount, product, "Cleared", "eCredit Funding", None, None, None, None]


def test_outlet_row(tmp_path):
    path = sheet(
        tmp_path,
        "TRE.xlsx",
        spend(datetime(2021, 8, 23, 12, 52, 13), -4.4, "SLAGERIJ AULA"),
    )
    (txn,) = list(edenred.parse_file(path, ACCOUNTS))

    assert txn.date.isoformat() == "2021-08-23"
    assert txn.payee == "SLAGERIJ AULA"
    assert txn.narration == "Transaction at outlet"
    assert txn.flag == FLAG_OKAY
    assert txn.meta["voucher"] == "meal"
    assert txn.meta["scheme"] == "CTAP_BELGIUM"

    own, unresolved = txn.postings
    assert own.account == "Assets:Edenred:Meal"
    assert own.units.number == Decimal("-4.40")
    assert unresolved.account is None


def test_float_artifacts_are_quantized_to_cents(tmp_path):
    """One sheet stores -4.4000000000000004 for what is plainly -4.40."""
    path = sheet(
        tmp_path,
        "TRE.xlsx",
        spend(datetime(2021, 8, 23), -4.4000000000000004, "SLAGERIJ AULA"),
    )
    (txn,) = list(edenred.parse_file(path, ACCOUNTS))

    assert txn.postings[0].units.number == Decimal("-4.40")
    assert str(txn.postings[0].units.number) == "-4.40"


def test_voucher_type_falls_back_to_the_filename(tmp_path):
    """`Product` is populated on some rows of a sheet and blank on others."""
    path = sheet(
        tmp_path,
        "TRE.xlsx",
        spend(datetime(2021, 8, 23), -4.4, "SLAGERIJ AULA", product=None),
        spend(
            datetime(2021, 8, 24), -5.0, "SLAGERIJ AULA", product="Ticket Restaurant Electronique"
        ),
    )
    blank, populated = list(edenred.parse_file(path, ACCOUNTS))

    assert blank.meta["voucher"] == populated.meta["voucher"] == "meal"
    assert blank.postings[0].account == populated.postings[0].account


@pytest.mark.parametrize(
    "name,product,expected",
    [
        ("ECE.xlsx", "Ticket EcoCheque", "Assets:Edenred:Eco"),
        ("SCE.xlsx", "Ticket Sport&Culture", "Assets:Edenred:Sport"),
        ("TRE.xlsx", "Ticket Restaurant Electronique", "Assets:Edenred:Meal"),
    ],
)
def test_each_voucher_type_gets_its_own_account(tmp_path, name, product, expected):
    """An eco-cheque cannot buy lunch, so the balances must not be pooled."""
    path = sheet(tmp_path, name, spend(datetime(2023, 4, 20), -7.5, "SHOP", product=product))
    (txn,) = list(edenred.parse_file(path, ACCOUNTS))

    assert txn.postings[0].account == expected


def test_funding_rows_are_positive_and_have_no_outlet(tmp_path):
    path = sheet(
        tmp_path, "TRE.xlsx", funding(datetime(2021, 10, 22), 320, "Ticket Restaurant Electronique")
    )
    (txn,) = list(edenred.parse_file(path, ACCOUNTS))

    assert txn.payee is None
    assert txn.narration == "eCredit Funding"
    assert txn.postings[0].units.number == Decimal("320.00")


def test_unsettled_rows_are_flagged_pending(tmp_path):
    """`Active` means the outlet has not settled yet — Beancount's `!` says the same."""
    path = sheet(
        tmp_path,
        "TRE.xlsx",
        spend(datetime(2026, 8, 22), -48.24, "DELHAIZE", status="Active"),
        spend(datetime(2026, 8, 23), -10.00, "DELHAIZE", status="Cleared"),
    )
    pending, settled = list(edenred.parse_file(path, ACCOUNTS))

    assert pending.flag == FLAG_PENDING
    assert settled.flag == FLAG_OKAY


def test_blank_rows_are_skipped(tmp_path):
    path = sheet(
        tmp_path,
        "TRE.xlsx",
        spend(datetime(2021, 8, 23), -4.4, "SLAGERIJ AULA"),
        [None] * len(HEADER),
    )
    assert len(list(edenred.parse_file(path, ACCOUNTS))) == 1


def test_unrecognized_filename_is_an_error(tmp_path):
    path = sheet(tmp_path, "MYSTERY.xlsx", spend(datetime(2021, 8, 23), -4.4, "SHOP"))
    with pytest.raises(ValueError, match="cannot tell which Edenred voucher"):
        list(edenred.parse_file(path, ACCOUNTS))


def test_missing_account_is_an_error_not_a_guess(tmp_path):
    path = sheet(tmp_path, "ECE.xlsx", spend(datetime(2023, 4, 20), -7.5, "SHOP"))
    with pytest.raises(UnknownAccountError, match="edenred_eco"):
        list(edenred.parse_file(path, {"EDENRED_MEAL": "Assets:Edenred:Meal"}))


def test_missing_columns_are_an_error(tmp_path):
    workbook = Workbook()
    workbook.active.append(["TransactionDate", "TransactionAmount"])
    path = tmp_path / "TRE.xlsx"
    workbook.save(path)

    with pytest.raises(ValueError, match="missing expected columns"):
        list(edenred.parse_file(str(path), ACCOUNTS))
