import pytest

from decimal import Decimal
from datetime import datetime
from finance.parsers import degiro
from finance.models import Account, Currency, Transaction
from io import StringIO

testcases = (
    (
        r'04-01-2021,15:31,04-01-2021,ASML HOLDING N.V. - AD,USN070592100,DEGIRO transactiekosten,,EUR,"-0,01",EUR,"20,07",xxx-xxx-xxx-xxx-xxxx',
        Transaction(
            source=Account(number="", name=None),
            destination=Account(name="", number=None),
            amount=Decimal("-0.01"),
            currency=Currency.EUR,
            timestamp=datetime.fromisoformat("2021-01-04T15:31:00"),
            description="ASML HOLDING N.V. - AD (USN070592100): DEGIRO transactiekosten"
        )
    ),
)

CSV_HEADER = "Datum,Tijd,Valutadatum,Product,ISIN,Omschrijving,FX,Mutatie,,Saldo,,OrderID"


@pytest.mark.parametrize("input,expected", testcases)
def test(input, expected):
    buf = StringIO(CSV_HEADER + "\n" + input)
    transactions = tuple(degiro.parse_from_io(buf))
    assert len(transactions) == 1
    assert transactions[0] == expected
