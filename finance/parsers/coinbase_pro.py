import csv
from decimal import Decimal
from datetime import datetime
from finance.models import Account, Transaction, Currency
from typing import Iterable


def parse_from_io(f) -> Iterable[Transaction]:
    reader = csv.DictReader(f, dialect=csv.excel())
    for row in reader:
        print(row)

        if row['amount/balance unit'] != 'EUR':
            continue

        yield Transaction(
            source=Account('', f"Coinbase Pro: {row['portfolio']}"),
            destination=Account('', None),
            timestamp=datetime.strptime(row['time'], "%Y-%m-%dT%H:%M:%S.%fZ"),
            amount=Decimal(row['amount']),
            currency=Currency.EUR,
            description=f"{row['type']}: {row['portfolio']}",
        )


def parse_file(file: str) -> Iterable[Transaction]:
    with open(file, 'r') as f:
        yield from parse_from_io(f)
