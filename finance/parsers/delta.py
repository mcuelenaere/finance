"""A portfolio reconstructed from a Delta export, for accounts nobody else will export.

Two use it: Belfius pension savings, and the granted eToro equity. Neither institution exports
anything, so the only record is what was copied into Delta by hand. That makes this parser
unlike the others: its input is not a bank's own file but a transcription, and the balance
assertions below are what keep it honest.

**Two cost bases, because the two portfolios mean different things by "price".**

``paid`` (the default) is the ordinary case: the price is what changed hands, so a purchase
books a lot at cost and pays for it out of the cash leaf.

``granted`` is for units that were *awarded* rather than bought. Delta still records a price,
but it is a valuation and not consideration — nothing was paid for them, so they cost nothing,
and the money from selling them arrives somewhere else entirely. So a granted portfolio records
**units only**: no cash leaf, no proceeds, and no gain derived here. That is not a shortcut. The
proceeds of the eToro sales reach the ledger as payments from the employer that administers the
award, and those are already recognised as income where they land — booking a gain here as well
would count the same money twice. What was missing was never the income; it was the *position*.

**Do not read a granted export's prices as a cost basis.** Delta converts a price into euro at
the rate current when the export is fetched, not the rate on the day of the transaction — for a
dollar-quoted holding the euro figure therefore drifts with the exchange rate, which is the same
defect as ``worth`` below one step further in. Verified directly: the portfolio price came back
as 27,23 EUR against a live quote of 31,59 USD, a ratio of exactly the day's EUR/USD. The figure
is kept in metadata because it is what the export said, and used for nothing.

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

ACQUISITION = "BUY"
DISPOSAL = "SELL"

#: Where an export names the counterparty to show on its transactions. One tracker holds many
#: portfolios and they do not share a counterparty, so an export that does not say gets a
#: generic one rather than another portfolio's.
PAYEE_FIELD = "payee"
DEFAULT_PAYEE = "Delta"

#: Where an export says what its prices mean. See the module docstring.
COST_BASIS_FIELD = "cost_basis"
GRANTED = "granted"

#: Cost of units that were awarded rather than bought. Nothing was paid, so this is not an
#: approximation of a figure nobody recorded — it is the figure.
NO_COST = Decimal(0)


def _decimal(value: object) -> Decimal:
    return Decimal(str(value))


def parse_file(path: str, accounts: Accounts) -> Iterator[Directive]:
    with open(path, encoding="utf-8") as handle:
        export = json.load(handle)

    key = str(export.get(ACCOUNT_FIELD) or DEFAULT_ACCOUNT_KEY)
    payee = str(export.get(PAYEE_FIELD) or DEFAULT_PAYEE)
    granted = str(export.get(COST_BASIS_FIELD) or "").strip().lower() == GRANTED

    named = resolve_account(accounts, key, source=path)
    if granted:
        # No cash leaf at all: nothing was paid for these units and nothing is received here,
        # so the key names where the units live and there is no second account to derive.
        cash, positions = None, named
    else:
        cash = named
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

        way = str(row.get("way") or "")

        if granted and commodity and quantity and way in (ACQUISITION, DISPOSAL):
            # Units in or out, and nothing else. The counter-posting is written as an explicit
            # zero rather than elided: the transaction already balances without it, and
            # Beancount answers an elided posting it cannot give a number by deleting it — so
            # the account naming where the units came from would vanish from the one entry that
            # records it.
            arriving = way == ACQUISITION
            postings = [
                Posting(
                    account=positions,
                    units=Amount(quantity if arriving else -quantity, commodity),
                    cost=(
                        Cost(NO_COST, currency, None, None, total=True)
                        if arriving
                        # Leaving: the booking method picks the lot, whose cost is nothing.
                        else Cost(None, currency, None, None)
                    ),
                ),
                Posting(account=None, units=Amount(NO_COST, currency)),
            ]
            booked = True
        elif not granted and way == ACQUISITION and commodity and quantity and unit_price:
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
            # Anything unrecognised goes to the rules engine, where it surfaces in review
            # rather than being guessed at.
            postings = [
                Posting(
                    account=cash or positions,
                    units=Amount(-_decimal(row.get("amount") or 0), currency),
                ),
                Posting(account=None, units=None),
            ]

        yield Transaction(
            meta=meta,
            date=date,
            flag=FLAG_OKAY,
            payee=payee,
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
