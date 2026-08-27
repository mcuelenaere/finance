"""ING Belgium account statement CSV.

The counterparty-extraction logic below is the most valuable code in this repo: ING does not
give a clean counterparty field for card payments or direct debits, so the name has to be
recovered from free-text description strings. Those patterns were built up against a decade of
real statements — change them only with a failing test in hand.

Note there is no running-balance column in this export, so no ``Balance`` directives can be
emitted for ING.
"""

from __future__ import annotations

import csv
import re
from collections.abc import Iterator

from finance.models import FLAG_OKAY, Amount, Posting, Transaction
from finance.parsers.base import Accounts, parse_date, parse_decimal, resolve_account
from finance.text import normalize_text

INSTITUTION = "ing_be"


class IngDialect(csv.excel):
    delimiter = ";"


def _counterparty(row: dict[str, str]) -> tuple[str | None, str | None]:
    """Recover ``(name, iban)`` of the other side of the transaction.

    Returns ``(None, None)`` when nothing can be determined, which is normal for bank-internal
    bookings such as interest or collective transfers.
    """
    description = row.get("Omschrijving") or ""
    detail = row.get("Detail van de omzet") or ""

    if description.startswith("Europese overschrijving"):
        name = number = None
        for part in re.split(r"\s{3,}", detail):
            if part.startswith("Van: ") or part.startswith("Ten gunste van: "):
                name = part.split(": ", 2)[1]
            elif part.startswith("IBAN: ") or part.startswith("IBAN : "):
                number = part.split(": ", 2)[1]
        if name or number:
            return name, number

    if description.startswith("Europese domiciliëring"):
        name = number = None
        for part in re.split(r"\s{3,}", detail):
            if part.startswith("Vandaag, debiteren wij uw rekening voor: "):
                name = re.split(r"\s+", part, maxsplit=2)[1]
            elif part.startswith("Rekening: "):
                number = re.split(r"\s+", part, maxsplit=2)[1]
        if name and number:
            return name, number

    if (
        description.startswith("ING Smart Banking-betaling")
        or description.startswith("Overschrijving MyING.be")
        or description.startswith("Overschrijving Home'Bank")
        or description.startswith("Overschrijving in euro (SEPA): ING Smart Banking")
    ):
        for part in re.split(r"\s{3,}", description):
            if part.startswith("Naar: "):
                name, _, number = part.replace("Naar: ", "").partition(" - ")
                if name and number:
                    return name, number

    if row.get("Rekening tegenpartij"):
        return None, row["Rekening tegenpartij"]

    if (
        description.startswith("Aankoop Bancontact/Mister Cash")
        or description.startswith("Aankoop Maestro")
        or description.startswith("Opvraging Bancontact/Mister Cash")
    ):
        for line in re.split(r"\s{3,}", description):
            parts = line.split(" - ")
            if len(parts) >= 3:
                return " - ".join(parts[2:]), None

    match = re.match(
        r"^(Opvraging Self'Bank|Opvraging Bancontact/Mister Cash|"
        r"Betaling aankoop Bancontact/Mister Cash|Aankoop Maestro|Opvraging Maestro|"
        r"Betaling tankbeurt Bancontact/Mister Cash)\s{3,}[\- \d,]+\s+([^,]+), \d+/\d+, ",
        description,
    )
    if match:
        return match.group(2), None

    match = re.match(r"^Kaart: [\-\d]+\s{3,}(.+) \d+/\d+ \d+:\d+", description)
    if match:
        return match.group(1), None

    return None, None


def parse_from_io(handle, accounts: Accounts, *, source_file: str = "") -> Iterator[Transaction]:
    for row in csv.DictReader(handle, dialect=IngDialect()):
        if not row.get("Bedrag") or not row.get("Valutadatum"):
            continue

        own = resolve_account(accounts, row["Rekeningnummer"], source=source_file or INSTITUTION)
        name, iban = _counterparty(row)
        currency = (row.get("Munteenheid") or "EUR").strip() or "EUR"

        meta: dict[str, object] = {"institution": INSTITUTION}
        if source_file:
            meta["source_file"] = source_file
        if iban:
            meta["counterparty_iban"] = normalize_text(iban)
        if row.get("Omzetnummer"):
            # Stable within an account+year; recorded so dedup could become exact later.
            meta["entry_number"] = row["Omzetnummer"].strip()
        # `Omschrijving` is the human-readable summary and becomes the narration.
        # `Detail van de omzet` is the raw block the counterparty was extracted from — kept as
        # metadata so it stays available to rules and to Fava without dominating the ledger.
        if row.get("Detail van de omzet"):
            meta["detail"] = normalize_text(row["Detail van de omzet"])
        if row.get("Bericht"):
            meta["message"] = normalize_text(row["Bericht"])

        yield Transaction(
            meta=meta,
            date=parse_date(row["Valutadatum"]),
            flag=FLAG_OKAY,
            payee=normalize_text(name) if name else None,
            narration=normalize_text(row.get("Omschrijving") or ""),
            tags=frozenset(),
            links=frozenset(),
            postings=(
                Posting(account=own, units=Amount(parse_decimal(row["Bedrag"]), currency)),
                Posting(account=None, units=None),
            ),
        )


def parse_file(path: str, accounts: Accounts) -> Iterator[Transaction]:
    with open(path, encoding="Windows-1252") as handle:
        yield from parse_from_io(handle, accounts, source_file=path)
