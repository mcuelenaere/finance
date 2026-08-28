"""Belfius Mastercard statements.

Every statement here is invented, laid out the way the real PDFs extract. The behaviour being
pinned is mostly about what happens when a statement is *misread*: this is the one input format
where losing a row to a layout change is realistic, and a statement short of a row still adds up
to something plausible.
"""

from datetime import date
from decimal import Decimal

import pytest

from finance.models import Transaction
from finance.parsers import belfius_card
from finance.parsers.base import UnknownAccountError

ACCOUNTS = {"BELFIUS_CARD": "Liabilities:CreditCard:Belfius"}
CARD = "Liabilities:CreditCard:Belfius"

_UNSET = object()

HEAD = """Belfius Bank NV
Karel Rogierplein 11
1210 Brussel
BIC: GKCCBEBB
Afsluitingsdatum {closing}
Datum van debet {debited}
Transacties van {start} tot {end}
UITGAVENSTAAT
Pagina : 1-2
Kaarthouder
A Cardholder
Klantenreferentie 1234567890
Gebruikslimiet 1.900,00 EUR
Transacties - Kaartnummer 1234 12XX XXXX 5678 - A Cardholder
DATUM
TRANSACTIE
DATUM
VERREKENING OMSCHRIJVING BEDRAG"""

BOILERPLATE = """Pagina : 2-2
PRAKTISCHE  INFORMATIE
Bel onmiddellijk naar
Card Stop !
Vanuit het buitenland
+32 78 170 170
Tel. +32 2 205 85 85  -  Fax +32 2 205 85 88
Ma.-vr. : 8:30 - 18:00  -  za. : 9:00 - 17:00"""


def statement(
    *body,
    closing="25/05/2024",
    debited="03/06/2024",
    start="26/04/2024",
    end="25/05/2024",
    total="70,37 EUR -",
):
    lines = [HEAD.format(closing=closing, debited=debited, start=start, end=end), *body]
    if total is not None:
        lines.append(f"Totaal {total}")
    lines.append(BOILERPLATE)
    return "\n".join(lines)


def parse(text, accounts=_UNSET):
    resolved = ACCOUNTS if accounts is _UNSET else accounts
    return list(belfius_card.parse_from_text(text, resolved, source_file="statement.pdf"))


def transactions(directives):
    return [d for d in directives if isinstance(d, Transaction)]


# --- refusing to import a statement it cannot verify -----------------------------------


def test_a_statement_whose_rows_miss_the_stated_total_is_refused():
    """The check that makes parsing a PDF safe at all. A dropped row leaves a statement that
    still looks entirely plausible, so importing it is worse than failing."""
    text = statement(
        "01/05 02/05  WIDGET SHOP widgets.example IE 6,49 EUR -",
        total="70,37 EUR -",
    )

    with pytest.raises(belfius_card.StatementError, match="6.49"):
        parse(text)


def test_a_statement_with_no_stated_total_is_refused():
    text = statement("01/05 02/05  WIDGET SHOP widgets.example IE 6,49 EUR -", total=None)

    with pytest.raises(belfius_card.StatementError, match="cannot be verified"):
        parse(text)


def test_rows_adding_up_to_the_stated_total_are_imported():
    text = statement(
        "01/05 02/05  WIDGET SHOP widgets.example IE 6,49 EUR -",
        "14/05 15/05  CORNER GROCER groceries.example LU 12,99 EUR -",
        total="19,48 EUR -",
    )

    assert len(transactions(parse(text))) == 2


def test_a_statement_with_no_transactions_at_all_is_not_an_error():
    """The card has months of no use, and those months have no settlement on the bank side
    either."""
    assert parse(statement(total=None)) == []


# --- what a row becomes ----------------------------------------------------------------


def test_a_charge_draws_on_the_card_and_leaves_the_merchant_to_a_rule():
    text = statement("01/05 02/05  WIDGET SHOP widgets.example IE 6,49 EUR -", total="6,49 EUR -")

    (txn,) = transactions(parse(text))

    liability, expense = txn.postings
    assert liability.account == CARD
    assert liability.units == (Decimal("-6.49"), "EUR"), "a charge increases what is owed"
    assert expense.account is None, "the merchant is the rules engine's to decide"
    assert expense.units == (Decimal("6.49"), "EUR")
    assert txn.narration == "Card payment"


def test_the_trailing_country_code_becomes_metadata_not_part_of_the_payee():
    """So the same merchant reads the same whichever country it billed from."""
    text = statement("01/05 02/05  WIDGET SHOP widgets.example IE 6,49 EUR -", total="6,49 EUR -")

    (txn,) = transactions(parse(text))

    assert txn.payee == "WIDGET SHOP widgets.example"
    assert txn.meta["country"] == "IE"


def test_the_statement_and_debit_dates_are_recorded():
    text = statement("01/05 02/05  WIDGET SHOP widgets.example IE 6,49 EUR -", total="6,49 EUR -")

    (txn,) = transactions(parse(text))

    assert txn.date == date(2024, 5, 1), "dated when it was bought"
    assert txn.meta["settlement_date"] == date(2024, 5, 2)
    assert txn.meta["statement"] == date(2024, 5, 25)
    assert txn.meta["debited"] == date(2024, 6, 3)
    assert txn.meta["card"] == "1234 12XX XXXX 5678"


# --- a foreign-currency purchase -------------------------------------------------------


def test_only_the_euro_is_booked_and_the_original_is_kept():
    """The row states both amounts. What the card was charged is the euro; the original has no
    rate that would let it be converted independently, and does not need one."""
    text = statement(
        "23/05 24/05  WIDGET SHOP 650-253-0000 GB 1.899,00 ARS 2,00 EUR -",
        total="2,00 EUR -",
    )

    (txn,) = transactions(parse(text))

    liability, expense = txn.postings
    assert liability.units == (Decimal("-2.00"), "EUR")
    assert expense.units == (Decimal("2.00"), "EUR")
    assert txn.meta["foreign_amount"] == "1899.00 ARS"
    assert txn.payee == "WIDGET SHOP 650-253-0000"


def test_a_rate_split_across_two_lines_is_still_read():
    """The text extractor breaks after the ``=`` about as often as not, which is why the
    continuation is accumulated rather than matched in one go."""
    text = statement(
        "23/05 24/05  WIDGET SHOP 650-253-0000 GB 1.899,00 ARS 2,00 EUR -",
        "(Via ) 1 EUR =",
        "1.000,5ARS",
        total="2,00 EUR -",
    )

    (txn,) = transactions(parse(text))

    assert txn.meta["exchange_rate"] == "1 EUR = 1.000,5 ARS"


def test_a_rate_on_one_line_is_read_too():
    text = statement(
        "23/05 24/05  WIDGET SHOP 650-253-0000 GB 389,00 ARS 1,32 EUR -",
        "(Via ) 1 EUR = 123,45678901ARS",
        total="1,32 EUR -",
    )

    (txn,) = transactions(parse(text))

    assert txn.meta["exchange_rate"] == "1 EUR = 123,45678901 ARS"


def test_a_wallet_is_recorded_and_an_empty_one_is_not():
    text = statement(
        "01/05 02/05  WIDGET SHOP widgets.example IE 6,49 EUR -",
        "(Via Google Pay)",
        "14/05 15/05  CORNER GROCER groceries.example LU 1,00 EUR -",
        "(Via )",
        total="7,49 EUR -",
    )

    first, second = transactions(parse(text))

    assert first.meta["wallet"] == "Google Pay"
    assert "wallet" not in second.meta


# --- the year the rows do not carry ----------------------------------------------------


def test_a_period_spanning_new_year_resolves_each_row_to_the_right_year():
    """Statements run from the 26th to the 25th, so every December one crosses a year."""
    text = statement(
        "28/12 29/12  WIDGET SHOP widgets.example IE 10,00 EUR -",
        "03/01 04/01  CORNER GROCER groceries.example LU 20,00 EUR -",
        closing="25/01/2024",
        debited="03/02/2024",
        start="26/12/2023",
        end="25/01/2024",
        total="30,00 EUR -",
    )

    december, january = transactions(parse(text))

    assert december.date == date(2023, 12, 28)
    assert january.date == date(2024, 1, 3)


def test_a_settlement_date_is_resolved_forward_from_its_own_purchase():
    """It can fall after the period closes, so it cannot be resolved against the period — and a
    purchase on 31 December settles in the next year."""
    text = statement(
        "31/12 02/01  WIDGET SHOP widgets.example IE 10,00 EUR -",
        closing="25/01/2024",
        debited="03/02/2024",
        start="26/12/2023",
        end="25/01/2024",
        total="10,00 EUR -",
    )

    (txn,) = transactions(parse(text))

    assert txn.date == date(2023, 12, 31)
    assert txn.meta["settlement_date"] == date(2024, 1, 2)


# --- identity -------------------------------------------------------------------------


def test_the_transaction_id_comes_from_the_content_not_the_filename():
    """The PDFs are named with an export timestamp rather than a statement identifier, so
    re-exporting the same statement gives it a new name. Without a content-derived id every row
    would import a second time."""
    text = statement(
        "01/05 02/05  WIDGET SHOP widgets.example IE 6,49 EUR -",
        "14/05 15/05  CORNER GROCER groceries.example LU 12,99 EUR -",
        total="19,48 EUR -",
    )

    again = list(
        belfius_card.parse_from_text(text, ACCOUNTS, source_file="a-different-name.pdf")
    )

    first = [t.meta["transaction_id"] for t in transactions(parse(text))]
    second = [t.meta["transaction_id"] for t in transactions(again)]
    assert first == second == ["2024-05-25:0", "2024-05-25:1"]


def test_boilerplate_cannot_be_mistaken_for_a_transaction():
    """The second page is a page of phone numbers and opening hours. None of it starts with a
    pair of dates, which is what a row needs."""
    text = statement("01/05 02/05  WIDGET SHOP widgets.example IE 6,49 EUR -", total="6,49 EUR -")

    assert len(transactions(parse(text))) == 1


def test_an_undeclared_account_raises_rather_than_being_guessed():
    text = statement("01/05 02/05  WIDGET SHOP widgets.example IE 6,49 EUR -", total="6,49 EUR -")

    with pytest.raises(UnknownAccountError, match="belfius_card"):
        parse(text, accounts={})
