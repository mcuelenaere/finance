"""Coinbase consumer account export.

A different product and a different file from the Coinbase Pro / GDAX export that
:mod:`finance.parsers.coinbase_pro` reads — same provider, two accounts, and the two move money
and coins between each other constantly. Both exports record those transfers, from their own
side, which is what lets them pair.

The fiat side of this file reconciles three ways and is not in doubt: its bank deposits equal
what the bank statements show leaving, its transfers to and from the exchange equal what the
exchange's own export shows arriving, and the proceeds of its sales exactly fill the gap between
its recorded euro movements — because the euro leg of a sale is not a row of its own, it is a
column on the crypto row.

Crypto is looser, and deliberately handled without assuming the two exports agree. They do not
quite: one holds a coin that appeared from a fork, and a few units crossed without both sides
recording it. So positions are kept per account rather than pooled, and every coin entering or
leaving is valued against a wallet account — the crypto held outside these two accounts, in
hardware wallets and in flight between them. Each account's own units then close on its own
data, which is the property that makes the build fail when a row is misread rather than when two
exports disagree about history.

Coins **arriving** are valued at the euro figure the export states for them; coins **leaving**
take their own lot cost with them, absorbed by an elided posting, so no gain is invented from a
price nobody recorded.
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Iterator
from datetime import date as Date
from datetime import datetime
from decimal import Decimal

from finance.models import (
    EMPTY_COST,
    FLAG_OKAY,
    Amount,
    Cost,
    Directive,
    Open,
    Posting,
    Transaction,
)
from finance.parsers.base import Accounts, resolve_account
from finance.text import normalize_text

INSTITUTION = "coinbase"

#: ``own_accounts`` key for the cash account. The export never names the account.
ACCOUNT_KEY = "coinbase"

#: Optional ``own_accounts`` key for where units are held. Defaults to a sibling of the cash
#: account; kept separate from the exchange's positions on purpose — see the module docstring.
POSITIONS_ACCOUNT_KEY = "coinbase_positions"
POSITIONS_LEAF = "Positions"
BOOKING = "FIFO"

#: Where realised gains land. Necessarily a parser choice: Beancount derives a disposal's gain
#: from the lots, so there is no row for a rule to claim.
GAINS_ACCOUNT_KEY = "coinbase_gains"
DEFAULT_GAINS_ACCOUNT = "Income:CapitalGains:Crypto"

FIAT = {"EUR", "USD", "GBP"}

#: The preamble is a title and an account line before the real header.
HEADER_PREFIX = "ID,Timestamp"

#: A sale states its euro proceeds in this column rather than as a row of its own.
TOTAL_COLUMN = "Total (inclusive of fees and/or spread)"

SELL_TYPES = ("Advanced Trade Sell",)

STAMP_FORMATS = ("%Y-%m-%d %H:%M:%S UTC", "%Y-%m-%d %H:%M:%S")


def _stamp(text: str) -> Date | None:
    text = (text or "").strip()
    for fmt in STAMP_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def money(text: str | None) -> Decimal:
    """A euro figure as the export writes it: ``€5.00``, ``-€3879.00192``, or a bare number.

    The sign sits *outside* the currency symbol, so stripping non-digits before reading it turns
    every debit into a credit.
    """
    text = (text or "").strip()
    if not text:
        return Decimal(0)
    negative = text.startswith("-")
    digits = re.sub(r"[^\d.]", "", text)
    if not digits:
        return Decimal(0)
    return -Decimal(digits) if negative else Decimal(digits)


def _rows(handle) -> list[dict[str, str]]:
    """Skip the preamble and read the real table."""
    lines = handle.read().splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith(HEADER_PREFIX)), None)
    if start is None:
        return []
    reader = csv.DictReader(io.StringIO("\n".join(lines[start:])))
    return [row for row in reader if (row.get("ID") or "").strip()]


def parse_from_io(handle, accounts: Accounts, *, source_file: str = "") -> Iterator[Directive]:
    cash = resolve_account(accounts, ACCOUNT_KEY, source=source_file or INSTITUTION)
    positions = accounts.get(
        POSITIONS_ACCOUNT_KEY.upper(), f"{cash.rpartition(':')[0]}:{POSITIONS_LEAF}"
    )
    gains = accounts.get(GAINS_ACCOUNT_KEY.upper(), DEFAULT_GAINS_ACCOUNT)

    booked = False
    first: Date | None = None

    # Oldest first, so the build cannot depend on the order the export happened to use — and
    # within each DAY, arrivals before departures.
    #
    # That second part is load-bearing. The export pairs a withdrawal from the exchange with a
    # send straight on to a wallet, and it does not always timestamp them in the order they
    # settled: on at least one day the send is stamped a second before the coin it sent arrives.
    # Rendered that way there is no lot to reduce and the build stops. Sorting arrivals first is
    # safe whatever the true order was, because a day's net is unchanged either way and no
    # position can dip below zero inside it.
    def order(row: dict[str, str]) -> tuple:
        try:
            quantity = Decimal((row.get("Quantity Transacted") or "0").strip() or 0)
        except (ArithmeticError, ValueError):
            quantity = Decimal(0)
        stamp = (row.get("Timestamp") or "").strip()
        return (stamp[:10], quantity < 0, stamp, row.get("ID") or "")

    rows = sorted(_rows(handle), key=order)

    for sequence, row in enumerate(rows):
        date = _stamp(row.get("Timestamp") or "")
        quantity_text = (row.get("Quantity Transacted") or "").strip()
        if date is None or not quantity_text:
            continue
        first = min(first or date, date)

        asset = (row.get("Asset") or "").strip()
        kind = (row.get("Transaction Type") or "").strip()
        quantity = Decimal(quantity_text)
        currency = (row.get("Price Currency") or "EUR").strip() or "EUR"
        subtotal = money(row.get("Subtotal"))
        total = money(row.get(TOTAL_COLUMN))

        meta: dict[str, object] = {
            "institution": INSTITUTION,
            "type": kind,
            "sequence": sequence,
            "transaction_id": (row.get("ID") or "").strip(),
        }
        if source_file:
            meta["source_file"] = source_file
        if row.get("Notes"):
            meta["note"] = normalize_text(row["Notes"])

        if asset in FIAT:
            postings = [
                Posting(account=cash, units=Amount(quantity, asset)),
                Posting(account=None, units=None),
            ]
        elif kind in SELL_TYPES:
            # The euro proceeds are a column on this row, net of the fee and the spread.
            postings = [
                Posting(account=positions, units=Amount(quantity, asset), cost=EMPTY_COST),
                Posting(account=cash, units=Amount(abs(total), currency)),
                Posting(account=gains, units=None),
            ]
            booked = True
        elif quantity > 0:
            postings = [
                Posting(
                    account=positions,
                    units=Amount(quantity, asset),
                    # A total, so a unit price to eighteen places cannot fail to multiply back.
                    cost=Cost(abs(subtotal), currency, None, None, total=True),
                ),
                Posting(account=None, units=None),
            ]
            booked = True
        else:
            postings = [
                Posting(account=positions, units=Amount(quantity, asset), cost=EMPTY_COST),
                Posting(account=None, units=None),
            ]
            booked = True

        # `Exchange Withdrawal` is one phrase for two different transactions here — euro
        # arriving from the exchange, or coins arriving from it. Naming the asset on crypto rows
        # is what lets a rule route one to the clearing account and the other to a wallet.
        narration = kind if asset in FIAT else f"{kind} {asset}"

        yield Transaction(
            meta=meta,
            date=date,
            flag=FLAG_OKAY,
            payee="Coinbase",
            narration=narration,
            tags=frozenset(),
            links=frozenset(),
            postings=tuple(postings),
        )

    if booked and first:
        yield Open(
            meta={"institution": INSTITUTION},
            date=first,
            account=positions,
            currencies=(),
            booking=BOOKING,
        )


def parse_file(path: str, accounts: Accounts) -> Iterator[Directive]:
    with open(path, encoding="utf-8-sig") as handle:
        yield from parse_from_io(handle, accounts, source_file=path)
