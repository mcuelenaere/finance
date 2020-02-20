import csv
import re
from decimal import Decimal
from datetime import datetime
from finance.models import Account, Transaction, Currency
from typing import Iterable


class BelfiusDialect(csv.excel):
    delimiter = ';'


def try_parse_destination(row):
    number = row['Rekening tegenpartij'] if row['Rekening tegenpartij'] else None
    name = row['Naam tegenpartij bevat'] if row['Naam tegenpartij bevat'] else None

    if not name and row['Mededelingen'].startswith('MAESTRO-BETALING'):
        m = re.match(r'MAESTRO-BETALING \d+/\d+-(.+) \d+,\d+\s+EUR', row['Mededelingen'])
        if m:
            name = m[1]

    return Account(number=number, name=name)


def parse_from_io(f) -> Iterable[Transaction]:
    # skip header
    for _ in range(12):
        next(f)

    reader = csv.DictReader(f, dialect=BelfiusDialect())
    for row in reader:
        source = Account(
            number=row['Rekening'],
            name=None
        )
        destination = try_parse_destination(row)
        currency = Currency.EUR if row['Devies'] == 'EUR' else None  # FIXME
        amount = Decimal(row['Bedrag'].replace(",", ".")) if row['Bedrag'] else None
        timestamp = datetime.strptime(row['Valutadatum'], "%d/%m/%Y") if row['Valutadatum'] else None

        if not destination:
            # print(dict(row))
            continue

        yield Transaction(
            source=source,
            destination=destination,
            timestamp=timestamp,
            amount=amount,
            currency=currency,
            description=row['Mededelingen'].strip(),
        )


def parse_file(file: str) -> Iterable[Transaction]:
    with open(file, 'r') as f:
        yield from parse_from_io(f)
