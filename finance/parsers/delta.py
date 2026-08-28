"""Belfius pension savings, reconstructed from a Delta portfolio export.

Belfius will not export this account, so the only record of it is what was copied into Delta
by hand. That makes this parser unlike the others: its input is not a bank's own file but a
transcription, and the balance assertions below are what keep it honest.

**Cost is ``amount * price``, never ``worth``.** Delta reports ``worth`` as the position's
*current* market value — a purchase of 4.741 units at 174.08 comes back as 985.37, which is
4.741 times today's price rather than the price paid. Using it would silently inflate the cost
basis by the whole gain. The field is dropped from the saved export for that reason; this note
is here in case a future export puts it back.

The export carries a positions block as well as transactions, gathered from a different Delta
endpoint. Those two agree only if every transaction was copied correctly, so the holdings are
emitted as balance assertions and ``bean-check`` does the reconciliation on every build.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import date as Date
from datetime import timedelta
from decimal import Decimal

from finance.models import (
    FLAG_OKAY,
    Amount,
    Balance,
    Cost,
    Directive,
    Open,
    Posting,
    Transaction,
)
from finance.parsers.base import Accounts, resolve_account

INSTITUTION = "delta"

#: ``own_accounts`` key used when an export does not name one. A file should say which account
#: it belongs to — see ``ACCOUNT_FIELD`` — because one tracker holds many portfolios and the
#: parser cannot tell them apart otherwise.
DEFAULT_ACCOUNT_KEY = "delta"

#: Where an export declares its own ``own_accounts`` key.
ACCOUNT_FIELD = "account"

#: Where the fund units are held, and how a future disposal picks its lots.
#: Where units are held. Derived from whichever account the export names — `<key>_positions`
#: in ``own_accounts`` — falling back to a sibling of the cash account. The fallback is only
#: right when cash and holdings live in one branch.
POSITIONS_KEY_SUFFIX = "_positions"
POSITIONS_LEAF = "Positions"
BOOKING = "FIFO"

#: Only purchases are booked. This export contains no disposals — the account has only ever
#: been paid into — so the shape of one cannot be checked against anything. Anything else is
#: left for the rules engine, where it shows up in review rather than being guessed at.
ACQUISITION = "BUY"


def _decimal(value: object) -> Decimal:
    return Decimal(str(value))


def parse_file(path: str, accounts: Accounts) -> Iterator[Directive]:
    with open(path, encoding="utf-8") as handle:
        export = json.load(handle)

    key = str(export.get(ACCOUNT_FIELD) or DEFAULT_ACCOUNT_KEY)
    cash = resolve_account(accounts, key, source=path)
    positions = accounts.get(
        f"{key}{POSITIONS_KEY_SUFFIX}".upper(),
        f"{cash.rpartition(':')[0]}:{POSITIONS_LEAF}",
    )

    rows = export.get("transactions") or []
    # Sorted explicitly: the export is newest-first, and the build must not depend on the order
    # a paginated API happened to return.
    rows = sorted(rows, key=lambda row: (str(row.get("datetime")), int(row.get("id") or 0)))

    booked = False
    first: Date | None = None
    last: Date | None = None

    for row in rows:
        stamp = str(row.get("datetime") or "")
        if not stamp:
            continue
        date = Date.fromisoformat(stamp[:10])
        first = min(first or date, date)
        last = max(last or date, date)

        commodity = str(row.get("base") or "").strip()
        currency = str(row.get("quote") or "EUR").strip() or "EUR"
        quantity = _decimal(row.get("amount") or 0)
        unit_price = _decimal((row.get("price") or {}).get("value") or 0)

        meta: dict[str, object] = {
            "institution": INSTITUTION,
            "source_file": path,
            "type": str(row.get("way") or ""),
            "unit_price": str(unit_price),
        }
        if row.get("id"):
            # Delta's own id, so re-importing the same export cannot double up.
            meta["transaction_id"] = str(row["id"])

        if str(row.get("way")) == ACQUISITION and commodity and quantity and unit_price:
            paid = (quantity * unit_price).quantize(Decimal("0.01"))
            postings = [
                Posting(
                    account=positions,
                    units=Amount(quantity, commodity),
                    # A total, so a unit price carried to five places cannot fail to
                    # multiply back to the cents actually paid.
                    cost=Cost(paid, currency, None, None, total=True),
                ),
                Posting(account=cash, units=Amount(-paid, currency)),
            ]
            booked = True
        else:
            postings = [
                Posting(account=cash, units=Amount(-_decimal(row.get("amount") or 0), currency)),
                Posting(account=None, units=None),
            ]

        yield Transaction(
            meta=meta,
            date=date,
            flag=FLAG_OKAY,
            payee="Belfius Pension",
            narration=f"{row.get('way')} {commodity}",
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

    # The holdings come from a different Delta endpoint than the transactions, so asserting
    # them checks the transcription rather than restating it: a fund unit mistyped anywhere in
    # the list makes bean-check fail.
    as_of = export.get("as_of")
    if as_of and last:
        stated = Date.fromisoformat(str(as_of))
        for position in export.get("positions") or []:
            holdings = position.get("holdings")
            commodity = str(position.get("shortCode") or "").strip()
            if holdings is None or not commodity:
                continue
            yield Balance(
                meta={"institution": INSTITUTION, "source_file": path},
                date=max(stated, last + timedelta(days=1)),
                account=positions,
                amount=Amount(_decimal(holdings), commodity),
            )
