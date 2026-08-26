import csv
from decimal import Decimal
from datetime import datetime
from finance.models import Account, Transaction, Currency
from typing import Iterable


def parse_from_io(f) -> Iterable[Transaction]:
    reader = csv.reader(f, dialect=csv.excel())
    for row in reader:
        print(row)
        if len(row) != 12:
            # invalid entry
            continue

        date, time, valuta_date, product, isin, description, fx, mutation, mutation_value, balance, balance_value, order_id = row

        if date == 'Datum' and time == 'Tijd':
            # skip first row
            continue
        elif date == '' and time == '':
            # invalid entry
            continue

        if mutation != 'EUR':
            # currently we only support EUR
            continue

        timestamp = datetime.strptime(f"{date} {time}", "%d-%m-%Y %H:%M")
        yield Transaction(
            source=Account('', "DEGIRO"),
            destination=Account('', None),
            timestamp=timestamp,
            amount=Decimal(mutation_value.replace(",", ".")),
            currency=Currency.EUR,
            description=f"{product} ({isin}): {description}" if product != '' else description,
        )


def parse_file(file: str) -> Iterable[Transaction]:
    with open(file, 'r') as f:
        yield from parse_from_io(f)
