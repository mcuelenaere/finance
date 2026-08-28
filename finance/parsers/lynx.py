"""Lynx transaction history, an Interactive Brokers flex report.

The file is several CSVs stacked in one: every row begins with its section name and whether it
is that section's header or a data row. Only ``Transaction History`` carries transactions;
``Statement`` and ``Summary`` are key/value metadata, and ``Summary`` is worth reading because
its ``Ending Cash`` gives a balance to assert against.

Two things are not in the file and have to be derived.

**The transaction tax.** ``Net Amount`` is not ``Gross Amount`` plus ``Commission``: on every
buy there is a further 0.12% of gross, which is the Belgian tax on stock-exchange transactions.
It has no column, so folding it into the cost silently would both overstate the basis and hide
a real expense. It is computed as the residual and booked on its own.

**Float noise.** Amounts are written as floats and arrive already damaged — ``-869.5862599999999``
for what is plainly ``-869.58626``. They are quantized on the way in, or the ledger fills with
implausible precision and the balance assertion misses by a billionth.
"""

from __future__ import annotations

import csv
import re
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
from finance.text import normalize_text

INSTITUTION = "lynx"

#: ``own_accounts`` key for the cash account.
ACCOUNT_KEY = "lynx"

#: Optional ``own_accounts`` keys overriding where costs of trading land. Both are parser policy
#: choices because neither is a row of its own: the commission is a column and the tax is a
#: residual, so no rule could ever claim either.
COMMISSION_ACCOUNT_KEY = "lynx_commission"
TAX_ACCOUNT_KEY = "lynx_tax"
DEFAULT_COMMISSION_ACCOUNT = "Expenses:Financial:Fees"
DEFAULT_TAX_ACCOUNT = "Expenses:Taxes"

#: Where positions are held, and how disposals pick their lots.
POSITIONS_LEAF = "Positions"
BOOKING = "FIFO"

TRANSACTIONS = "Transaction History"
SUMMARY = "Summary"

#: The export writes an absent value as a bare dash.
BLANK = "-"

#: Amounts carry float damage; eight places is past any real precision and removes it.
PRECISION = Decimal("0.00000001")

#: Only purchases are booked as positions. This export contains no disposals, so the shape of
#: one — whether the tax is deducted from proceeds the same way, and how the lot is identified —
#: cannot be checked against anything. A sale is left as cash and shows up in review rather than
#: being booked on a guess.
ACQUISITION = "Buy"


def _decimal(value: str | None) -> Decimal | None:
    text = (value or "").strip()
    if not text or text == BLANK:
        return None
    value = Decimal(text).quantize(PRECISION).normalize()
    # normalize() turns 1000 into 1E+3; re-quantize whole numbers so no exponent survives.
    return value.quantize(Decimal(1)) if value == value.to_integral_value() else value


def commodity_of(symbol: str) -> str | None:
    """A Beancount commodity for a ticker.

    Tickers occasionally carry a lowercase exchange suffix (``MEUSl``), which is not a valid
    commodity, so they are upper-cased and stripped of anything else.
    """
    name = re.sub(r"[^A-Za-z0-9]", "", symbol).upper()
    return name if name[:1].isalpha() else None


def _sections(handle) -> tuple[dict[str, str], list[dict[str, str]]]:
    """Split the stacked CSV into the Summary key/values and the transaction rows."""
    summary: dict[str, str] = {}
    columns: list[str] = []
    transactions: list[dict[str, str]] = []

    for row in csv.reader(handle):
        if len(row) < 3:
            continue
        section, kind = row[0], row[1]
        if section == SUMMARY and kind == "Data":
            summary[row[2].strip()] = row[3].strip() if len(row) > 3 else ""
        elif section == TRANSACTIONS and kind == "Header":
            columns = [name.strip() for name in row[2:]]
        elif section == TRANSACTIONS and kind == "Data" and columns:
            transactions.append(dict(zip(columns, row[2:], strict=False)))

    return summary, transactions


def parse_file(path: str, accounts: Accounts) -> Iterator[Directive]:
    cash = resolve_account(accounts, ACCOUNT_KEY, source=path)
    positions = f"{cash.rpartition(':')[0]}:{POSITIONS_LEAF}"
    commission_account = accounts.get(COMMISSION_ACCOUNT_KEY.upper(), DEFAULT_COMMISSION_ACCOUNT)
    tax_account = accounts.get(TAX_ACCOUNT_KEY.upper(), DEFAULT_TAX_ACCOUNT)

    with open(path, encoding="utf-8-sig") as handle:
        summary, rows = _sections(handle)

    base = summary.get("Base Currency", "USD") or "USD"
    booked_positions = False
    last_date: Date | None = None

    for row in rows:
        net = _decimal(row.get("Net Amount"))
        raw_date = (row.get("Date") or "").strip()
        if net is None or not raw_date:
            continue
        date = Date.fromisoformat(raw_date)
        last_date = max(last_date or date, date)

        kind = (row.get("Transaction Type") or "").strip()
        description = normalize_text(row.get("Description") or "")
        symbol = (row.get("Symbol") or "").strip()
        quantity = _decimal(row.get("Quantity"))
        gross = _decimal(row.get("Gross Amount"))
        commission = _decimal(row.get("Commission"))
        price_currency = (row.get("Price Currency") or "").strip()

        meta: dict[str, object] = {
            "institution": INSTITUTION,
            "source_file": path,
            "type": kind,
        }
        if symbol and symbol != BLANK:
            meta["symbol"] = symbol
        if row.get("Price") and row["Price"].strip() != BLANK:
            meta["price"] = row["Price"].strip()

        commodity = commodity_of(symbol) if symbol and symbol != BLANK else None

        # A position is only booked when the trade is priced in the account's own currency;
        # anything else needs an explicit conversion and is left as cash.
        if kind == ACQUISITION and commodity and quantity and gross and price_currency == base:
            # The cash paid explains itself: cost, then commission, then whatever is left,
            # which is the transaction tax. Deriving the tax as the residual rather than
            # assuming a rate means the postings balance exactly whatever the rate is.
            tax = abs(net) - abs(gross) - abs(commission or 0)
            postings = [
                Posting(
                    account=positions,
                    units=Amount(abs(quantity), commodity),
                    # A total, so a rounded unit price cannot fail to multiply back.
                    cost=Cost(abs(gross), base, None, None, total=True),
                ),
                Posting(account=cash, units=Amount(net, base)),
            ]
            if commission:
                postings.append(
                    Posting(account=commission_account, units=Amount(abs(commission), base))
                )
            if tax:
                postings.append(Posting(account=tax_account, units=Amount(tax, base)))
            booked_positions = True
        else:
            postings = [
                Posting(account=cash, units=Amount(net, base)),
                Posting(account=None, units=None),
            ]

        yield Transaction(
            meta=meta,
            date=date,
            flag=FLAG_OKAY,
            payee="Lynx",
            narration=description or kind,
            tags=frozenset(),
            links=frozenset(),
            postings=tuple(postings),
        )

    if booked_positions and last_date:
        yield Open(
            meta={"institution": INSTITUTION},
            date=min(Date.fromisoformat(r["Date"].strip()) for r in rows if r.get("Date")),
            account=positions,
            currencies=(),
            booking=BOOKING,
        )

    ending = _decimal(summary.get("Ending Cash"))
    if ending is not None and last_date is not None:
        # The Summary states the closing cash, and the report starts from zero, so this is a
        # genuine check that every row was read rather than a restatement of them.
        yield Balance(
            meta={"institution": INSTITUTION, "source_file": path},
            date=last_date + timedelta(days=1),
            account=cash,
            amount=Amount(ending, base),
        )
