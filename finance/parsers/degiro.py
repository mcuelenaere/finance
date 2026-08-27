"""DEGIRO cash account export (``Account.csv``).

This file is the *cash* ledger only — deposits, withdrawals, fees, dividends and the cash leg
of trades. It carries no share counts, so no positions or cost basis can be derived from it;
that needs DEGIRO's separate per-trade export.

The CSV has two unnamed columns (the currency of each amount precedes it), so it is read
positionally rather than with ``DictReader``.
"""

from __future__ import annotations

import csv
from collections.abc import Iterator
from datetime import datetime

from finance.models import FLAG_OKAY, Amount, Posting, Transaction
from finance.parsers.base import Accounts, parse_decimal, resolve_account
from finance.text import normalize_text

INSTITUTION = "degiro"

#: Key under which the DEGIRO cash account is expected in ``own_accounts``.
ACCOUNT_KEY = "degiro"

COLUMNS = 12


def parse_from_io(handle, accounts: Accounts, *, source_file: str = "") -> Iterator[Transaction]:
    own = resolve_account(accounts, ACCOUNT_KEY, source=source_file or INSTITUTION)

    for row in csv.reader(handle):
        if len(row) != COLUMNS:
            continue

        date, time, _value_date, product, isin, description, _fx, currency, amount, *rest = row
        order_id = rest[-1] if rest else ""

        if date == "Datum" or not date or not time or not amount:
            continue

        meta: dict[str, object] = {"institution": INSTITUTION}
        if source_file:
            meta["source_file"] = source_file
        if isin:
            meta["isin"] = isin
        if product:
            meta["product"] = normalize_text(product)
        if order_id:
            meta["order_id"] = order_id

        narration = normalize_text(description)
        if product:
            narration = f"{normalize_text(product)} ({isin}): {narration}" if isin else narration

        yield Transaction(
            meta=meta,
            date=datetime.strptime(f"{date} {time}", "%d-%m-%Y %H:%M").date(),
            flag=FLAG_OKAY,
            payee="DEGIRO",
            narration=narration,
            tags=frozenset(),
            links=frozenset(),
            postings=(
                Posting(account=own, units=Amount(parse_decimal(amount), currency or "EUR")),
                Posting(account=None, units=None),
            ),
        )


def parse_file(path: str, accounts: Accounts) -> Iterator[Transaction]:
    with open(path, encoding="utf-8") as handle:
        yield from parse_from_io(handle, accounts, source_file=path)
