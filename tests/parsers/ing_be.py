import pytest

from decimal import Decimal
from datetime import datetime
from finance.parsers import ing_be
from finance.models import Account, Currency, Transaction
from io import StringIO

testcases = (
    (
        r"390-1234567-89;DE H JOHN DOE;;159;01/04/2019;03/04/2019;-10,44;EUR;Aankoop Bancontact/Mister Cash  "
        + r"                                                03/04 - 12.15 uur - PROXY DELHAIZE K - BRUGGE - BEL       "
        + r"                      Kaartnummer 6703 39XX XXXX 1234 5                                                   "
        + r"                                                                                ;;",
        Transaction(
            source=Account(number="390-1234567-89", name="DE H JOHN DOE"),
            destination=Account(name="PROXY DELHAIZE K - BRUGGE - BEL", number=None),
            amount=Decimal("-10.44"),
            currency=Currency.EUR,
            timestamp=datetime.fromisoformat("2019-04-03T00:00:00"),
            description="Aankoop Bancontact/Mister Cash                                                  03/04 - 12.15 uur - PROXY DELHAIZE K - BRUGGE - BEL                             Kaartnummer 6703 39XX XXXX 1234 5"
        )
    ),
)

CSV_HEADER = "Rekeningnummer;Naam van de rekening;Rekening tegenpartij;Omzetnummer;Boekingsdatum;Valutadatum;Bedrag;Munteenheid;Omschrijving;Detail van de omzet;Bericht"


@pytest.mark.parametrize("input,expected", testcases)
def test(input, expected):
    buf = StringIO(CSV_HEADER + "\n" + input)
    transactions = tuple(ing_be.parse_from_io(buf))
    assert len(transactions) == 1
    assert transactions[0] == expected
