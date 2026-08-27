"""Edenred meal-voucher and eco-cheque exports (``TRE.xlsx``, ``ECE.xlsx``, ``SCE.xlsx``).

One parser covers all three: the sheets share a column layout and differ only in which voucher
they describe. Each voucher type gets its own asset account, because the balances are not
interchangeable — an eco-cheque cannot buy lunch.

This is the only source here that is otherwise completely invisible. Voucher spending never
touches a bank statement, so without it that spending is simply missing from the expense
picture rather than merely uncategorized.

``MovementType`` splits the rows cleanly in two:

``eCredit Funding``
    the employer topping the voucher balance up — income, though which income account is a
    question for the rules file.
``Transaction at outlet``
    spending it, with the merchant in ``Outlet``.

``ECreditStatus`` maps onto Beancount's flags: ``Cleared`` is settled (``*``) and ``Active``
is not yet (``!``).
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

from openpyxl import load_workbook

from finance.models import FLAG_OKAY, FLAG_PENDING, Amount, Posting, Transaction
from finance.parsers.base import Accounts, quantize_money, resolve_account
from finance.text import normalize_text

INSTITUTION = "edenred"

CURRENCY = "EUR"

#: Voucher type per ``Product`` value, and per filename for the rows that leave it blank.
BY_PRODUCT = {
    "Ticket Restaurant Electronique": "meal",
    "Ticket EcoCheque": "eco",
    "Ticket Sport&Culture": "sport",
}
BY_FILENAME = {"TRE": "meal", "ECE": "eco", "SCE": "sport"}

#: ``own_accounts`` key per voucher type, e.g. ``edenred_meal``.
ACCOUNT_KEY = "edenred_{kind}"

FUNDING = "eCredit Funding"
SETTLED = "Cleared"

COLUMNS = (
    "TransactionDate",
    "TransactionAmount",
    "Product",
    "ECreditStatus",
    "MovementType",
    "Scheme",
    "Outlet",
    "ReasonTxt",
    "OrderNumberEdps",
)


def voucher_kind(product: str | None, path: str) -> str:
    """Which voucher a row describes.

    ``Product`` is populated on some rows and blank on others within the same sheet, so the
    filename is the fallback rather than an alternative.
    """
    if product and product.strip() in BY_PRODUCT:
        return BY_PRODUCT[product.strip()]
    stem = Path(path).stem.upper()
    for marker, kind in BY_FILENAME.items():
        if stem.startswith(marker):
            return kind
    raise ValueError(
        f"cannot tell which Edenred voucher {path!r} holds: "
        f"Product={product!r} and the filename matches none of {sorted(BY_FILENAME)}"
    )


def _rows(path: str) -> Iterator[dict[str, object]]:
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        sheet = workbook[workbook.sheetnames[0]]
        rows = sheet.iter_rows(values_only=True)
        header = next(rows, None)
        if header is None:
            return
        names = [str(name).strip() if name else "" for name in header]
        missing = set(COLUMNS) - set(names)
        if missing:
            raise ValueError(f"{path}: missing expected columns {sorted(missing)}")
        for row in rows:
            if not any(cell is not None for cell in row):
                continue
            yield dict(zip(names, row, strict=False))
    finally:
        workbook.close()


def parse_file(path: str, accounts: Accounts) -> Iterator[Transaction]:
    for row in _rows(path):
        date = row.get("TransactionDate")
        amount = row.get("TransactionAmount")
        if date is None or amount is None:
            continue

        kind = voucher_kind(row.get("Product"), path)  # type: ignore[arg-type]
        own = resolve_account(accounts, ACCOUNT_KEY.format(kind=kind), source=path)

        movement = str(row.get("MovementType") or "").strip()
        outlet = normalize_text(str(row["Outlet"])) if row.get("Outlet") else None
        status = str(row.get("ECreditStatus") or "").strip()

        meta: dict[str, object] = {
            "institution": INSTITUTION,
            "source_file": path,
            "voucher": kind,
        }
        if movement:
            meta["movement_type"] = movement
        if row.get("Scheme"):
            meta["scheme"] = normalize_text(str(row["Scheme"]))
        if row.get("OrderNumberEdps"):
            # Populated on only a handful of rows, so not usable as a dedup id.
            meta["order_number"] = str(row["OrderNumberEdps"]).strip()
        if row.get("ReasonTxt"):
            meta["reason"] = normalize_text(str(row["ReasonTxt"]))

        yield Transaction(
            meta=meta,
            date=date.date() if hasattr(date, "date") else date,
            # `Active` means the outlet has not settled yet; Beancount's `!` says exactly that.
            flag=FLAG_OKAY if status == SETTLED else FLAG_PENDING,
            payee=outlet,
            narration=movement or "Edenred",
            tags=frozenset(),
            links=frozenset(),
            postings=(
                Posting(
                    account=own,
                    units=Amount(quantize_money(_as_decimal(amount)), CURRENCY),
                ),
                Posting(account=None, units=None),
            ),
        )


def _as_decimal(value: object) -> Decimal:
    """Coerce openpyxl's int/float/str cell value to Decimal via str.

    Going through ``str`` matters: these amounts are stored as floats, and one sheet holds
    values such as -4.4000000000000004 for what is plainly -4.40.
    """
    return Decimal(str(value))
