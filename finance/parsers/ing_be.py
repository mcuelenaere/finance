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


#: A direct debit, under both names ING has used for one. The creditor is never in the
#: description; it has to be read out of the detail block.
DIRECT_DEBIT_PREFIXES = ("Europese domiciliëring", "Domiciliering in euro")

#: The label the creditor's name follows. It sits on the *next* part, not this one — the label
#: always ends at its colon. Requiring the name on the same part is why every direct debit in
#: this repo lost its counterparty: the condition matched nothing, in either spelling.
DEBTOR_LABEL = "Vandaag, debiteren wij uw rekening voor:"

#: The older first-payment notice announces a new mandate instead, and labels the creditor.
CREDITOR_LABEL = "Schuldeiser"

#: SEPA creditor identifier. Worth keeping: it is issued to the company and outlives the
#: trading name on the statement, so a rule keyed on it survives a rebrand that a rule keyed
#: on the name does not.
CREDITOR_ID_LABELS = ("Identificatienr", "Schuldeisers ID.")


def _parts(block: str) -> list[str]:
    return [part.strip() for part in re.split(r"\s{3,}", block) if part.strip()]


def _labelled(part: str, label: str) -> str | None:
    """The value after ``label`` on this part, if any. Tolerates a space before the colon."""
    for separator in (": ", " : "):
        prefix = label + separator
        if part.startswith(prefix):
            return part[len(prefix) :].strip() or None
    return None


#: How the detail block labels the two sides of a transfer. `Van:` is the payer, and the two
#: `... gunste van:` forms the payee; which appears depends on the direction.
PARTY_LABELS = ("Van", "Ten gunste van")


#: Both sides of a transfer on a single part, joined by a hyphen.
HYPHENATED_LABELS = ("Naar", "Ten gunste van", "Van")


def _hyphenated_party(block: str) -> tuple[str | None, str | None]:
    """``(name, IBAN)`` from a ``Naar: <name> - <iban>`` part, if one is present."""
    for part in _parts(block):
        for label in HYPHENATED_LABELS:
            value = _labelled(part, label)
            if value is None:
                continue
            name, _, number = value.partition(" - ")
            if name.strip() and number.strip():
                return name.strip(), number.strip()
    return None, None


def _detail_parties(detail: str) -> tuple[str | None, str | None]:
    """``(counterparty name, IBAN)`` from a transfer's detail block.

    The name's part carries only the name — the address follows on parts of its own — so no
    trimming is needed beyond the label.
    """
    name = number = None
    for part in _parts(detail):
        for label in PARTY_LABELS:
            name = name or _labelled(part, label)
        number = number or _labelled(part, "IBAN")
    return name, number


def _is_direct_debit(description: str) -> bool:
    return description.startswith(DIRECT_DEBIT_PREFIXES)


def _direct_debit_parties(detail: str) -> tuple[str | None, str | None]:
    """``(creditor name, creditor IBAN)`` for a direct debit."""
    name = number = None
    parts = _parts(detail)
    for index, part in enumerate(parts):
        if part.startswith(DEBTOR_LABEL) and index + 1 < len(parts):
            name = name or parts[index + 1]
        else:
            name = name or _labelled(part, CREDITOR_LABEL)
        number = number or _labelled(part, "Rekening")
    return name, number


def creditor_id(row: dict[str, str]) -> str | None:
    """The SEPA creditor identifier of a direct debit, if this row is one."""
    if not _is_direct_debit(row.get("Omschrijving") or ""):
        return None
    for part in _parts(row.get("Detail van de omzet") or ""):
        for label in CREDITOR_ID_LABELS:
            value = _labelled(part, label)
            if value:
                return value
    return None


def _counterparty(row: dict[str, str]) -> tuple[str | None, str | None]:
    """Recover ``(name, iban)`` of the other side of the transaction.

    Returns ``(None, None)`` when nothing can be determined, which is normal for bank-internal
    bookings such as interest or collective transfers.
    """
    description = row.get("Omschrijving") or ""
    detail = row.get("Detail van de omzet") or ""

    # A transfer's detail block labels each side on its own part. This used to be gated on the
    # description starting with "Europese overschrijving", which ING does not always write:
    # two of its spellings are `Overschrijvingin euro` and `Overschrijvingin Euro`, missing a
    # space in the bank's own export. Those rows arrived with no counterparty at all — and
    # where the statement's counterparty column was also empty, with no IBAN either, so
    # transfers to accounts the owner holds could not be recognised as internal.
    name, number = _detail_parties(detail)
    if name or number:
        return name, number

    if _is_direct_debit(description):
        name, number = _direct_debit_parties(detail)
        if name or number:
            return name, number

    # The other shape a transfer uses: `Naar: <name> - <iban>`, both sides on one part.
    #
    # Searched in the description AND the detail, because ING puts it in either. This was once
    # gated on a list of description prefixes, which meant every rename of a payment channel
    # silently dropped the counterparty — one spelling differed from a listed one by a single
    # colon. Keying on the label makes the channel irrelevant; searching both fields makes it
    # irrelevant which of them the bank chose to write it in.
    for block in (description, detail):
        name, number = _hyphenated_party(block)
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
        mandate_creditor = creditor_id(row)
        if mandate_creditor:
            meta["creditor_id"] = normalize_text(mandate_creditor)
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
