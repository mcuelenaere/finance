import pytest

from decimal import Decimal
from datetime import datetime
from finance.parsers import belfius_be
from finance.models import Account, Currency, Transaction
from io import StringIO

testcases = (
    (
        r"BE33 1234 5678 9123;07/01/2020;00001;5;BE16 1234 5678 9123;Foo bar;Kerkstraat 165 b. 9;9000 Gent;"
        + r"OVERSCHRIJVING BELFIUS MOBILE NAAR BE16 1234 5678 9123 Foo bar Kerkstraat 165 b. 9 9000   Gent BE"
        + r" +++712/3456/45754+++                       REF. : 0905413217072 VAL. 07-01;07/01/2020;-9,68;EUR;"
        + r"GKCCBEBB;BE;+++712/3456/45754+++",
        Transaction(
            source=Account(number="BE33 1234 5678 9123", name=None),
            destination=Account(number="BE16 1234 5678 9123", name="Foo bar"),
            amount=Decimal("-9.68"),
            currency=Currency.EUR,
            timestamp=datetime.fromisoformat("2020-01-07T00:00:00"),
            description="+++712/3456/45754+++"
        )
    ),
    (
        r"BE33 1234 5678 9123;13/02/2020;00005;17;;;;;UW COLLECTIEVE OVERSCHRIJVING LONEN ISABEL BESTAND :"
        + r"   010/2020-02-13 100001 GROEP : 010/2020-0/0000          REF. : 0000000000001 VAL. 13-02;13/02/2020;"
        + r"-123,45;EUR;;;UW COLLECTIEVE OVERSCHRIJVING LONEN ISABEL BESTAND :   050/2020-02-13 140003 GROEP :"
        + r" 010/2020-0/0000          REF. : 0000000000001 VAL. 13-02",
        Transaction(
            source=Account(number="BE33 1234 5678 9123", name=None),
            destination=Account(number=None, name=None),
            amount=Decimal("-123.45"),
            currency=Currency.EUR,
            timestamp=datetime.fromisoformat("2020-02-13T00:00:00"),
            description="UW COLLECTIEVE OVERSCHRIJVING LONEN ISABEL BESTAND :   050/2020-02-13 140003 GROEP : 010/2020-0/0000          REF. : 0000000000001 VAL. 13-02"
        )
    )
)

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
Rekening;Boekingsdatum;Rekeninguittrekselnummer;Transactienummer;Rekening tegenpartij;Naam tegenpartij bevat;Straat en nummer;Postcode en plaats;Transactie;Valutadatum;Bedrag;Devies;BIC;Landcode;Mededelingen"""


@pytest.mark.parametrize("input,expected", testcases)
def test(input, expected):
    buf = StringIO(CSV_HEADER + "\n" + input)
    transactions = tuple(belfius_be.parse_from_io(buf))
    assert len(transactions) == 1
    assert transactions[0] == expected
