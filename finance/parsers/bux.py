"""BUX transaction history CSV.

The friendliest export in this repo: every row states its cash effect in one column and the
resulting balance in another, and the categories are structured rather than free text. Two
things still need care.

**The rows are not in balance order.** They are sorted by timestamp, and several share one to
the microsecond — a trade and its fee, or a trade and its tax — where the running balance shows
the trade landing first. So the running balance cannot be replayed row by row, and only the
closing figure is asserted. That is enough: the amounts sum to exactly the closing balance, so
one assertion checks every row.

**The transaction tax is a row here.** Lynx has to derive the same Belgian tax as a residual
because its export omits it; BUX states it outright, which is why this parser never computes it.

One thing the export does not say: a holding was bought under one ISIN and sold under another
after the issuer re-registered. There is no row for the change, so it cannot be inferred from
the file — the disposal simply names a commodity that was never acquired, and the resulting
error is the right outcome. It is reconciled by hand; see ``manual.beancount``.
"""

from __future__ import annotations

import csv
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
from finance.parsers.base import DEFAULT_GAINS_ACCOUNT, Accounts, resolve_account
from finance.text import normalize_text

INSTITUTION = "bux"

#: ``own_accounts`` key for the cash account. The export never names the account.
ACCOUNT_KEY = "bux"

#: Optional ``own_accounts`` keys overriding where the structured categories land. Realised gains
#: have no row of their own — Beancount derives them from the lots — so that one is necessarily
#: a parser choice; the rest are parser choices because the export classifies them itself, and
#: re-deriving that from text in a rule would only lose information.
GAINS_ACCOUNT_KEY = "bux_gains"
CATEGORY_KEYS = {
    "fees": ("bux_fees", "Expenses:Financial:Fees"),
    "tax": ("bux_tax", "Expenses:Taxes"),
    "interest": ("bux_interest", "Income:Interest"),
    "dividends": ("bux_dividends", "Income:Dividends:Broker"),
}

#: Categories left for the rules engine. Deposits and withdrawals have a bank leg that is
#: exported too, so they must route through the clearing account rather than be guessed here;
#: `others` is a grab-bag and belongs in review.
UNRESOLVED_CATEGORIES = ("deposits", "withdrawals", "others")

#: Optional ``own_accounts`` key for where units are held. Defaults to a sibling of the cash
#: account, which is only right when the two live in one branch — so a ledger that files cash
#: and holdings under different roots must say where this one goes.
POSITIONS_ACCOUNT_KEY = "bux_positions"
POSITIONS_LEAF = "Positions"
BOOKING = "FIFO"

BUY = "Buy Trade"
SELL = "Sell Trade"

STAMP = "%Y-%m-%d %H:%M:%S.%f"


def _decimal(text: str | None) -> Decimal | None:
    text = (text or "").strip()
    return Decimal(text) if text else None


def _stamp(text: str) -> datetime | None:
    try:
        return datetime.strptime((text or "").strip(), STAMP)
    except ValueError:
        return None


def parse_from_io(handle, accounts: Accounts, *, source_file: str = "") -> Iterator[Directive]:
    cash = resolve_account(accounts, ACCOUNT_KEY, source=source_file or INSTITUTION)
    positions = accounts.get(
        POSITIONS_ACCOUNT_KEY.upper(), f"{cash.rpartition(':')[0]}:{POSITIONS_LEAF}"
    )
    gains = accounts.get(GAINS_ACCOUNT_KEY.upper(), DEFAULT_GAINS_ACCOUNT)
    category_accounts = {
        category: accounts.get(key.upper(), default)
        for category, (key, default) in CATEGORY_KEYS.items()
    }

    booked = False
    first: Date | None = None
    closing: tuple[datetime, Decimal] | None = None

    for sequence, row in enumerate(csv.DictReader(handle)):
        stamp = _stamp(row.get("Transaction Time (CET)") or "")
        amount = _decimal(row.get("Transaction Amount"))
        if stamp is None or amount is None:
            continue
        date = stamp.date()
        first = min(first or date, date)

        currency = (row.get("Transaction Currency") or "EUR").strip() or "EUR"
        category = (row.get("Transaction Category") or "").strip()
        kind = (row.get("Transaction Type") or "").strip()
        isin = (row.get("Asset Id") or "").strip().upper()
        name = normalize_text(row.get("Asset Name") or "")
        quantity = _decimal(row.get("Trade Quantity"))
        price = _decimal(row.get("Trade Price"))

        stated = _decimal(row.get("Cash Balance Amount"))
        if stated is not None and (closing is None or stamp >= closing[0]):
            closing = (stamp, stated)

        # The export sells holdings it bought the same day, so the file's order has to survive
        # into the ledger: rendered the other way round there is no lot for the disposal to
        # reduce. See `finance.render.SEQUENCE`.
        meta: dict[str, object] = {
            "institution": INSTITUTION,
            "category": category,
            "sequence": sequence,
            "type": kind,
        }
        if source_file:
            meta["source_file"] = source_file
        if isin:
            meta["isin"] = isin
        if name:
            meta["product"] = name
        if price is not None:
            meta["unit_price"] = str(price)

        if category == "trades" and isin and quantity:
            if kind == BUY:
                postings = [
                    Posting(
                        account=positions,
                        units=Amount(quantity, isin),
                        # A total, so a price carried to eight places cannot fail to multiply
                        # back to the cents the statement says left the account.
                        cost=Cost(abs(amount), currency, None, None, total=True),
                    ),
                    Posting(account=cash, units=Amount(amount, currency)),
                ]
            else:
                # A disposal names no lot, so the booking method picks one and the elided gain
                # posting absorbs whatever the difference turns out to be.
                postings = [
                    Posting(
                        account=positions,
                        units=Amount(-quantity, isin),
                        cost=EMPTY_COST,
                    ),
                    Posting(account=cash, units=Amount(amount, currency)),
                    Posting(account=gains, units=None),
                ]
            booked = True
        elif category in category_accounts:
            postings = [
                Posting(account=cash, units=Amount(amount, currency)),
                Posting(account=category_accounts[category], units=Amount(-amount, currency)),
            ]
        else:
            postings = [
                Posting(account=cash, units=Amount(amount, currency)),
                Posting(account=None, units=None),
            ]

        yield Transaction(
            meta=meta,
            date=date,
            flag=FLAG_OKAY,
            payee="BUX",
            narration=f"{kind}{f' {name}' if name else ''}",
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

    if closing is not None:
        yield Balance(
            meta={"institution": INSTITUTION, "source_file": source_file},
            date=closing[0].date() + timedelta(days=1),
            account=cash,
            amount=Amount(closing[1], "EUR"),
        )


def parse_file(path: str, accounts: Accounts) -> Iterator[Directive]:
    with open(path, encoding="utf-8-sig") as handle:
        yield from parse_from_io(handle, accounts, source_file=path)
