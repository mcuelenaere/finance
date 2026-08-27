"""Belfius account statement CSV.

The file opens with a twelve-line preamble of export parameters. Two of those lines are worth
keeping: ``Laatste saldo`` and its timestamp give the account's closing balance at export
time, which becomes a ``Balance`` directive — the only independent check we get that the
parser and the bank agree.
"""

from __future__ import annotations

import csv
import re
from collections.abc import Iterator
from datetime import timedelta

from finance.models import (
    FLAG_OKAY,
    Amount,
    Balance,
    Directive,
    Posting,
    Transaction,
)
from finance.parsers.base import Accounts, parse_date, parse_decimal, resolve_account
from finance.text import normalize_text

INSTITUTION = "belfius_be"

#: Rows of export parameters before the real CSV header.
PREAMBLE_LINES = 12

_MAESTRO = re.compile(r"MAESTRO-BETALING \d+/\d+-(.+?) \d+,\d+\s+EUR")
_LAST_BALANCE = re.compile(r"^Laatste saldo;([\d.,\-]+)\s*([A-Z]{3})", re.MULTILINE)
_LAST_BALANCE_DATE = re.compile(
    r"^Datum/uur van het laatste saldo;(\d{2}/\d{2}/\d{4})", re.MULTILINE
)


def _counterparty(row: dict[str, str]) -> tuple[str | None, str | None]:
    iban = row.get("Rekening tegenpartij") or None
    name = row.get("Naam tegenpartij bevat") or None

    # Card payments carry no counterparty field; the merchant is inside the free-text message.
    if not name:
        message = row.get("Mededelingen") or ""
        if message.startswith("MAESTRO-BETALING"):
            match = _MAESTRO.match(message)
            if match:
                name = match.group(1)

    return name, iban


def parse_from_io(handle, accounts: Accounts, *, source_file: str = "") -> Iterator[Directive]:
    preamble = [next(handle, "") for _ in range(PREAMBLE_LINES)]

    seen_accounts: set[str] = set()

    for row in csv.DictReader(handle, delimiter=";"):
        if not row.get("Bedrag") or not row.get("Valutadatum"):
            continue

        own_account = resolve_account(accounts, row["Rekening"], source=source_file or INSTITUTION)
        seen_accounts.add(own_account)
        name, iban = _counterparty(row)
        currency = (row.get("Devies") or "EUR").strip() or "EUR"

        meta: dict[str, object] = {"institution": INSTITUTION}
        if source_file:
            meta["source_file"] = source_file
        if iban:
            meta["counterparty_iban"] = normalize_text(iban)
        # Statement + transaction number are stable within an account+year; recorded so dedup
        # could become exact later without re-parsing everything.
        if row.get("Rekeninguittrekselnummer"):
            meta["statement_number"] = row["Rekeninguittrekselnummer"].strip()
        if row.get("Transactienummer"):
            meta["entry_number"] = row["Transactienummer"].strip()
        if row.get("Transactie"):
            meta["description"] = normalize_text(row["Transactie"])

        yield Transaction(
            meta=meta,
            date=parse_date(row["Valutadatum"]),
            flag=FLAG_OKAY,
            payee=normalize_text(name) if name else None,
            narration=normalize_text(row.get("Mededelingen") or ""),
            tags=frozenset(),
            links=frozenset(),
            postings=(
                Posting(
                    account=own_account,
                    units=Amount(parse_decimal(row["Bedrag"]), currency),
                ),
                Posting(account=None, units=None),
            ),
        )

    # Only safe to attach the header balance when the file covers a single account.
    if len(seen_accounts) == 1:
        balance = _parse_header_balance("".join(preamble), next(iter(seen_accounts)), source_file)
        if balance is not None:
            yield balance


def _parse_header_balance(preamble: str, own_account: str, source_file: str) -> Balance | None:
    amount_match = _LAST_BALANCE.search(preamble)
    date_match = _LAST_BALANCE_DATE.search(preamble)
    if not amount_match or not date_match:
        return None

    meta: dict[str, object] = {"institution": INSTITUTION}
    if source_file:
        meta["source_file"] = source_file

    # The bank states a *closing* balance for that day, but Beancount evaluates `balance` at
    # the start of the date given — so assert it on the following day, after the day's
    # transactions have been applied.
    return Balance(
        meta=meta,
        date=parse_date(date_match.group(1)) + timedelta(days=1),
        account=own_account,
        amount=Amount(parse_decimal(amount_match.group(1)), amount_match.group(2)),
    )


def parse_file(path: str, accounts: Accounts) -> Iterator[Directive]:
    with open(path, encoding="utf-8-sig") as handle:
        yield from parse_from_io(handle, accounts, source_file=path)
