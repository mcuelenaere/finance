"""Coinbase consumer account exports.

A different product and a different file from the Coinbase Pro / GDAX export, which
``test_coinbase_pro`` covers. The tests here concentrate on what this format does unusually: it
writes the sign outside the currency symbol, it hides a sale's euro proceeds in a column rather
than a row, and it does not always timestamp a coin's arrival before its departure.
"""

from decimal import Decimal
from io import StringIO

import pytest

from finance.models import Open, Transaction
from finance.parsers import coinbase
from finance.parsers.base import UnknownAccountError

ACCOUNTS = {"COINBASE": "Assets:Investments:Crypto:Coinbase:Cash"}
CASH = "Assets:Investments:Crypto:Coinbase:Cash"
POSITIONS = "Assets:Investments:Crypto:Coinbase:Positions"

PREAMBLE = [
    "",
    "Transactions",
    "User,Some One,120b411f",
    "ID,Timestamp,Transaction Type,Asset,Quantity Transacted,Price Currency,"
    "Price at Transaction,Subtotal,Total (inclusive of fees and/or spread),"
    "Fees and/or Spread,Notes,Sender Address,Recipient Address",
]

_UNSET = object()


def row(rid, stamp, kind, asset, qty, *, subtotal="", total="", fee="€0.00", notes=""):
    return f"{rid},{stamp} UTC,{kind},{asset},{qty},EUR,€1.00,{subtotal},{total},{fee},{notes},,"


def parse(*rows, accounts=_UNSET):
    resolved = ACCOUNTS if accounts is _UNSET else accounts
    handle = StringIO("\n".join([*PREAMBLE, *rows]))
    return list(coinbase.parse_from_io(handle, resolved, source_file="coinbase.csv"))


def transactions(directives):
    return [d for d in directives if isinstance(d, Transaction)]


def test_the_sign_sits_outside_the_currency_symbol():
    """`-€3879.00192`. Stripping non-digits before reading it turns every debit into a credit."""
    assert coinbase.money("-€3879.00192") == Decimal("-3879.00192")
    assert coinbase.money("€5.00") == Decimal("5.00")
    assert coinbase.money("0.008530255486584") == Decimal("0.008530255486584")
    assert coinbase.money("") == Decimal(0)
    assert coinbase.money("€0.00") == Decimal(0)


def test_the_preamble_is_skipped():
    (txn,) = transactions(
        parse(row("a1", "2017-06-15 10:00:00", "Deposit", "EUR", "300", subtotal="€300.00"))
    )

    assert txn.postings[0].account == CASH
    assert txn.postings[0].units == (Decimal("300"), "EUR")


def test_a_sale_takes_its_proceeds_from_the_total_column():
    """The euro leg of a sale is not a row of its own — it is a column on the crypto row, net of
    the fee and the spread. Reading the quantity column instead would book euro as ether."""
    (txn,) = transactions(
        parse(
            row(
                "a2",
                "2025-08-15 14:00:31",
                "Advanced Trade Sell",
                "ETH",
                "-1.00164796",
                subtotal="-€3879.00192",
                total="-€3855.72791",
                fee="€23.2740115371312",
            )
        )
    )

    position, cash, gain = txn.postings
    assert position.units == (Decimal("-1.00164796"), "ETH")
    assert position.cost.number is None  # the booking method picks the lot
    assert cash.units == (Decimal("3855.72791"), "EUR")
    assert gain.account == "Income:CapitalGains:Crypto"
    assert gain.units is None


def test_a_coin_arriving_is_valued_at_what_the_export_says_it_was_worth():
    (txn,) = transactions(
        parse(row("a3", "2017-09-12 07:45:14", "Receive", "ETH", "1", subtotal="€3904.55366"))
    )

    assert txn.postings[0].units == (Decimal("1"), "ETH")
    assert txn.postings[0].cost.number == Decimal("3904.55366")
    assert txn.postings[0].cost.total is True


def test_a_coin_leaving_takes_its_own_cost_with_it():
    """No gain is invented from a price nobody recorded."""
    (txn,) = transactions(
        parse(row("a4", "2017-10-12 09:00:00", "Send", "BTC", "-0.1", subtotal="-€400.00"))
    )

    assert txn.postings[0].cost.number is None
    assert txn.postings[1].account is None


def test_arrivals_are_ordered_before_departures_within_a_day():
    """The export stamps at least one send a second BEFORE the coin it sends arrives. Rendered
    that way there is no lot to reduce."""
    rows = [
        row("b1", "2017-10-12 09:00:01", "Send", "BTC", "-0.1", subtotal="-€400.00"),
        row("b2", "2017-10-12 09:00:02", "Exchange Withdrawal", "BTC", "0.1", subtotal="€400.00"),
    ]
    first, second = transactions(parse(*rows))

    assert first.narration == "Exchange Withdrawal BTC"
    assert second.narration == "Send BTC"
    assert int(first.meta["sequence"]) < int(second.meta["sequence"])


def test_a_crypto_row_names_its_asset_in_the_narration():
    """`Exchange Withdrawal` is one phrase for two transactions: euro arriving from the exchange,
    or coins arriving from it. One pairs with a bank leg, the other with a wallet."""
    rows = [
        row("c1", "2020-01-24 10:00:00", "Pro Withdrawal", "EUR", "100", subtotal="€100.00"),
        row("c2", "2020-01-24 10:00:00", "Pro Withdrawal", "BTC", "0.01", subtotal="€80.00"),
    ]
    fiat, crypto = transactions(parse(*rows))

    assert fiat.narration == "Pro Withdrawal"
    assert crypto.narration == "Pro Withdrawal BTC"


def test_the_export_id_is_kept_so_a_re_import_cannot_double_up():
    (txn,) = transactions(
        parse(row("689f3fb1", "2025-08-15 14:09:53", "Withdrawal", "EUR", "-5", subtotal="€5.00"))
    )

    assert txn.meta["transaction_id"] == "689f3fb1"


def test_positions_open_fifo_only_when_a_coin_was_handled():
    crypto = row("d1", "2017-09-12 07:45:14", "Receive", "ETH", "1", subtotal="€100.00")
    fiat = row("d2", "2017-06-15 10:00:00", "Deposit", "EUR", "300", subtotal="€300.00")

    (opened,) = [d for d in parse(crypto) if isinstance(d, Open)]
    assert opened.account == POSITIONS
    assert opened.booking == "FIFO"
    assert [d for d in parse(fiat) if isinstance(d, Open)] == []


def test_missing_account_is_an_error_not_a_guess():
    with pytest.raises(UnknownAccountError, match="coinbase"):
        parse(row("e1", "2017-06-15 10:00:00", "Deposit", "EUR", "300"), accounts={})
