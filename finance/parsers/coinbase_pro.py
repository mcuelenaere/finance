"""Coinbase Pro / GDAX account export.

An impeccable export: every fiat row replays exactly against its own running ``balance`` column,
which is what the closing assertion below rests on, and the account closes to a fraction of a
cent.

**Only the fiat legs are emitted, and that is a decision rather than a gap.** The crypto did not
stay inside this account: units were moved to and from the same provider's retail wallet, whose
export is a partial history — it is missing the sales, so its own holdings sum *negative*.
Tracking units from this side alone would therefore show crypto that has already been sold, and
a phantom holding overstates net worth exactly as badly as a missing one understates it. The
fiat slice, by contrast, closes.

So a trade's fiat leg is not income: it is value moving between this cash account and the
provider's wider pool, which a rule points at. The realised result comes out of that pool.
"""

from __future__ import annotations

import csv
from collections.abc import Iterator
from datetime import date as Date
from datetime import datetime, timedelta
from decimal import Decimal

from finance.models import FLAG_OKAY, Amount, Balance, Directive, Posting, Transaction
from finance.parsers.base import Accounts, parse_decimal, resolve_account
from finance.text import normalize_text

INSTITUTION = "coinbase_pro"

#: Key under which the Coinbase Pro fiat account is expected in ``own_accounts``.
ACCOUNT_KEY = "coinbase_pro"

FIAT = {"EUR", "USD", "GBP"}


def parse_from_io(handle, accounts: Accounts, *, source_file: str = "") -> Iterator[Directive]:
    own = resolve_account(accounts, ACCOUNT_KEY, source=source_file or INSTITUTION)

    #: The export's own closing balance per fiat currency, and when it applies.
    closing: dict[str, tuple[Date, Decimal]] = {}

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
        # A trade id is shared by every row of the trade — the crypto leg, the fiat leg and the
        # fee — so it does not identify a row. Using it alone made two rows look like one
        # transaction, and deduplication would have dropped one of each pair: 81 fee rows here.
        # The type and unit are what separate them, and both are intrinsic to the row, so the
        # id stays stable across re-exports.
        if row.get("transfer id"):
            meta["transaction_id"] = row["transfer id"].strip()
        elif row.get("trade id"):
            kind = (row.get("type") or "").strip()
            meta["transaction_id"] = f"{row['trade id'].strip()}:{kind}:{currency}"

        date = datetime.strptime(row["time"], "%Y-%m-%dT%H:%M:%S.%fZ").date()
        stated = row.get("balance")
        if stated is not None and stated.strip():
            seen = closing.get(currency)
            if seen is None or date >= seen[0]:
                closing[currency] = (date, parse_decimal(stated))

        yield Transaction(
            meta=meta,
            date=date,
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

    # Stated per row by the exchange and independent of the amounts, so this checks that every
    # row was read. It caught the deduplication problem noted above before it could matter.
    for currency, (date, balance) in sorted(closing.items()):
        yield Balance(
            meta={"institution": INSTITUTION, "source_file": source_file},
            date=date + timedelta(days=1),
            account=own,
            amount=Amount(balance, currency),
        )


def parse_file(path: str, accounts: Accounts) -> Iterator[Directive]:
    with open(path, encoding="utf-8") as handle:
        yield from parse_from_io(handle, accounts, source_file=path)
