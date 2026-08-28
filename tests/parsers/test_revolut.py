"""Revolut account statements.

The statement carries its own running balance, which is what makes the fee convention checkable
rather than guessable — and it is the thing a parser here can most easily get wrong.
"""

from decimal import Decimal
from io import StringIO

import pytest

from finance.models import Balance, Transaction
from finance.parsers import revolut
from finance.parsers.base import UnknownAccountError

ACCOUNTS = {"REVOLUT": "Assets:Revolut:Cash"}
CASH = "Assets:Revolut:Cash"
FEES = "Expenses:Financial:Fees"

HEADER = "Type,Product,Started Date,Completed Date,Description,Amount,Fee,Currency,State,Balance"


#: An empty mapping is falsy, so `accounts or ACCOUNTS` would quietly substitute the real
#: accounts and the "no account declared" test would pass for the wrong reason.
_UNSET = object()


def parse(*rows, accounts=_UNSET):
    handle = StringIO("\n".join([HEADER, *rows]))
    resolved = ACCOUNTS if accounts is _UNSET else accounts
    return list(revolut.parse_from_io(handle, resolved, source_file="revolut.csv"))


def transactions(directives):
    return [d for d in directives if isinstance(d, Transaction)]


def test_the_fee_is_on_top_of_the_amount_not_inside_it():
    """A 1000 transfer with a 3.51 fee moves 1003.51. The statement's own balance says so:
    49813.13 = 50816.64 - 1000 - 3.51, and does not reconcile without the fee."""
    (txn,) = transactions(
        parse(
            "Transfer,Current,2025-09-09 18:00:52,2025-09-09 18:29:49,"
            "SWIFT Transfer to Interactive Brokers Ireland Ltd.,-1000,3.51,USD,COMPLETED,49813.13"
        )
    )

    cash, fee, unresolved = txn.postings
    assert cash.account == CASH
    assert cash.units.number == Decimal("-1003.51")
    assert fee.account == FEES
    assert fee.units.number == Decimal("3.51")
    assert unresolved.account is None


def test_a_row_without_a_fee_has_no_fee_posting():
    (txn,) = transactions(
        parse(
            "Topup,Current,2025-09-02 10:46:55,2025-09-02 17:24:30,"
            "Payment from OPUS LABS NV,50816.64,0,USD,COMPLETED,50816.64"
        )
    )

    assert len(txn.postings) == 2
    assert txn.postings[0].units.number == Decimal("50816.64")


def test_only_settled_rows_book():
    """A pending or reverted row has not moved money, and booking it would invent some."""
    rows = [
        "Transfer,Current,2025-09-09 18:00:52,2025-09-09 18:29:49,A,-10,0,USD,COMPLETED,90",
        "Transfer,Current,2025-09-10 18:00:52,2025-09-10 18:29:49,B,-20,0,USD,PENDING,70",
        "Transfer,Current,2025-09-11 18:00:52,2025-09-11 18:29:49,C,-30,0,USD,REVERTED,40",
    ]
    assert len(transactions(parse(*rows))) == 1


def test_the_date_is_when_it_completed_not_when_it_started():
    """This transfer took two days to settle, and the balance moved on the later date."""
    (txn,) = transactions(
        parse(
            "Transfer,Current,2025-09-20 8:46:47,2025-09-22 1:34:51,"
            "SWIFT Transfer to Interactive Brokers Limited,-1200,1.8,USD,COMPLETED,37.06"
        )
    )

    assert txn.date.isoformat() == "2025-09-22"


def test_the_stated_closing_balance_becomes_an_assertion():
    rows = [
        "Topup,Current,2025-09-02 10:46:55,2025-09-02 17:24:30,X,50816.64,0,USD,COMPLETED,50816.64",
        "Transfer,Current,2025-09-09 18:00:52,2025-09-09 18:29:49,Y,-1000,3.51,USD,"
        "COMPLETED,49813.13",
    ]
    (balance,) = [d for d in parse(*rows) if isinstance(d, Balance)]

    assert balance.account == CASH
    assert balance.amount == (Decimal("49813.13"), "USD")
    assert balance.date.isoformat() == "2025-09-10"


def test_each_currency_pocket_is_asserted_separately():
    rows = [
        "Topup,Current,2025-09-02 10:00:00,2025-09-02 10:00:00,X,100,0,USD,COMPLETED,100",
        "Topup,Current,2025-09-03 10:00:00,2025-09-03 10:00:00,Y,25,0,EUR,COMPLETED,25",
    ]
    balances = {d.amount.currency: d.amount.number for d in parse(*rows) if isinstance(d, Balance)}

    assert balances == {"USD": Decimal("100"), "EUR": Decimal("25")}


def test_an_unpadded_hour_still_parses():
    """Revolut writes `8:24:07`, not `08:24:07`."""
    (txn,) = transactions(
        parse(
            "Transfer,Current,2025-09-07 8:24:07,2025-09-07 8:24:07,Z,-100,0,USD,COMPLETED,50716.64"
        )
    )

    assert txn.date.isoformat() == "2025-09-07"


def test_missing_account_is_an_error_not_a_guess():
    with pytest.raises(UnknownAccountError, match="revolut"):
        parse(
            "Topup,Current,2025-09-02 10:00:00,2025-09-02 10:00:00,X,5,0,EUR,COMPLETED,5",
            accounts={},
        )
