from decimal import Decimal
from io import StringIO

import pytest

from finance.models import Balance, Transaction
from finance.parsers import belfius_be

ACCOUNTS = {"BE33123456789123": "Assets:Belfius:Checking"}

CSV_HEADER = """Boekingsdatum vanaf;
Boekingsdatum tot en met;
Bedrag vanaf;
Bedrag tot en met;
Rekeninguittrekselnummer vanaf;
Rekeninguittrekselnummer tot en met;
Mededeling;
Naam tegenpartij bevat;
Rekening tegenpartij;
Laatste saldo;1.234,56 EUR
Datum/uur van het laatste saldo;09/02/2020 21:30:53
;
Rekening;Boekingsdatum;Rekeninguittrekselnummer;Transactienummer;Rekening tegenpartij;\
Naam tegenpartij bevat;Straat en nummer;Postcode en plaats;Transactie;Valutadatum;Bedrag;\
Devies;BIC;Landcode;Mededelingen"""

TESTCASES = (
    pytest.param(
        r"BE33 1234 5678 9123;07/01/2020;00001;5;BE16 1234 5678 9123;Foo bar;"
        r"Kerkstraat 165 b. 9;9000 Gent;OVERSCHRIJVING BELFIUS MOBILE NAAR "
        r"BE16 1234 5678 9123 Foo bar;07/01/2020;-9,68;EUR;GKCCBEBB;BE;"
        r"+++712/3456/45754+++",
        "Foo bar",
        "BE16 1234 5678 9123",
        Decimal("-9.68"),
        id="mobile-transfer",
    ),
    pytest.param(
        r"BE33 1234 5678 9123;13/02/2020;00005;17;;;;;UW COLLECTIEVE OVERSCHRIJVING;"
        r"13/02/2020;-123,45;EUR;;;UW COLLECTIEVE OVERSCHRIJVING LONEN",
        None,
        None,
        Decimal("-123.45"),
        id="no-counterparty",
    ),
    pytest.param(
        r"BE33 1234 5678 9123;03/02/2020;00002;73;;;;;MAESTRO;03/02/2020;-8,00;EUR;;;"
        r"MAESTRO-BETALING 01/02-GENT COMIC SANS BE 8,00 EUR     KAART NR 1234",
        "GENT COMIC SANS BE",
        None,
        Decimal("-8"),
        id="maestro-merchant-from-message",
    ),
)


def _parse(row: str):
    return tuple(belfius_be.parse_from_io(StringIO(f"{CSV_HEADER}\n{row}"), ACCOUNTS))


@pytest.mark.parametrize("row,payee,iban,amount", TESTCASES)
def test_counterparty_extraction(row, payee, iban, amount):
    directives = _parse(row)
    txn = next(d for d in directives if isinstance(d, Transaction))

    assert txn.payee == payee
    assert txn.meta.get("counterparty_iban") == iban
    assert txn.postings[0].account == "Assets:Belfius:Checking"
    assert txn.postings[0].units.number == amount
    assert txn.postings[1].account is None


def test_header_balance_becomes_an_assertion_on_the_following_day():
    """The bank states a closing balance; Beancount asserts at the *start* of a date."""
    directives = _parse(TESTCASES[0].values[0])
    balance = next(d for d in directives if isinstance(d, Balance))

    assert balance.account == "Assets:Belfius:Checking"
    assert balance.amount.number == Decimal("1234.56")
    assert balance.amount.currency == "EUR"
    # Stated 09/02/2020, asserted the day after so that day's transactions are included.
    assert balance.date.isoformat() == "2020-02-10"
