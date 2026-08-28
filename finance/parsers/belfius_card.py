"""Belfius Mastercard statement — the monthly ``UITGAVENSTAAT`` PDF.

The one export here that is not a data file. It is a printed statement, and it is parsed rather
than transcribed because the alternative is hand-typing every row: the PDFs carry a real text
layer, laid out one line per transaction, so nothing has to be recognised from an image.

**What this adds is decomposition, not money.** The card's settlements were already in the
ledger — the bank export shows each month's ``MASTERCARD AFREKENING`` leaving the account — so
the total was never missing. What was missing is *what was bought*: one opaque monthly figure
per statement in place of the individual merchants. That is also why the card can become a
liability rather than an expense: with the purchases visible it can be drawn on as well as
repaid, which is what a card actually does. A card whose statements are absent cannot, and must
stay an expense — otherwise it only ever gets repaid, from nothing.

So there are two halves and they must not be booked twice. This parser emits the **purchases**
against the liability; the monthly settlement on the bank side pays it down. The liability then
nets to what has been charged and not yet debited, which is a check on both halves at once: a
misread row or an unmatched settlement shows up as a balance that will not go away.

**Each statement states its own total, and the rows must sum to it.** They do, in every
statement here, and the parser *raises* when they do not. That is deliberate: a PDF is the one
input format where partial extraction is a realistic failure — a layout change drops a row
rather than corrupting it — and importing a statement with a row missing is far worse than
failing to import it, because the total would still look plausible. The same reasoning as
``resolve_account`` raising rather than inventing an account.

**Row dates carry no year.** They are given as ``dd/mm`` and the year comes from the statement
period, which routinely spans a year boundary — the period runs from the 26th to the 25th, so
every December statement crosses one. Each date is resolved to the candidate year that puts it
inside the period; a settlement date, which may fall a few days *after* the period closes, is
resolved forward from its own transaction date instead.

Two shapes worth knowing:

* A purchase in a foreign currency states **both** amounts on the row — the original and what
  it settled at — with the conversion rate on a continuation line below. Only the euro is
  booked, because that is what the card was charged; the original goes to metadata. The rate
  line is split across two lines by the text extractor as often as not, which is why the
  continuation is accumulated rather than matched in one go.
* The description ends in a two-letter country code, always. It is stripped off into metadata
  so that the same merchant reads the same whichever country it billed from, and what is left
  becomes the payee — which for the recurring merchants here is stable enough to group on.

There is **no row identifier** in a statement, so the transaction id is built from the closing
date and the row's position in it. That matters more than it sounds: the file *names* are
export timestamps rather than statement identifiers, so re-exporting the same statement gives
it a new filename, and without a content-derived id the whole statement would import twice.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import date as Date
from datetime import datetime
from decimal import Decimal

from finance.models import FLAG_OKAY, Amount, Directive, Posting, Transaction
from finance.parsers.base import Accounts, parse_decimal, resolve_account
from finance.text import normalize_text

INSTITUTION = "belfius_card"

#: Key under which the card account is expected in ``own_accounts``.
ACCOUNT_KEY = "belfius_card"

#: A transaction row: two ``dd/mm`` dates, a description, then the amount. A foreign-currency
#: purchase puts its original amount at the end of the description, which is peeled off below.
ROW = re.compile(
    r"^(?P<booked>\d{2}/\d{2})\s+(?P<settled>\d{2}/\d{2})\s+(?P<description>.*?)\s+"
    r"(?P<amount>[\d.]+,\d+)\s+(?P<currency>[A-Z]{3})\s*(?P<sign>[-+])\s*$"
)

#: The original amount of a foreign-currency purchase, trailing its description.
ORIGINAL = re.compile(r"^(?P<description>.*?)\s+(?P<amount>[\d.]+,\d+)\s+(?P<currency>[A-Z]{3})$")

#: The statement's own total, which every row must add up to.
TOTAL = re.compile(
    r"^Totaal\s+(?P<amount>[\d.]+,\d+)\s+(?P<currency>[A-Z]{3})\s*(?P<sign>[-+])\s*$"
)

CLOSING = re.compile(r"^Afsluitingsdatum\s+(\d{2}/\d{2}/\d{4})")
DEBITED = re.compile(r"^Datum van debet\s+(\d{2}/\d{2}/\d{4})")
PERIOD = re.compile(r"^Transacties van\s+(\d{2}/\d{2}/\d{4})\s+tot\s+(\d{2}/\d{2}/\d{4})")
CARD = re.compile(r"^Transacties\s*-\s*Kaartnummer\s+(\S(?:.*\S)??)\s+-\s")

#: Continuation lines belonging to the row above: the wallet a payment went through, and the
#: conversion rate, which the text extractor splits after the ``=`` about as often as not.
WALLET = re.compile(r"\(Via\s*(?P<wallet>[^)]*)\)")
RATE = re.compile(r"1\s*EUR\s*=\s*(?P<rate>[\d.]+,?\d*)\s*(?P<currency>[A-Z]{3})")
CONTINUES = re.compile(r"^(\(Via\b|1\s*EUR\s*=|[\d.]+,?\d*\s*[A-Z]{3}$)")

#: The trailing country code every description ends with.
COUNTRY = re.compile(r"\s+([A-Z]{2})$")

#: ``-`` marks a charge. No credit appears in any statement here, so the other direction is
#: reasoned from the totals rather than observed: the sign is repeated on the statement total,
#: which is what the bank debits, and a debit is written ``-``.
CHARGE = "-"


class StatementError(ValueError):
    """A statement's rows do not add up to the total it states.

    Raised rather than imported. A PDF is the one input here where losing a row to a layout
    change is realistic, and a statement short of a row still looks entirely plausible.
    """


def _date(text: str) -> Date:
    return datetime.strptime(text.strip(), "%d/%m/%Y").date()


def _within(day: int, month: int, start: Date, end: Date) -> Date:
    """Resolve a ``dd/mm`` to the year that puts it inside the statement period.

    The period runs from the 26th of one month to the 25th of the next, so every December
    statement spans a year boundary and the year cannot be assumed from either end.
    """
    for year in dict.fromkeys((start.year, end.year)):
        try:
            candidate = Date(year, month, day)
        except ValueError:
            continue
        if start <= candidate <= end:
            return candidate
    # Outside the stated period: keep the closing year rather than dropping the row, and let
    # the date look wrong in review instead of the transaction disappearing.
    return Date(end.year, month, day)


def _after(day: int, month: int, booked: Date) -> Date:
    """Resolve a settlement ``dd/mm`` forward from the purchase it settles.

    A purchase near the end of a period settles after the period closes, and one on 31 December
    settles in the next year, so this cannot be resolved against the period.
    """
    for year in (booked.year, booked.year + 1):
        try:
            candidate = Date(year, month, day)
        except ValueError:
            continue
        if candidate >= booked:
            return candidate
    return booked


def _split_dm(text: str) -> tuple[int, int]:
    day, _, month = text.partition("/")
    return int(day), int(month)


class _Row:
    __slots__ = ("amount", "booked", "continuation", "description", "original", "settled", "sign")

    def __init__(self, match: re.Match) -> None:
        self.booked = match.group("booked")
        self.settled = match.group("settled")
        self.amount = parse_decimal(match.group("amount"))
        self.sign = match.group("sign")
        description = match.group("description").strip()
        self.original: tuple[Decimal, str] | None = None
        found = ORIGINAL.match(description)
        if found and found.group("currency") != match.group("currency"):
            # A foreign-currency purchase states what it cost abroad as well as what the card
            # was charged. Only the latter is booked.
            self.original = (parse_decimal(found.group("amount")), found.group("currency"))
            description = found.group("description").strip()
        self.description = description
        self.continuation = ""


def parse_from_text(text: str, accounts: Accounts, *, source_file: str = "") -> Iterator[Directive]:
    """Parse the text of one statement. Split out so tests need no PDF."""
    card = resolve_account(accounts, ACCOUNT_KEY, source=source_file or INSTITUTION)

    closing: Date | None = None
    debited: Date | None = None
    period: tuple[Date, Date] | None = None
    number: str | None = None
    rows: list[_Row] = []
    total: tuple[Decimal, str, str] | None = None
    current: _Row | None = None

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue

        found = ROW.match(line)
        if found:
            current = _Row(found)
            rows.append(current)
            continue

        found = TOTAL.match(line)
        if found:
            total = (
                parse_decimal(found.group("amount")),
                found.group("currency"),
                found.group("sign"),
            )
            current = None
            continue

        if current is not None and CONTINUES.match(line):
            current.continuation = f"{current.continuation} {line}".strip()
            continue
        current = None

        found = CLOSING.match(line)
        if found:
            closing = _date(found.group(1))
            continue
        found = DEBITED.match(line)
        if found:
            debited = _date(found.group(1))
            continue
        found = PERIOD.match(line)
        if found:
            period = (_date(found.group(1)), _date(found.group(2)))
            continue
        found = CARD.match(line)
        if found:
            number = normalize_text(found.group(1))

    if not rows:
        # A statement with no transactions is a real thing — the card has months of no use, and
        # those months simply have no settlement on the bank side either.
        return

    charged = sum(row.amount if row.sign == CHARGE else -row.amount for row in rows)
    if total is not None and charged != total[0]:
        raise StatementError(
            f"{source_file or 'statement'}: {len(rows)} row(s) add up to {charged}, "
            f"but the statement states {total[0]} {total[1]} — a row was probably not read"
        )
    if total is None:
        raise StatementError(
            f"{source_file or 'statement'}: {len(rows)} row(s) read but no stated total to "
            "check them against; refusing to import a statement that cannot be verified"
        )

    start, end = period or (closing or Date.min, closing or Date.max)

    for index, row in enumerate(rows):
        day, month = _split_dm(row.booked)
        booked = _within(day, month, start, end)
        settled_day, settled_month = _split_dm(row.settled)

        meta: dict[str, object] = {"institution": INSTITUTION}
        if source_file:
            meta["source_file"] = source_file
        # A statement carries no row identifier, and the file names are export timestamps
        # rather than statement identifiers — so without an id derived from the content, a
        # re-export of the same statement imports every row a second time.
        if closing is not None:
            meta["transaction_id"] = f"{closing.isoformat()}:{index}"
            meta["statement"] = closing
        if debited is not None:
            meta["debited"] = debited
        if number:
            meta["card"] = number
        meta["settlement_date"] = _after(settled_day, settled_month, booked)

        description = row.description
        country = COUNTRY.search(description)
        if country:
            meta["country"] = country.group(1)
            description = description[: country.start()].strip()

        if row.original is not None:
            meta["foreign_amount"] = f"{row.original[0]} {row.original[1]}"
        if row.continuation:
            wallet = WALLET.search(row.continuation)
            if wallet and wallet.group("wallet").strip():
                meta["wallet"] = normalize_text(wallet.group("wallet"))
            rate = RATE.search(row.continuation)
            if rate:
                meta["exchange_rate"] = f"1 EUR = {rate.group('rate')} {rate.group('currency')}"

        signed = row.amount if row.sign == CHARGE else -row.amount
        yield Transaction(
            meta=meta,
            date=booked,
            flag=FLAG_OKAY,
            payee=normalize_text(description) or None,
            narration="Card payment",
            tags=frozenset(),
            links=frozenset(),
            postings=(
                # A charge draws on the card, which is what makes it a liability rather than a
                # category of spending. The settlement on the bank side pays it back down.
                Posting(account=card, units=Amount(-signed, "EUR")),
                Posting(account=None, units=Amount(signed, "EUR")),
            ),
        )


def _text(path: str) -> str:
    from pypdf import PdfReader

    reader = PdfReader(path)
    # Every page is read, not just the first. Transactions are on page one in every statement
    # here, but a busier month would spill over, and the boilerplate page cannot match a
    # transaction row — it has no pair of dates to start one.
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def parse_file(path: str, accounts: Accounts) -> Iterator[Directive]:
    yield from parse_from_text(_text(path), accounts, source_file=path)
