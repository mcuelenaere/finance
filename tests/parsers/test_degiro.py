from decimal import Decimal
from io import StringIO

from finance.models import Balance, Transaction
from finance.parsers import degiro

ACCOUNTS = {"DEGIRO": "Assets:Investments:Degiro:Cash"}
CASH = "Assets:Investments:Degiro:Cash"
FLATEX = "Assets:Investments:Degiro:Flatex"

HEADER = "Datum,Tijd,Valutadatum,Product,ISIN,Omschrijving,FX,Mutatie,,Saldo,,Order Id"

# The export is newest-first, so rows appear in reverse chronological order.
FEE = (
    "04-01-2021,15:31,04-01-2021,ASML HOLDING N.V. - AD,USN070592100,"
    'DEGIRO transactiekosten,,EUR,"-0,01",EUR,"20,07",xxx-xxx-xxx-xxx-xxxx'
)
SWEEP_TO = (
    "07-08-2026,17:34,07-08-2026,,,"
    '"Overboeking naar uw geldrekening bij flatexDEGIRO Bank SE: 1.345,39 EUR",,,,EUR,'
    '"29543,06",'
)
SWEEP_FROM = (
    "10-08-2026,03:30,10-08-2026,,,"
    '"Overboeking van uw geldrekening bij flatexDEGIRO Bank SE: 0,01 EUR",,,,EUR,'
    '"29543,05",'
)
SWEEP_NO_CENTS = (
    "21-10-2024,03:30,21-10-2024,,,"
    '"Overboeking naar uw geldrekening bij flatexDEGIRO Bank SE: 8.305 EUR",,,,EUR,'
    '"12345,00",'
)


def parse(*rows):
    return tuple(degiro.parse_from_io(StringIO("\n".join([HEADER, *rows])), ACCOUNTS))


def transactions(directives):
    return [d for d in directives if isinstance(d, Transaction)]


def test_fee_row():
    (txn,) = transactions(parse(FEE))

    assert txn.date.isoformat() == "2021-01-04"
    assert txn.payee == "DEGIRO"
    assert txn.narration == "ASML HOLDING N.V. - AD (USN070592100): DEGIRO transactiekosten"
    assert txn.meta["isin"] == "USN070592100"
    assert txn.meta["order_id"] == "xxx-xxx-xxx-xxx-xxxx"

    own, unresolved = txn.postings
    assert own.account == CASH
    assert own.units.number == Decimal("-0.01")
    assert unresolved.account is None  # left for the rules engine


def test_sweep_rows_carry_their_amount_in_the_description():
    """`Mutatie` is empty on sweep rows; skipping them is what broke the balance column."""
    # Fixtures are listed newest-first, as the real export is.
    to_flatex, from_flatex = transactions(parse(SWEEP_FROM, SWEEP_TO))

    # Reversed to chronological order, so the earlier date comes first.
    assert to_flatex.date.isoformat() == "2026-08-07"
    assert to_flatex.postings[0].units.number == Decimal("1345.39")
    assert from_flatex.postings[0].units.number == Decimal("-0.01")


def test_sweeps_are_booked_against_the_flatex_account():
    """Both sides stay inside Assets:Investments:Degiro:*, so a sweep cannot alter net worth."""
    (txn,) = transactions(parse(SWEEP_TO))

    assert [p.account for p in txn.postings] == [CASH, FLATEX]
    # The parser fills this in itself rather than leaving it to a rule that could misroute it.
    assert txn.postings[1].units is None


def test_sweep_amount_without_cents_is_thousands_not_decimals():
    """`8.305 EUR` means 8305, not 8.305 — the one row in this export that omits cents."""
    (txn,) = transactions(parse(SWEEP_NO_CENTS))

    assert txn.postings[0].units.number == Decimal("8305")


def test_balance_assertion_comes_from_the_newest_row():
    # Newest first: 2026-08-10, then 2026-08-07, then 2021-01-04.
    directives = parse(SWEEP_FROM, SWEEP_TO, FEE)
    (balance,) = [d for d in directives if isinstance(d, Balance)]

    assert balance.account == CASH
    assert balance.amount.number == Decimal("29543.05")
    assert balance.amount.currency == "EUR"
    # Stated after that day's movements, so asserted the following day.
    assert balance.date.isoformat() == "2026-08-11"
    # DEGIRO rounds the balance to cents while recording some amounts to three decimals.
    assert balance.tolerance == Decimal("0.10")


def test_rows_that_move_no_cash_are_skipped():
    unparseable = '01-01-2021,10:00,01-01-2021,,,Some note with no amount,,,,EUR,"1,00",'
    assert transactions(parse(unparseable)) == []


def test_multi_currency_gets_one_assertion_each():
    usd = '05-01-2021,15:31,05-01-2021,APPLE INC.,US0378331005,Dividend,,USD,"1,50",USD,"3,00",'
    balances = [d for d in parse(usd, FEE) if isinstance(d, Balance)]

    assert {(b.amount.currency, b.amount.number) for b in balances} == {
        ("EUR", Decimal("20.07")),
        ("USD", Decimal("3.00")),
    }
