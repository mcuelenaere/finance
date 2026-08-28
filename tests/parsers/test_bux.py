"""BUX transaction history.

The export is friendly — one column for the cash effect, another for the resulting balance, and
structured categories — so the tests concentrate on the two things it does not say plainly: the
order its rows have to keep, and what happens when a holding changes ISIN.
"""

from decimal import Decimal
from io import StringIO

import pytest

from finance.models import Balance, Open, Transaction
from finance.parsers import bux
from finance.parsers.base import UnknownAccountError

ACCOUNTS = {
    "BUX": "Assets:Cash:BUX",
    "BUX_POSITIONS": "Assets:Investments:BUX:Positions",
}
CASH = "Assets:Cash:BUX"
POSITIONS = "Assets:Investments:BUX:Positions"
GAINS = "Income:CapitalGains:Broker"

HEADER = (
    "Transaction Time (CET),Transaction Category,Transaction Type,Asset Id,Asset Name,"
    "Asset Currency,Transaction Currency,Currency Pair,Exchange Rate,Transaction Amount,"
    "Trade Amount,Trade Price,Trade Quantity,Cash Balance Amount"
)

_UNSET = object()


def row(stamp, category, kind, *, isin="", name="", amount="0", price="", quantity="", balance=""):
    return (
        f"{stamp},{category},{kind},{isin},{name},EUR,EUR,EUREUR,1.0,{amount},,"
        f"{price},{quantity},{balance}"
    )


def parse(*rows, accounts=_UNSET):
    resolved = ACCOUNTS if accounts is _UNSET else accounts
    return list(
        bux.parse_from_io(StringIO("\n".join([HEADER, *rows])), resolved, source_file="b.csv")
    )


def transactions(directives):
    return [d for d in directives if isinstance(d, Transaction)]


def test_a_buy_books_a_lot_at_the_cash_that_left():
    (txn,) = transactions(
        parse(
            row(
                "2023-08-02 11:47:32.234000",
                "trades",
                "Buy Trade",
                isin="IE00BKM4GZ66",
                amount="-1312.93",
                price="28.542",
                quantity="46.000000",
                balance="368.4",
            )
        )
    )

    position, cash = txn.postings
    assert position.account == POSITIONS
    assert position.units == (Decimal("46.000000"), "IE00BKM4GZ66")
    assert position.cost.number == Decimal("1312.93")
    assert position.cost.total is True
    assert cash.units.number == Decimal("-1312.93")


def test_a_sell_names_no_lot_and_leaves_the_gain_elided():
    (txn,) = transactions(
        parse(
            row(
                "2023-08-02 11:48:31.681000",
                "trades",
                "Sell Trade",
                isin="IE00BKM4GZ66",
                amount="542.21",
                price="28.5376",
                quantity="19",
                balance="880.96",
            )
        )
    )

    position, cash, gain = txn.postings
    assert position.units == (Decimal("-19"), "IE00BKM4GZ66")
    assert position.cost.number is None  # the booking method picks the lot
    assert cash.units.number == Decimal("542.21")
    assert gain.account == GAINS
    assert gain.units is None


def test_the_source_order_survives_into_the_ledger():
    """A holding is sold the same day it is bought, so a disposal rendered before its
    acquisition would have no lot to reduce. The renderer sorts, so the order has to be
    carried as data rather than relied on implicitly."""
    rows = [
        row(
            "2023-08-02 11:47:32.234000",
            "trades",
            "Buy Trade",
            isin="X",
            amount="-100",
            price="10",
            quantity="10",
            balance="900",
        ),
        row(
            "2023-08-02 11:48:31.681000",
            "trades",
            "Sell Trade",
            isin="X",
            amount="60",
            price="12",
            quantity="5",
            balance="960",
        ),
    ]
    buy, sell = transactions(parse(*rows))

    assert int(buy.meta["sequence"]) < int(sell.meta["sequence"])


def test_the_structured_categories_are_mapped_not_left_to_rules():
    rows = [
        row("2023-08-02 11:47:32.234000", "fees", "Trading Fee", amount="-1.99", balance="1"),
        row("2023-08-02 11:47:32.235000", "tax", "Tob", amount="-1.6", balance="1"),
        row(
            "2023-10-06 08:13:04.582000",
            "interest",
            "Interest Payment",
            amount="90.04",
            balance="1",
        ),
        row("2023-09-27 10:54:08.286000", "dividends", "Cash Dividend", amount="0.11", balance="1"),
    ]
    accounts = [t.postings[1].account for t in transactions(parse(*rows))]

    assert accounts == [
        "Expenses:Financial:Fees",
        "Expenses:Taxes",
        "Income:Interest",
        "Income:Dividends:Broker",
    ]


def test_cash_movements_are_left_for_the_rules():
    """Their bank leg is exported too, so they must route through the clearing account rather
    than be decided here."""
    rows = [
        row(
            "2023-08-02 10:45:46.361000",
            "deposits",
            "Sepa Deposit",
            amount="1681.3",
            balance="1681.3",
        ),
        row(
            "2023-09-01 15:51:30.267000",
            "withdrawals",
            "Sepa Withdrawal",
            amount="-500.0",
            balance="1",
        ),
        row("2023-08-02 11:10:15.363000", "others", "Promotional", amount="4.87", balance="1"),
    ]
    for txn in transactions(parse(*rows)):
        assert txn.postings[1].account is None


def test_the_closing_balance_becomes_an_assertion():
    rows = [
        row(
            "2023-08-02 10:45:46.361000",
            "deposits",
            "Sepa Deposit",
            amount="1681.3",
            balance="1681.3",
        ),
        row(
            "2024-01-08 16:06:02.314000",
            "withdrawals",
            "Sepa Withdrawal",
            amount="-1681.3",
            balance="0.0",
        ),
    ]
    (balance,) = [d for d in parse(*rows) if isinstance(d, Balance)]

    assert balance.account == CASH
    assert balance.amount == (Decimal("0.0"), "EUR")
    assert balance.date.isoformat() == "2024-01-09"


def test_the_closing_balance_is_the_latest_row_not_the_last_line():
    """Rows sharing a timestamp are not in balance order, so the closing figure is taken by
    time rather than by position in the file."""
    rows = [
        row(
            "2024-01-08 16:06:02.314000",
            "withdrawals",
            "Sepa Withdrawal",
            amount="-10",
            balance="0.0",
        ),
        row("2023-08-02 10:45:46.361000", "deposits", "Sepa Deposit", amount="10", balance="10"),
    ]
    (balance,) = [d for d in parse(*rows) if isinstance(d, Balance)]

    assert balance.amount.number == Decimal("0.0")


def test_positions_open_fifo_only_when_something_was_traded():
    traded = row(
        "2023-08-02 11:47:32.234000",
        "trades",
        "Buy Trade",
        isin="X",
        amount="-100",
        price="10",
        quantity="10",
        balance="900",
    )
    cash_only = row(
        "2023-08-02 10:45:46.361000", "deposits", "Sepa Deposit", amount="10", balance="10"
    )

    (opened,) = [d for d in parse(traded) if isinstance(d, Open)]
    assert opened.booking == "FIFO"
    assert [d for d in parse(cash_only) if isinstance(d, Open)] == []


def test_missing_account_is_an_error_not_a_guess():
    with pytest.raises(UnknownAccountError, match="bux"):
        parse(
            row(
                "2023-08-02 10:45:46.361000", "deposits", "Sepa Deposit", amount="10", balance="10"
            ),
            accounts={},
        )
