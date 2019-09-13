import csv
import re
from decimal import Decimal
from datetime import datetime
from finance.models import Account, Transaction, Currency
from typing import Iterable, Optional


class IngDialect(csv.excel):
    delimiter = ';'


def try_parse_destination(row: dict) -> Optional[Account]:
    if row['Omschrijving'].startswith('Europese overschrijving'):
        name = None
        number = None
        for part in re.split(r"\s{3,}", row['Detail van de omzet']):
            if part.startswith("Van: ") or part.startswith('Ten gunste van: '):
                name = part.split(": ", 2)[1]
            elif part.startswith("IBAN: ") or part.startswith("IBAN : "):
                number = part.split(": ", 2)[1]
        if name or number:
            return Account(number=number, name=name)

    if row['Omschrijving'].startswith('Europese domiciliëring'):
        name = None
        number = None
        for part in re.split(r"\s{3,}", row['Detail van de omzet']):
            if part.startswith("Vandaag, debiteren wij uw rekening voor: "):
                name = re.split("\s+", part, 2)[1]
            elif part.startswith("Rekening: "):
                number = re.split("\s+", part, 2)[1]
        if name and number:
            return Account(number=number, name=name)

    if row['Omschrijving'].startswith('ING Smart Banking-betaling') or \
       row['Omschrijving'].startswith('Overschrijving MyING.be') or \
       row['Omschrijving'].startswith('Overschrijving Home\'Bank') or \
       row['Omschrijving'].startswith('Overschrijving in euro (SEPA): ING Smart Banking'):
        for part in re.split(r"\s{3,}", row['Omschrijving']):
            if part.startswith("Naar: "):
                name, number = part.replace('Naar: ', '').split(' - ', 2)
                if name and number:
                    return Account(number=number, name=name)

    if row['Rekening tegenpartij']:
        return Account(
            number=row['Rekening tegenpartij'],
            name=None
        )

    if row['Omschrijving'].startswith('Aankoop Bancontact/Mister Cash') or \
       row['Omschrijving'].startswith('Aankoop Maestro') or \
       row['Omschrijving'].startswith('Opvraging Bancontact/Mister Cash'):
        for line in re.split(r"\s{3,}", row['Omschrijving']):
            parts = line.split(" - ")
            if len(parts) >= 3:
                return Account(number=None, name=' - '.join(parts[2:]))

    m = re.match(
        r'^(Opvraging Self\'Bank|Opvraging Bancontact/Mister Cash|Betaling aankoop Bancontact/Mister Cash|Aankoop Maestro|Opvraging Maestro|Betaling tankbeurt Bancontact/Mister Cash)\s{3,}[\- \d,]+\s+([^,]+), \d+/\d+, ',
        row['Omschrijving']
    )
    if m:
        return Account(number=None, name=m.group(2))

    m = re.match(
        r'^Kaart: [\-\d]+\s{3,}(.+) \d+/\d+ \d+:\d+',
        row['Omschrijving']
    )
    if m:
        return Account(number=None, name=m.group(1))

    return None


def parse_from_io(f) -> Iterable[Transaction]:
    reader = csv.DictReader(f, dialect=IngDialect())
    for row in reader:
        source = Account(
            number=row['Rekeningnummer'],
            name=row['Naam van de rekening']
        )
        destination = try_parse_destination(row)
        currency = Currency.EUR if row['Munteenheid'] == 'EUR' else None  # FIXME
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
            description=row['Omschrijving'].strip(),
        )


def parse_file(file: str) -> Iterable[Transaction]:
    with open(file, 'r', encoding='Windows-1252') as f:
        yield from parse_from_io(f)
