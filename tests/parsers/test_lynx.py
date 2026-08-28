"""Lynx flex reports.

Several CSVs stacked in one file, so the fixtures below keep the section structure rather than
just the transaction rows — getting the dispatch wrong is the most likely way to break this.
"""

from decimal import Decimal

import pytest

from finance.models import Balance, Open, Transaction
from finance.parsers import lynx
from finance.parsers.base import UnknownAccountError

ACCOUNTS = {
    "LYNX": "Assets:Cash:Lynx",
    "LYNX_POSITIONS": "Assets:Investments:Lynx:Positions",
}
CASH = "Assets:Cash:Lynx"
POSITIONS = "Assets:Investments:Lynx:Positions"

HEADER = [
    "Statement,Header,Field Name,Field Value",
    'Statement,Data,Period,"August 25, 2025 - August 25, 2026"',
    "Summary,Header,Field Name,Field Value",
    "Summary,Data,Base Currency,USD",
    "Summary,Data,Starting Cash,0.0",
    "Transaction History,Header,Date,Account,Description,Transaction Type,Symbol,Quantity,"
    "Price,Price Currency,Gross Amount ,Commission,Net Amount",
]

# A purchase: gross is quantity x price, and the net is short by more than the commission.
BUY = (
    "Transaction History,Data,2026-06-05,U1234567,SS SPDR S&P 500 UCIT ETF ACC,Buy,SPYL,"
    "54.0,18.55,USD,-1001.7,-9.0,-1011.90204"
)
DEPOSIT = (
    "Transaction History,Data,2026-06-01,U1234567,Electronic Fund Transfer,Deposit,"
    "-,-,-,-,16980.0,-,16980.0"
)
INTEREST = (
    "Transaction History,Data,2026-07-06,U1234567,USD Credit Interest for Jun-2026,"
    "Credit Interest,-,-,-,-,0.52,-,0.52"
)


def write(tmp_path, *rows, ending_cash=None):
    lines = list(HEADER)
    if ending_cash is not None:
        lines.insert(5, f"Summary,Data,Ending Cash,{ending_cash}")
    lines.extend(rows)
    path = tmp_path / "flex.csv"
    path.write_text("\n".join(lines), encoding="utf-8")
    return str(path)


def parse(tmp_path, *rows, **kwargs):
    return list(lynx.parse_file(write(tmp_path, *rows, **kwargs), ACCOUNTS))


def transactions(directives):
    return [d for d in directives if isinstance(d, Transaction)]


def test_a_purchase_books_a_lot_commission_and_the_residual_tax(tmp_path):
    """`Net` is not `Gross + Commission`: the remainder is the Belgian transaction tax.

    It has no column of its own, so folding it into the cost would both overstate the basis
    and hide a real expense. Deriving it as the residual also guarantees the postings balance,
    whatever the rate happens to be.
    """
    (txn,) = transactions(parse(tmp_path, BUY))

    position, cash, commission, tax = txn.postings
    assert position.account == POSITIONS
    assert position.units.number == Decimal("54")
    assert position.units.currency == "SPYL"
    assert position.cost.number == Decimal("1001.7")
    assert position.cost.total is True

    assert cash.units.number == Decimal("-1011.90204")
    assert commission.units.number == Decimal("9")
    assert tax.units.number == Decimal("1.20204")
    # 0.12% of gross, though the parser never assumes that rate.
    assert tax.units.number == (Decimal("1001.7") * Decimal("0.0012")).quantize(Decimal("0.00001"))


def test_the_postings_balance_exactly(tmp_path):
    (txn,) = transactions(parse(tmp_path, BUY))
    total = sum(
        (p.units.number for p in txn.postings if p.units and p.units.currency == "USD"),
        Decimal(0),
    )
    # Cost is a separate commodity, so the USD legs alone must cancel against it.
    assert total == Decimal("-1001.7")


def test_float_damage_in_the_source_is_quantized_away(tmp_path):
    """The export writes -869.5862599999999 for what is plainly -869.58626."""
    row = (
        "Transaction History,Data,2026-06-05,U1234567,ISHARES CORE EM IMI ACC,Buy,EIMI,"
        "16.0,53.9717,USD,-863.55,-5.0,-869.5862599999999"
    )
    (txn,) = transactions(parse(tmp_path, row))

    assert txn.postings[1].units.number == Decimal("-869.58626")


def test_whole_numbers_keep_no_exponent(tmp_path):
    """normalize() turns 1000 into 1E+3, which has no place in a ledger."""
    (txn,) = transactions(parse(tmp_path, DEPOSIT))

    assert str(txn.postings[0].units.number) == "16980"


def test_a_deposit_is_cash_with_the_counter_left_to_rules(tmp_path):
    (txn,) = transactions(parse(tmp_path, DEPOSIT))

    assert len(txn.postings) == 2
    assert txn.postings[0].account == CASH
    assert txn.postings[1].account is None


def test_interest_is_left_to_rules(tmp_path):
    (txn,) = transactions(parse(tmp_path, INTEREST))

    assert txn.postings[0].units.number == Decimal("0.52")
    assert txn.postings[1].account is None


def test_a_lowercase_exchange_suffix_is_not_a_valid_commodity(tmp_path):
    """`MEUSl` appears in the real export and would be rejected by Beancount as written."""
    row = (
        "Transaction History,Data,2025-09-22,U1234567,AM CR STX EUROPE 600 ETF ACC,Buy,MEUSl,"
        "6.0,312.9063,USD,-1877.44,-5.0,-1884.692928"
    )
    (txn,) = transactions(parse(tmp_path, row))

    assert txn.postings[0].units.currency == "MEUSL"


def test_a_sale_is_left_as_cash(tmp_path):
    """No disposal exists in this export, so its shape cannot be checked against anything."""
    row = (
        "Transaction History,Data,2026-06-05,U1234567,SS SPDR S&P 500,Sell,SPYL,"
        "-54.0,18.55,USD,1001.7,-9.0,991.5"
    )
    (txn,) = transactions(parse(tmp_path, row))

    assert len(txn.postings) == 2
    assert txn.postings[1].account is None


def test_a_trade_priced_in_another_currency_stays_cash(tmp_path):
    row = (
        "Transaction History,Data,2026-06-05,U1234567,SOMETHING,Buy,ABC,"
        "10.0,5.0,EUR,-50.0,-1.0,-51.0"
    )
    (txn,) = transactions(parse(tmp_path, row))

    assert len(txn.postings) == 2
    assert txn.postings[1].account is None


def test_positions_open_with_fifo_only_when_something_was_booked(tmp_path):
    assert [d for d in parse(tmp_path, BUY) if isinstance(d, Open)][0].booking == "FIFO"
    assert [d for d in parse(tmp_path, DEPOSIT) if isinstance(d, Open)] == []


def test_ending_cash_becomes_a_balance_assertion(tmp_path):
    """The Summary states closing cash independently of the rows, so it is a real check."""
    directives = parse(tmp_path, BUY, DEPOSIT, ending_cash="15968.09796")
    (balance,) = [d for d in directives if isinstance(d, Balance)]

    assert balance.account == CASH
    assert balance.amount == (Decimal("15968.09796"), "USD")
    # Stated at the close of the last row's day, so asserted the day after.
    assert balance.date.isoformat() == "2026-06-06"


def test_no_ending_cash_means_no_assertion(tmp_path):
    assert [d for d in parse(tmp_path, BUY) if isinstance(d, Balance)] == []


def test_statement_and_summary_rows_are_not_transactions(tmp_path):
    assert len(transactions(parse(tmp_path, BUY))) == 1


def test_missing_account_is_an_error_not_a_guess(tmp_path):
    with pytest.raises(UnknownAccountError, match="lynx"):
        list(lynx.parse_file(write(tmp_path, BUY), {}))


def test_positions_fall_back_to_a_sibling_of_the_cash_account(tmp_path):
    """The fallback for a ledger that keeps one branch per broker.

    It is only right when cash and holdings live together, which is why a ledger that files them
    under different roots has to name the positions account — see `POSITIONS_ACCOUNT_KEY`. Kept
    tested because it is the documented default and nothing else exercises it any more.
    """
    one_branch = {"LYNX": "Assets:Investments:Lynx:Cash"}
    (txn,) = [
        d for d in lynx.parse_file(write(tmp_path, BUY), one_branch) if isinstance(d, Transaction)
    ]

    assert txn.postings[0].account == "Assets:Investments:Lynx:Positions"


def test_a_named_positions_account_wins_over_the_sibling(tmp_path):
    elsewhere = {
        "LYNX": "Assets:Cash:Lynx",
        "LYNX_POSITIONS": "Assets:Somewhere:Else",
    }
    (txn,) = [
        d for d in lynx.parse_file(write(tmp_path, BUY), elsewhere) if isinstance(d, Transaction)
    ]

    assert txn.postings[0].account == "Assets:Somewhere:Else"
