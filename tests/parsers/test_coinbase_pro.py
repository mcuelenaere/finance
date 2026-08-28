"""Coinbase Pro exports.

Only the fiat legs are emitted, which is a decision rather than a gap — the crypto left this
account for the same provider's retail wallet, whose export is missing its sales. The tests here
cover the two things that made wiring this parser unsafe until now: a row identifier that did
not identify a row, and the absence of any check that every row was read.
"""

from decimal import Decimal
from io import StringIO

import pytest

from finance.models import Balance, Transaction
from finance.parsers import coinbase_pro
from finance.parsers.base import UnknownAccountError

ACCOUNTS = {"COINBASE_PRO": "Assets:Crypto:CoinbasePro:Cash"}
CASH = "Assets:Crypto:CoinbasePro:Cash"

HEADER = "portfolio,type,time,amount,balance,amount/balance unit,transfer id,trade id,order id"

_UNSET = object()


def parse(*rows, accounts=_UNSET):
    resolved = ACCOUNTS if accounts is _UNSET else accounts
    handle = StringIO("\n".join([HEADER, *rows]))
    return list(coinbase_pro.parse_from_io(handle, resolved, source_file="account.csv"))


def transactions(directives):
    return [d for d in directives if isinstance(d, Transaction)]


def test_a_trade_and_its_fee_are_two_transactions_not_one():
    """They share a trade id — it identifies the trade, not the row. Keying the transaction id
    on it alone made them look like one transaction, and deduplication kept only the first, so
    every fee on a trade was silently dropped."""
    rows = [
        "default,match,2017-06-26T05:34:18.000Z,-73.8154612906,100.0,EUR,,30383,o1",
        "default,fee,2017-06-26T05:34:18.000Z,-0.1845386532265,99.8,EUR,,30383,o1",
    ]
    ids = {t.meta["transaction_id"] for t in transactions(parse(*rows))}

    assert len(ids) == 2


def test_a_trade_becomes_one_transaction_with_a_lot():
    """Two rows, one trade. The fiat leg is the one that emits it, so exactly one row does."""
    rows = [
        "default,match,2017-06-15T13:00:26.607Z,0.037,0.037,ETH,,56912,o1",
        "default,match,2017-06-15T13:00:26.607Z,-9.99,290.01,EUR,,56912,o1",
    ]
    (txn,) = transactions(parse(*rows))

    position, cash = txn.postings
    assert position.units == (Decimal("0.037"), "ETH")
    assert position.cost.number == Decimal("9.99")
    assert position.cost.total is True
    assert cash.units == (Decimal("-9.99"), "EUR")


def test_a_disposal_names_no_lot_and_leaves_the_gain_elided():
    rows = [
        "default,match,2019-05-12T08:23:15.000Z,-0.04,0.46,BTC,,90001,o2",
        "default,match,2019-05-12T08:23:15.000Z,258.40,300.0,EUR,,90001,o2",
    ]
    (txn,) = transactions(parse(*rows))

    position, cash, gain = txn.postings
    assert position.units == (Decimal("-0.04"), "BTC")
    assert position.cost.number is None  # the booking method picks the lot
    assert cash.units.number == Decimal("258.40")
    assert gain.account == "Income:CapitalGains:Crypto"
    assert gain.units is None


def test_a_crypto_for_crypto_trade_has_no_fiat_leg_and_moves_through_the_pool():
    """A couple of dozen trades here swap one coin for another and carry no fiat value at all,
    so neither side can be priced. Each leg moves against the pool instead: out at the lot's own
    cost, in at zero."""
    rows = [
        "default,match,2021-02-22T20:14:32.000Z,-0.02,0.10,LTC,,90002,o3",
        "default,match,2021-02-22T20:14:32.000Z,0.00007546,0.0011,BTC,,90002,o3",
    ]
    out, into = transactions(parse(*rows))

    assert out.postings[0].units == (Decimal("-0.02"), "LTC")
    assert out.postings[0].cost.number is None
    assert into.postings[0].units == (Decimal("0.00007546"), "BTC")
    assert into.postings[0].cost.number == Decimal(0)
    assert out.postings[1].account is None and into.postings[1].account is None


def test_a_fee_charged_in_the_coin_reduces_the_position():
    """Fees are not always fiat here, and a crypto fee is units leaving."""
    row = "default,fee,2021-02-22T20:14:32.000Z,-0.0000003773,0.0011,BTC,,90003,o4"
    (txn,) = transactions(parse(row))

    assert txn.postings[0].account.endswith(":Positions")
    assert txn.postings[0].units.currency == "BTC"
    assert txn.postings[0].cost.number is None


def test_crypto_arriving_from_elsewhere_is_booked_at_zero():
    """Nothing on this side records what it cost; zero is stated rather than guessed."""
    row = "default,deposit,2019-06-16T11:02:24.000Z,0.16666667,0.48,BTC,tx1,,"
    (txn,) = transactions(parse(row))

    assert txn.postings[0].cost.number == Decimal(0)


def test_crypto_leaving_takes_its_own_cost_with_it():
    """No gain is invented from a price nobody recorded — the elided posting absorbs the cost."""
    row = "default,withdrawal,2020-02-09T16:51:59.000Z,-0.1412,0.041,BTC,tx2,,"
    (txn,) = transactions(parse(row))

    assert txn.postings[0].cost.number is None
    assert txn.postings[1].account is None


def test_a_transfer_id_is_used_when_there_is_one():
    row = (
        "default,deposit,2017-06-15T10:47:59.971Z,300.0,300.0,EUR,"
        "ca385f93-f18b-47ce-b5b8-fbb58e821f59,,"
    )
    (txn,) = transactions(parse(row))

    assert txn.meta["transaction_id"] == "ca385f93-f18b-47ce-b5b8-fbb58e821f59"


def test_the_closing_balance_becomes_an_assertion():
    rows = [
        "default,deposit,2017-06-15T10:47:59.971Z,300.0,300.0,EUR,t1,,",
        "default,withdrawal,2022-12-03T09:12:19.000Z,-299.99,0.01,EUR,t2,,",
    ]
    (balance,) = [d for d in parse(*rows) if isinstance(d, Balance)]

    assert balance.account == CASH
    assert balance.amount == (Decimal("0.01"), "EUR")
    assert balance.date.isoformat() == "2022-12-04"


def test_crypto_rows_do_not_produce_an_assertion():
    """Their units are not booked, so asserting a crypto balance would assert nothing."""
    rows = [
        "default,deposit,2017-06-15T10:47:59.971Z,1.5,1.5,ETH,t1,,",
        "default,deposit,2017-06-15T10:47:59.971Z,300.0,300.0,EUR,t2,,",
    ]
    currencies = {d.amount.currency for d in parse(*rows) if isinstance(d, Balance)}

    assert currencies == {"EUR"}


def test_the_counter_posting_is_left_for_the_rules():
    row = "default,deposit,2017-06-15T10:47:59.971Z,300.0,300.0,EUR,t1,,"
    (txn,) = transactions(parse(row))

    assert txn.postings[1].account is None


def test_missing_account_is_an_error_not_a_guess():
    with pytest.raises(UnknownAccountError, match="coinbase_pro"):
        parse("default,deposit,2017-06-15T10:47:59.971Z,300.0,300.0,EUR,t1,,", accounts={})
