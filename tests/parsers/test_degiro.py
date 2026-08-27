from decimal import Decimal
from io import StringIO

from finance.parsers import degiro

ACCOUNTS = {"DEGIRO": "Assets:Investments:Degiro:Cash"}

CSV_HEADER = "Datum,Tijd,Valutadatum,Product,ISIN,Omschrijving,FX,Mutatie,,Saldo,,Order Id"


def test_fee_row():
    row = (
        "04-01-2021,15:31,04-01-2021,ASML HOLDING N.V. - AD,USN070592100,"
        'DEGIRO transactiekosten,,EUR,"-0,01",EUR,"20,07",xxx-xxx-xxx-xxx-xxxx'
    )
    (txn,) = tuple(degiro.parse_from_io(StringIO(f"{CSV_HEADER}\n{row}"), ACCOUNTS))

    assert txn.date.isoformat() == "2021-01-04"
    assert txn.payee == "DEGIRO"
    assert txn.narration == "ASML HOLDING N.V. - AD (USN070592100): DEGIRO transactiekosten"
    assert txn.meta["isin"] == "USN070592100"
    assert txn.meta["order_id"] == "xxx-xxx-xxx-xxx-xxxx"

    own, unresolved = txn.postings
    assert own.account == "Assets:Investments:Degiro:Cash"
    assert own.units.number == Decimal("-0.01")
    assert own.units.currency == "EUR"
    assert unresolved.account is None


def test_header_and_blank_rows_are_skipped():
    rows = "\n".join(
        [
            CSV_HEADER,
            ",,,,,,,,,,,",
            '04-01-2021,15:31,04-01-2021,,,Storting,,EUR,"1.000,00",EUR,"1.020,07",',
        ]
    )
    (txn,) = tuple(degiro.parse_from_io(StringIO(rows), ACCOUNTS))

    assert txn.narration == "Storting"
    # A four-figure deposit: the thousands separator must survive.
    assert txn.postings[0].units.number == Decimal("1000.00")
