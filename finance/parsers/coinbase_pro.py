"""Coinbase Pro / GDAX account export.

An impeccable export: every fiat row replays exactly against its own running ``balance`` column,
which is what the closing assertion rests on, and the account closes to a fraction of a cent.

Trades are booked as lots, the same as any other broker here. Each trade is two rows sharing a
``trade id`` — the crypto leg and the fiat leg — plus a fee row on the same id when there is one.
That shared id is not a row identifier, which matters more than it sounds: keyed on it alone,
two rows look like one transaction and deduplication drops one of each pair.

**Crypto arriving from elsewhere has no recorded cost, and is booked at zero.** Units moved both
ways between this account and the same provider's retail wallet, and the retail export is a
partial history — it is missing its own trades, so nothing there says what those units cost. The
consequence is worth stating plainly: proceeds of eventually selling them are recognised here as
gain in full, while the money that bought them sits as a debit in the pool account the transfers
point at. The two halves are in different accounts and the sum across them is right, but neither
figure alone is the economic one.

Every position in this account nets to zero — it was emptied — so nothing above leaves a phantom
holding behind. A transfer *out* is different and needs no assumption: the lot's own cost moves
with it, absorbed by an elided posting, so value leaves at cost and no gain is invented.
"""

from __future__ import annotations

import csv
from collections import defaultdict
from collections.abc import Iterator
from datetime import date as Date
from datetime import datetime, timedelta
from decimal import Decimal

from finance.models import (
    EMPTY_COST,
    FLAG_OKAY,
    Amount,
    Balance,
    Cost,
    Directive,
    Open,
    Posting,
    Transaction,
)
from finance.parsers.base import Accounts, parse_decimal, resolve_account
from finance.text import normalize_text

INSTITUTION = "coinbase_pro"

#: Key under which the Coinbase Pro fiat account is expected in ``own_accounts``.
ACCOUNT_KEY = "coinbase_pro"

#: Optional ``own_accounts`` key for where realised gains land. Necessarily a parser choice: a
#: disposal's gain is derived from the lots, so there is no row for a rule to claim.
GAINS_ACCOUNT_KEY = "coinbase_pro_gains"
DEFAULT_GAINS_ACCOUNT = "Income:CapitalGains:Crypto"

POSITIONS_LEAF = "Positions"
BOOKING = "FIFO"

FIAT = {"EUR", "USD", "GBP"}
STAMP = "%Y-%m-%dT%H:%M:%S.%fZ"

#: Cost assigned to units arriving from somewhere that does not record what they cost. See the
#: module docstring for what that does to the gain.
UNKNOWN_COST = Decimal(0)
UNKNOWN_COST_CURRENCY = "EUR"


def _has_fiat_leg(trades: dict[str, list[dict]], row: dict) -> bool:
    """Whether this row's trade has a fiat leg, which is the leg that emits it."""
    legs = trades.get((row.get("trade id") or "").strip()) or []
    return any((r["amount/balance unit"] or "") in FIAT for r in legs)


def _stamp(text: str) -> Date | None:
    try:
        return datetime.strptime((text or "").strip(), STAMP).date()
    except ValueError:
        return None


def _meta(row: dict, source_file: str) -> dict[str, object]:
    meta: dict[str, object] = {"institution": INSTITUTION, "type": (row.get("type") or "").strip()}
    if source_file:
        meta["source_file"] = source_file
    if row.get("portfolio"):
        meta["portfolio"] = normalize_text(row["portfolio"])
    # A trade id belongs to the trade, not the row: the crypto leg, the fiat leg and the fee all
    # carry it. Adding what distinguishes them keeps the identifier a row identifier — without
    # that, deduplication treats a trade and its fee as one transaction and drops the fee.
    if row.get("transfer id"):
        meta["transaction_id"] = row["transfer id"].strip()
    elif row.get("trade id"):
        unit = (row.get("amount/balance unit") or "").strip()
        meta["transaction_id"] = f"{row['trade id'].strip()}:{meta['type']}:{unit}"
    return meta


def parse_from_io(handle, accounts: Accounts, *, source_file: str = "") -> Iterator[Directive]:
    cash = resolve_account(accounts, ACCOUNT_KEY, source=source_file or INSTITUTION)
    positions = f"{cash.rpartition(':')[0]}:{POSITIONS_LEAF}"
    gains = accounts.get(GAINS_ACCOUNT_KEY.upper(), DEFAULT_GAINS_ACCOUNT)

    rows = [r for r in csv.DictReader(handle) if r.get("amount") and r.get("time")]

    # Both legs of a trade are needed at once to book a lot, so the matches are grouped first.
    trades: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        if (row.get("type") or "").strip() == "match" and row.get("trade id"):
            trades[row["trade id"].strip()].append(row)

    closing: dict[str, tuple[Date, Decimal]] = {}
    booked = False
    first: Date | None = None

    for sequence, row in enumerate(rows):
        kind = (row.get("type") or "").strip()
        currency = (row.get("amount/balance unit") or "").strip()
        date = _stamp(row.get("time") or "")
        if date is None:
            continue
        amount = parse_decimal(row["amount"])
        first = min(first or date, date)

        if currency in FIAT:
            stated = row.get("balance")
            if stated is not None and stated.strip():
                seen = closing.get(currency)
                if seen is None or date >= seen[0]:
                    closing[currency] = (date, parse_decimal(stated))

        meta = _meta(row, source_file)
        meta["sequence"] = sequence

        if currency in FIAT and kind == "match":
            # The fiat leg is the one that emits the trade, so exactly one row does and it is
            # always the same one. Its crypto partner is skipped below.
            legs = trades.get((row.get("trade id") or "").strip()) or []
            crypto = next((r for r in legs if (r["amount/balance unit"] or "") not in FIAT), None)
            if crypto is None:
                postings = [
                    Posting(account=cash, units=Amount(amount, currency)),
                    Posting(account=None, units=None),
                ]
            else:
                quantity = parse_decimal(crypto["amount"])
                commodity = (crypto["amount/balance unit"] or "").strip()
                meta["commodity"] = commodity
                if quantity > 0:
                    postings = [
                        Posting(
                            account=positions,
                            units=Amount(quantity, commodity),
                            # A total, so a price to sixteen places cannot fail to multiply back
                            # to the cents that actually left the account.
                            cost=Cost(abs(amount), currency, None, None, total=True),
                        ),
                        Posting(account=cash, units=Amount(amount, currency)),
                    ]
                else:
                    postings = [
                        Posting(
                            account=positions,
                            units=Amount(quantity, commodity),
                            cost=EMPTY_COST,
                        ),
                        Posting(account=cash, units=Amount(amount, currency)),
                        Posting(account=gains, units=None),
                    ]
                booked = True
        elif currency not in FIAT:
            # Every crypto row that is not half of a fiat-priced trade is handled the same way,
            # which keeps four different rows from needing four different guesses:
            #
            #   * a transfer in or out, to the provider's retail wallet;
            #   * one side of a crypto-for-crypto trade, of which there are a couple of dozen
            #     here and which carry no fiat value at all;
            #   * a fee charged in the coin itself.
            #
            # Leaving means the lot's own cost leaves with it, absorbed by the elided posting, so
            # value moves at cost and no gain is invented from a price nobody recorded. Arriving
            # means zero, because nothing on this side says what it cost — see the module
            # docstring for what that does to the eventual gain.
            if (row.get("trade id") or "").strip() and _has_fiat_leg(trades, row):
                continue  # the fiat leg already emitted this trade
            if amount > 0:
                postings = [
                    Posting(
                        account=positions,
                        units=Amount(amount, currency),
                        cost=Cost(UNKNOWN_COST, UNKNOWN_COST_CURRENCY, None, None, total=True),
                    ),
                    Posting(account=None, units=None),
                ]
            else:
                postings = [
                    Posting(account=positions, units=Amount(amount, currency), cost=EMPTY_COST),
                    Posting(account=None, units=None),
                ]
            booked = True
        else:
            postings = [
                Posting(account=cash, units=Amount(amount, currency)),
                Posting(account=None, units=None),
            ]

        yield Transaction(
            meta=meta,
            date=date,
            flag=FLAG_OKAY,
            payee="Coinbase Pro",
            narration=kind,
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

    # Stated per row by the exchange and independent of the amounts, so this checks that every
    # row was read. It catches a dropped fee outright.
    for currency, (date, balance) in sorted(closing.items()):
        yield Balance(
            meta={"institution": INSTITUTION, "source_file": source_file},
            date=date + timedelta(days=1),
            account=cash,
            amount=Amount(balance, currency),
        )


def parse_file(path: str, accounts: Accounts) -> Iterator[Directive]:
    with open(path, encoding="utf-8") as handle:
        yield from parse_from_io(handle, accounts, source_file=path)
