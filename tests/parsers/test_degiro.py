from decimal import Decimal
from io import StringIO

from finance.models import Balance, Open, Transaction
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


# --- positions -------------------------------------------------------------------------

POSITIONS = "Assets:Investments:Degiro:Positions"


def trade_row(
    description, amount, saldo="1000,00", isin="IE00B4L5Y983", product="ISHARES", currency="EUR"
):
    return (
        f'01-03-2024,10:00,01-03-2024,{product},{isin},"{description}",,{currency},'
        f'"{amount}",{currency},"{saldo}",'
    )


def test_a_buy_books_a_lot_at_the_cash_actually_paid():
    """Total cost, not unit price: 5 @ 0,2784 is 1.392, but 1.39 left the account.

    A transaction that misses by a fraction of a cent is rejected outright, so the cash paid
    is the cost.
    """
    (txn,) = transactions(parse(trade_row("Koop 5 @ 0,2784 USD", "-1,39", currency="USD")))

    cash, position = txn.postings
    assert position.account == POSITIONS
    assert position.units.number == Decimal("5")
    assert position.units.currency == "IE00B4L5Y983"
    assert position.cost.number == Decimal("1.39")
    assert position.cost.total is True
    assert cash.units.number == Decimal("-1.39")


def test_the_cash_amount_disambiguates_the_number_format():
    """`1.172` is either 1.172 or 1172, and the description cannot say which.

    Multiplying against the cash that moved settles it, rather than guessing.
    """
    (txn,) = transactions(parse(trade_row("Koop 2 @ 1.172 EUR", "-2.344,00")))

    assert txn.postings[1].units.number == Decimal("2")
    assert txn.postings[1].cost.number == Decimal("2344.00")


def test_a_bond_quoted_as_a_percentage_of_nominal():
    (txn,) = transactions(parse(trade_row("Koop 8.305 @ 96,341 EUR", "-8.001,12")))

    assert txn.postings[1].units.number == Decimal("8305")


def test_a_sale_names_no_lot_so_the_booking_method_picks_one():
    directives = parse(
        trade_row("Verkoop 2 @ 100,00 EUR", "200,00"),
        trade_row("Koop 5 @ 100,00 EUR", "-500,00"),
    )
    sale = next(t for t in transactions(directives) if t.postings[0].account == POSITIONS)

    position, cash, gain = sale.postings
    assert position.units.number == Decimal("-2")
    assert position.cost.number is None  # renders as {}
    assert position.price.number == Decimal("100.00")
    assert cash.units.number == Decimal("200.00")
    assert gain.account == "Income:CapitalGains:Broker" and gain.units is None


def test_a_split_is_booked_with_its_side_taken_from_the_cash_sign():
    """Split rows name no side. They come in pairs whose cash cancels: out means acquired."""
    directives = parse(
        trade_row("SPLIT AANPASSING: 17 Sony @ 95,14 USD", "1.617,38", currency="USD"),
        trade_row("SPLIT AANPASSING: 85 Sony @ 19,028 USD", "-1.617,38", currency="USD"),
    )
    acquired, retired = transactions(directives)

    assert acquired.postings[1].units.number == Decimal("85")
    assert retired.postings[0].units.number == Decimal("-17")


def test_a_disposal_of_shares_never_booked_stays_cash_only():
    """Spin-offs create shares with no cash, so nothing proves their quantity.

    Reducing a position the parser never created makes Beancount reject the transaction, so
    the row falls back to cash and surfaces in review instead.
    """
    (txn,) = transactions(parse(trade_row("Verkoop 2 @ 10,89 USD", "21,78", currency="USD")))

    assert len(txn.postings) == 2
    assert txn.postings[1].account is None  # left for the rules engine


def test_positions_are_opened_with_fifo():
    directives = parse(trade_row("Koop 5 @ 100,00 EUR", "-500,00"))
    (opened,) = [d for d in directives if isinstance(d, Open)]

    assert opened.account == POSITIONS
    assert opened.booking == "FIFO"


def test_no_open_when_nothing_was_booked():
    assert [d for d in parse(FEE) if isinstance(d, Open)] == []


def test_a_trade_priced_in_another_currency_than_the_cash_stays_cash_only():
    (txn,) = transactions(parse(trade_row("Koop 5 @ 10,00 USD", "-50,00", currency="EUR")))

    assert len(txn.postings) == 2
    assert txn.postings[1].account is None


def test_a_trade_whose_arithmetic_does_not_reconcile_stays_cash_only():
    """Better a row that shows up in review than a holding invented from a misread number."""
    (txn,) = transactions(parse(trade_row("Koop 3 @ 7,00 EUR", "-999,00")))

    assert len(txn.postings) == 2
    assert txn.postings[1].account is None


def test_crypto_without_an_isin_uses_the_product_name():
    (txn,) = transactions(
        parse(trade_row("Koop 0,595193 @ 1.980,59 EUR", "-1.178,83", isin="", product="BITCOIN"))
    )

    assert txn.postings[1].units.currency == "BITCOIN"
    assert txn.postings[1].units.number == Decimal("0.595193")
