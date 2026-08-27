"""Trade Republic transaction export.

The richest export of any source here: every row carries a stable ``transaction_id``, and
transfers name the counterparty IBAN outright, so deduplication is exact and transfers between
the account holder's own accounts are recognised without any rule.

``amount`` is signed from the account's point of view, and ``fee`` and ``tax`` are *separate*
deductions rather than being folded into it. A ``CARD_ORDERING_FEE`` row makes this plain: its
amount is zero and its fee is the whole story, so the cash movement is ``amount + fee + tax``
and the fee is booked as its own posting.

The export is cash-only in its current form — ``shares``, ``price``, ``symbol`` and
``asset_class`` exist as columns but are empty throughout. If a future export populates them,
the cash side stays correct while the *position* is not tracked, exactly as with DEGIRO. Those
columns are recorded in metadata so the omission is visible rather than silent.
"""

from __future__ import annotations

import csv
from collections.abc import Iterator
from datetime import date as Date
from decimal import Decimal

from finance.models import FLAG_OKAY, Amount, Posting, Transaction
from finance.parsers.base import Accounts, quantize_money, resolve_account
from finance.text import normalize_text

INSTITUTION = "trade_republic"

#: ``own_accounts`` key for the cash account.
ACCOUNT_KEY = "trade_republic"

#: Optional ``own_accounts`` keys overriding where fees and taxes are booked. These are the
#: parser's only policy choices, and they exist because a fee here is a *column* on another
#: transaction rather than a row of its own, so no rule could ever claim it.
FEE_ACCOUNT_KEY = "trade_republic_fee"
TAX_ACCOUNT_KEY = "trade_republic_tax"
DEFAULT_FEE_ACCOUNT = "Expenses:Financial:Fees"
DEFAULT_TAX_ACCOUNT = "Expenses:Taxes"

#: Placeholder Trade Republic writes when a transfer carried no description.
NO_DESCRIPTION = "No SEPA description provided"

#: Columns describing a position rather than cash. Empty in every row seen so far.
POSITION_COLUMNS = ("asset_class", "symbol", "shares", "price")


def _decimal(value: str | None) -> Decimal:
    """Parse an amount, or zero when the column is blank.

    Values arrive with six decimal places (``1000.000000``), so they are quantized to cents
    rather than carried through as-is.
    """
    text = (value or "").strip()
    return quantize_money(Decimal(text)) if text else Decimal("0.00")


def parse_file(path: str, accounts: Accounts) -> Iterator[Transaction]:
    own = resolve_account(accounts, ACCOUNT_KEY, source=path)
    fee_account = accounts.get(FEE_ACCOUNT_KEY.upper(), DEFAULT_FEE_ACCOUNT)
    tax_account = accounts.get(TAX_ACCOUNT_KEY.upper(), DEFAULT_TAX_ACCOUNT)

    with open(path, encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if not (row.get("date") or "").strip():
                continue

            amount = _decimal(row.get("amount"))
            fee = _decimal(row.get("fee"))
            tax = _decimal(row.get("tax"))
            if amount == 0 and fee == 0 and tax == 0:
                continue

            currency = (row.get("currency") or "EUR").strip() or "EUR"
            kind = (row.get("type") or "").strip()
            description = normalize_text(row.get("description") or "")
            counterparty = normalize_text(row.get("counterparty_name") or "")
            name = normalize_text(row.get("name") or "")

            meta: dict[str, object] = {
                "institution": INSTITUTION,
                "source_file": path,
            }
            if row.get("transaction_id"):
                meta["transaction_id"] = row["transaction_id"].strip()
            if kind:
                meta["type"] = kind
            if row.get("counterparty_iban"):
                meta["counterparty_iban"] = normalize_text(row["counterparty_iban"])
            if row.get("mcc_code"):
                meta["mcc"] = row["mcc_code"].strip()
            if row.get("payment_reference"):
                meta["reference"] = normalize_text(row["payment_reference"])
            if name and name != counterparty:
                meta["name"] = name
            for column in POSITION_COLUMNS:
                if (row.get(column) or "").strip():
                    meta[column] = row[column].strip()

            postings = [
                # The cash actually moved: the amount net of anything deducted alongside it.
                Posting(account=own, units=Amount(amount + fee + tax, currency)),
            ]
            if fee:
                postings.append(Posting(account=fee_account, units=Amount(-fee, currency)))
            if tax:
                postings.append(Posting(account=tax_account, units=Amount(-tax, currency)))
            if amount:
                # Beancount balances this to -amount, the gross movement.
                postings.append(Posting(account=None, units=None))

            yield Transaction(
                meta=meta,
                date=Date.fromisoformat(row["date"].strip()),
                flag=FLAG_OKAY,
                payee=counterparty or name or None,
                narration=description if description != NO_DESCRIPTION else kind,
                tags=frozenset(),
                links=frozenset(),
                postings=tuple(postings),
            )
