"""Coinbase Pro / GDAX account export.

Only the fiat legs are emitted for now. The crypto legs are deliberately skipped: they need
commodity accounts and cost basis to be meaningful, which belongs with the investment work
rather than the cash slice. The model already supports it (``Posting.cost``) when that lands.
"""

from __future__ import annotations

import csv
from collections.abc import Iterator
from datetime import datetime

from finance.models import FLAG_OKAY, Amount, Posting, Transaction
from finance.parsers.base import Accounts, parse_decimal, resolve_account
from finance.text import normalize_text

INSTITUTION = "coinbase_pro"

#: Key under which the Coinbase Pro fiat account is expected in ``own_accounts``.
ACCOUNT_KEY = "coinbase_pro"

FIAT = {"EUR", "USD", "GBP"}


def parse_from_io(handle, accounts: Accounts, *, source_file: str = "") -> Iterator[Transaction]:
    own = resolve_account(accounts, ACCOUNT_KEY, source=source_file or INSTITUTION)

    for row in csv.DictReader(handle):
        currency = (row.get("amount/balance unit") or "").strip()
        if currency not in FIAT:
            continue
        if not row.get("amount") or not row.get("time"):
            continue

        meta: dict[str, object] = {"institution": INSTITUTION}
        if source_file:
            meta["source_file"] = source_file
        if row.get("portfolio"):
            meta["portfolio"] = normalize_text(row["portfolio"])
        if row.get("transfer id"):
            meta["transaction_id"] = row["transfer id"].strip()
        elif row.get("trade id"):
            meta["transaction_id"] = row["trade id"].strip()

        yield Transaction(
            meta=meta,
            date=datetime.strptime(row["time"], "%Y-%m-%dT%H:%M:%S.%fZ").date(),
            flag=FLAG_OKAY,
            payee="Coinbase Pro",
            narration=normalize_text(row.get("type") or ""),
            tags=frozenset(),
            links=frozenset(),
            postings=(
                Posting(account=own, units=Amount(parse_decimal(row["amount"]), currency)),
                Posting(account=None, units=None),
            ),
        )


def parse_file(path: str, accounts: Accounts) -> Iterator[Transaction]:
    with open(path, encoding="utf-8") as handle:
        yield from parse_from_io(handle, accounts, source_file=path)
