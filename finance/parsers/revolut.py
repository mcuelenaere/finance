"""Revolut account statement CSV.

One file per currency pocket, with no account identifier anywhere in it — only a ``Currency``
column — so the account comes from a fixed ``own_accounts`` key and the currency separates the
pockets inside it.

**The fee is not included in ``Amount``.** A transfer of 1000 with a fee of 3.51 moves 1003.51
out of the account, and the statement's own running ``Balance`` column proves it: every row
reconciles as ``previous + Amount - Fee``, and none reconcile without the fee. Booking ``Amount``
alone would leave the fee unrecorded and the balance permanently adrift.

That running balance is the reason this parser can assert anything at all. It is stated per row
by the bank, independently of the amounts, so the closing figure checks that every row was read.
"""

from __future__ import annotations

import csv
from collections.abc import Iterator
from datetime import date as Date
from datetime import datetime, timedelta
from decimal import Decimal

from finance.models import (
    FLAG_OKAY,
    Amount,
    Balance,
    Directive,
    Posting,
    Transaction,
)
from finance.parsers.base import Accounts, resolve_account
from finance.text import normalize_text

INSTITUTION = "revolut"

#: ``own_accounts`` key for the account. Fixed, because the export never names the account.
ACCOUNT_KEY = "revolut"

#: Where transfer fees land, unless ``own_accounts`` overrides it. A parser policy choice: the
#: fee is a column rather than a row, so no rule could ever claim it.
FEE_ACCOUNT_KEY = "revolut_fee"
DEFAULT_FEE_ACCOUNT = "Expenses:Financial:Fees"

#: Only settled rows move money. Anything else — pending, reverted, declined — has not happened,
#: and booking it would put money in the ledger that the bank never moved.
SETTLED = "COMPLETED"

#: Revolut writes seconds but not always zero-padded hours, so the format is parsed leniently.
STAMP_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M")


def _stamp(text: str) -> Date | None:
    text = (text or "").strip()
    for fmt in STAMP_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _decimal(text: str | None) -> Decimal:
    text = (text or "").strip()
    return Decimal(text) if text else Decimal(0)


def parse_from_io(handle, accounts: Accounts, *, source_file: str = "") -> Iterator[Directive]:
    own = resolve_account(accounts, ACCOUNT_KEY, source=source_file or INSTITUTION)
    fee_account = accounts.get(FEE_ACCOUNT_KEY.upper(), DEFAULT_FEE_ACCOUNT)

    #: The statement's own closing balance for each currency, and when it applies.
    closing: dict[str, tuple[Date, Decimal]] = {}

    for row in csv.DictReader(handle):
        if (row.get("State") or "").strip().upper() != SETTLED:
            continue
        # Completed, not started: the balance column moves when the money does, and a SWIFT
        # transfer here took two days to settle.
        date = _stamp(row.get("Completed Date") or "") or _stamp(row.get("Started Date") or "")
        if date is None:
            continue

        currency = (row.get("Currency") or "EUR").strip() or "EUR"
        amount = _decimal(row.get("Amount"))
        fee = _decimal(row.get("Fee"))
        description = normalize_text(row.get("Description") or "")

        stated = row.get("Balance")
        if stated is not None and stated.strip():
            seen = closing.get(currency)
            if seen is None or date >= seen[0]:
                closing[currency] = (date, _decimal(stated))

        meta: dict[str, object] = {
            "institution": INSTITUTION,
            "type": (row.get("Type") or "").strip(),
        }
        if source_file:
            meta["source_file"] = source_file
        if row.get("Product"):
            meta["product"] = normalize_text(row["Product"])
        if fee:
            meta["fee"] = str(fee)

        postings = [Posting(account=own, units=Amount(amount - fee, currency))]
        if fee:
            postings.append(Posting(account=fee_account, units=Amount(fee, currency)))
        # The counter-posting is left for the rules engine; Beancount balances the elision, so
        # it absorbs the amount before the fee rather than after it.
        postings.append(Posting(account=None, units=None))

        yield Transaction(
            meta=meta,
            date=date,
            flag=FLAG_OKAY,
            payee=None,
            narration=description or (row.get("Type") or "").strip(),
            tags=frozenset(),
            links=frozenset(),
            postings=tuple(postings),
        )

    # Stated by the bank per row, independently of the amounts, so this checks that every row
    # was read rather than restating them.
    for currency, (date, balance) in sorted(closing.items()):
        yield Balance(
            meta={"institution": INSTITUTION, "source_file": source_file},
            date=date + timedelta(days=1),
            account=own,
            amount=Amount(balance, currency),
        )


def parse_file(path: str, accounts: Accounts) -> Iterator[Directive]:
    with open(path, encoding="utf-8-sig") as handle:
        yield from parse_from_io(handle, accounts, source_file=path)
