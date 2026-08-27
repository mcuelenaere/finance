"""DEGIRO cash account export (``Account.csv``).

This is the *cash* ledger — deposits, withdrawals, fees, dividends, taxes and the cash leg of
trades.

It is also the best source for positions, despite the lack of a quantity column. Trade rows
describe themselves as ``Koop|Verkoop <quantity> @ <price> <currency>`` in the description, and
556 rows here parse that way with all 114 fractional quantities intact. DEGIRO's separate
``Transactions.csv`` looks like the obvious choice for cost basis and is not: it covers fewer
trades (498) and leaves the quantity column *empty* for fractional crypto, so the size of those
positions is simply absent. When lots are implemented, parse them from the descriptions here.

Two things about this format matter enough to spell out, because getting either wrong
overstates the account by six figures without any error surfacing.

**Export it with "hide background transactions" turned off.** Since DEGIRO moved cash to a
flatexDEGIRO Bank account, idle cash is swept there at the end of each day and swept back when
needed. Those sweeps move the reported balance, so an export that hides them has a balance
column that jumps for no visible reason. With them included the balance reconciles; without
them it cannot.

**Sweep rows put the amount in the description, not in the amount column.** They read
``Overboeking naar|van uw geldrekening bij flatexDEGIRO Bank SE: 1.345,39 EUR`` with ``Mutatie``
left empty. A parser that skips rows lacking an amount drops exactly the rows that explain the
balance jumps — which is how this file came to look undecodable.

The sweeps are booked against a sibling ``:Flatex`` account rather than being discarded. That
keeps the cash account reconcilable against ``Saldo`` (so the balance assertions below are
meaningful) while ensuring the parked cash still counts once, and only once, toward net worth.
"""

from __future__ import annotations

import csv
import re
from collections.abc import Iterator
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

INSTITUTION = "degiro"

#: ``own_accounts`` key for the trading cash account.
ACCOUNT_KEY = "degiro"

#: Optional ``own_accounts`` key for the flatexDEGIRO parking account. Defaults to a sibling of
#: the cash account.
FLATEX_ACCOUNT_KEY = "degiro_flatex"

#: Optional ``own_accounts`` key for where realised gains land. A parser policy choice, and
#: necessarily one: a disposal's gain is computed by Beancount from the lots, so there is no row
#: for a rule to claim.
GAINS_ACCOUNT_KEY = "degiro_gains"
DEFAULT_GAINS_ACCOUNT = "Income:CapitalGains"

COLUMNS = 12

#: End-of-day cash sweep to or from the flatexDEGIRO Bank account. "naar" (to) increases the
#: reported balance and "van" (from) decreases it.
SWEEP = re.compile(
    r"Overboeking (?P<direction>naar|van) uw geldrekening bij flatexDEGIRO Bank SE: "
    r"(?P<amount>[\d.,]+) (?P<currency>[A-Z]{3})"
)

#: Balance assertions are allowed to drift by this much. DEGIRO records money-market-fund
#: conversions to three decimals while rounding the balance column to cents, so a few cents of
#: divergence accumulates over the years and is not a parser error.
BALANCE_TOLERANCE = "0.10"


def _parse_narration_amount(text: str) -> Decimal:
    """Parse a money amount taken from description text.

    Deliberately not :func:`finance.parsers.base.parse_decimal`. Amounts written into these
    descriptions sometimes omit the cents, leaving values like ``8.305`` where the dot is a
    thousands separator — 405 of 413 sweep rows include comma decimals, but the handful that do
    not would otherwise be read as 8.305 instead of 8305.

    The rule cannot go in the shared helper: a dots-only value with a three-digit tail is a
    thousands separator *here*, but elsewhere it is an ordinary decimal (an FX rate of ``1.234``
    must stay 1.234).
    """
    if "," in text:
        return Decimal(text.replace(".", "").replace(",", "."))
    if "." in text and len(text.rsplit(".", 1)[1]) == 3:
        return Decimal(text.replace(".", ""))
    return Decimal(text)


#: Cash swept to or from the flatexDEGIRO account, however the row happens to describe it.
SWEEP_PREFIX = "Overboeking naar uw geldrekening bij flatexDEGIRO"
SWEEP_PREFIX_BACK = "Overboeking van uw geldrekening bij flatexDEGIRO"

#: A trade, as the description spells it: ``Koop 474 @ 91,295 EUR``. Mergers and spin-offs use
#: the same wording, so they are matched by this too.
TRADE = re.compile(
    r"\b(?P<side>Koop|Verkoop)\s+(?P<quantity>[\d.,]+)\s*@\s*(?P<price>[\d.,]+)\s*"
    r"(?P<currency>[A-Z]{3})"
)

#: A share split, which names no side: ``SPLIT AANPASSING: 85 ADR on Sony @ 19,028 USD``.
#: These come in pairs whose cash amounts cancel — one row retires the old share count and the
#: other creates the new one — so the direction comes from the sign of the cash instead.
#: Without them the lots never adjust, and a later disposal has nothing to reduce.
SPLIT = re.compile(
    r"SPLIT AANPASSING:\s*(?P<quantity>[\d.,]+)\s+.*?@\s*(?P<price>[\d.,]+)\s*"
    r"(?P<currency>[A-Z]{3})"
)

#: Where positions are held. One account holding many commodities: FIFO applies per commodity,
#: so splitting per instrument would buy nothing.
POSITIONS_LEAF = "Positions"

#: Lot matching for disposals. A broker export never names the lot being sold.
BOOKING = "FIFO"

#: How far quantity x price may sit from the cash amount and still be considered proven.
TRADE_TOLERANCE = Decimal("0.02")


def _number_readings(text: str) -> set[Decimal]:
    """Every plausible reading of a European-formatted number.

    ``1.172`` is either one-point-one-seven-two or one thousand one hundred and seventy-two,
    and the description gives nothing to decide with. Rather than guess, produce both and let
    the caller pick the one the cash amount confirms.
    """
    readings = set()
    if "," in text:
        readings.add(Decimal(text.replace(".", "").replace(",", ".")))
    else:
        readings.add(Decimal(text))
        if "." in text:
            readings.add(Decimal(text.replace(".", "")))
    return readings


def parse_trade(
    description: str, cash: Decimal, cash_currency: str
) -> tuple[Decimal, Decimal] | None:
    """Return ``(quantity, unit_price)`` for a trade, or None if it cannot be proven.

    The reading is chosen by arithmetic rather than by guesswork: of the candidate
    interpretations, the one whose quantity times price equals the cash that actually moved is
    the right one. Bonds are quoted as a percentage of nominal, so a hundredfold scaling is a
    candidate too.

    None means "do not book a position" — better a row that stays cash-only and shows up in
    review than a holding invented from a misread number.
    """
    match = TRADE.search(description) or SPLIT.search(description)
    if not match or not cash or match.group("currency") != cash_currency:
        return None

    target = abs(cash)
    if "side" in match.groupdict() and match.group("side"):
        sign = Decimal(1) if match.group("side") == "Koop" else Decimal(-1)
    else:
        # A split row names no side: cash out means shares acquired, cash in means retired.
        sign = Decimal(1) if cash < 0 else Decimal(-1)

    for quantity in _number_readings(match.group("quantity")):
        for price in _number_readings(match.group("price")):
            for scale in (Decimal(1), Decimal(100)):
                if abs(quantity * price / scale - target) <= TRADE_TOLERANCE:
                    return sign * quantity, price / scale
    return None


def commodity_of(isin: str, product: str) -> str | None:
    """The Beancount commodity for an instrument.

    ISIN where there is one — stable, unambiguous and already a valid commodity name. Crypto
    has no ISIN, so the product name is used instead.
    """
    if isin.strip():
        return isin.strip().upper()
    name = re.sub(r"[^A-Za-z0-9]", "", product).upper()
    return name if name[:1].isalpha() else None


#: Idle cash converted into, or out of, DEGIRO's money-market fund.
MONEY_MARKET = ("Conversie geldmarktfonds", "DEGIRO Geldmarktfondsen", "EUR CASH FUND")


def _row_amount(row: list[str], previous: Decimal | None) -> tuple[Decimal, str] | None:
    """Return ``(amount, currency)``, or None when the row moves no cash.

    Three sources, in order of preference:

    1. the ``Mutatie`` column;
    2. the amount written into a sweep description, where ``Mutatie`` is empty;
    3. the change in ``Saldo`` since the previous row in the same currency.

    The third exists because DEGIRO occasionally emits a row with neither — one sweep in this
    export has its description truncated mid-sentence, before the amount, and the
    money-market-fund conversions carry no amount at all. Their effect on the balance is real,
    so deriving it from the balance column is the only way the account reconciles.
    """
    currency = row[9] or row[7] or "EUR"

    if row[8]:
        return parse_decimal(row[8]), (row[7] or currency)

    match = SWEEP.search(row[5])
    if match:
        amount = _parse_narration_amount(match.group("amount"))
        if match.group("direction") == "van":
            amount = -amount
        return amount, match.group("currency")

    if previous is not None and row[10]:
        return parse_decimal(row[10]) - previous, currency

    return None


def _counter_account(description: str, cash: str, flatex: str) -> str | None:
    """The other side, where the row's own description already determines it.

    Both of these are movements between DEGIRO's own pots, so booking them here keeps them out
    of the rules engine — where a mismatched rule could route them somewhere that inflates net
    worth. Everything else is left unresolved for rules to classify.
    """
    if (
        description.startswith((SWEEP_PREFIX, SWEEP_PREFIX_BACK))
        or "Cash Sweep Transfer" in description
    ):
        return flatex
    if any(marker in description for marker in MONEY_MARKET):
        return _sibling(cash, "MoneyMarket")
    return None


def parse_from_io(handle, accounts: Accounts, *, source_file: str = "") -> Iterator[Directive]:
    cash = resolve_account(accounts, ACCOUNT_KEY, source=source_file or INSTITUTION)
    flatex = accounts.get(FLATEX_ACCOUNT_KEY.upper()) or _sibling(cash, "Flatex")
    positions = _sibling(cash, POSITIONS_LEAF)
    gains = accounts.get(GAINS_ACCOUNT_KEY.upper(), DEFAULT_GAINS_ACCOUNT)
    booked_positions = False
    held: dict[str, Decimal] = {}

    rows = [
        row
        for row in csv.reader(handle)
        if len(row) == COLUMNS and row[0] and row[0] != "Datum" and row[10]
    ]
    rows.reverse()  # the export is newest-first

    last_balance: dict[str, tuple[str, Decimal]] = {}
    running: dict[str, Decimal] = {}

    for row in rows:
        currency_of_balance = row[9] or row[7] or "EUR"
        parsed = _row_amount(row, running.get(currency_of_balance))
        if row[10]:
            running[currency_of_balance] = parse_decimal(row[10])
        if parsed is None:
            continue
        amount, currency = parsed

        date, time, _value_date, product, isin, description = row[:6]
        order_id = row[11]

        meta: dict[str, object] = {"institution": INSTITUTION}
        if source_file:
            meta["source_file"] = source_file
        if isin:
            meta["isin"] = isin
        if product:
            meta["product"] = normalize_text(product)
        if order_id:
            meta["order_id"] = order_id

        narration = normalize_text(description)
        if product and isin:
            narration = f"{normalize_text(product)} ({isin}): {narration}"

        counter = _counter_account(description, cash, flatex)

        postings = _postings(
            held=held,
            description=description,
            isin=isin,
            product=product,
            amount=amount,
            currency=currency,
            cash=cash,
            counter=counter,
            positions=positions,
            gains=gains,
        )
        if any(posting.account == positions for posting in postings):
            booked_positions = True

        yield Transaction(
            meta=meta,
            date=datetime.strptime(f"{date} {time}", "%d-%m-%Y %H:%M").date(),
            flag=FLAG_OKAY,
            payee="DEGIRO",
            narration=narration,
            tags=frozenset(),
            links=frozenset(),
            postings=postings,
        )

        if row[9]:
            last_balance[row[9]] = (row[0], parse_decimal(row[10]))

    if booked_positions and rows:
        # Disposals need FIFO: the export says what was sold, never which lot it came from.
        yield Open(
            meta={"institution": INSTITUTION},
            date=datetime.strptime(rows[0][0], "%d-%m-%Y").date(),
            account=positions,
            currencies=(),
            booking=BOOKING,
        )

    # The balance column is the whole point of insisting on background transactions: it is the
    # only independent check that this file has been read correctly.
    for currency, (date, number) in sorted(last_balance.items()):
        meta: dict[str, object] = {"institution": INSTITUTION}
        if source_file:
            meta["source_file"] = source_file
        yield Balance(
            meta=meta,
            # Stated after the day's movements; Beancount evaluates at the start of a date.
            date=datetime.strptime(date, "%d-%m-%Y").date() + timedelta(days=1),
            account=cash,
            amount=Amount(number, currency),
            tolerance=Decimal(BALANCE_TOLERANCE),
        )


def _postings(
    *,
    held: dict[str, Decimal],
    description: str,
    isin: str,
    product: str,
    amount: Decimal,
    currency: str,
    cash: str,
    counter: str | None,
    positions: str,
    gains: str,
) -> tuple[Posting, ...]:
    """Postings for one row: a trade with its lot where that can be proven, else cash only.

    ``held`` tracks what this parser has actually booked, so a disposal is only written when
    there is something to dispose of. Zero-cash acquisitions — spin-offs, and splits whose
    rows carry no amount — cannot be proven against a cash figure and so are never booked;
    without this check the later sale of those shares would reduce a position that does not
    exist, and Beancount rejects the whole transaction.
    """
    trade = parse_trade(description, amount, currency) if counter is None else None
    commodity = commodity_of(isin, product) if trade else None

    if trade and commodity and trade[0] < 0 and held.get(commodity, Decimal(0)) < -trade[0]:
        trade = None  # more shares than this parser ever created; stay cash-only

    if trade and commodity:
        quantity, price = trade
        held[commodity] = held.get(commodity, Decimal(0)) + quantity
        if quantity > 0:
            return (
                Posting(account=cash, units=Amount(amount, currency)),
                Posting(
                    account=positions,
                    units=Amount(quantity, commodity),
                    # The cash paid, as a total: a quoted unit price rounded to four decimals
                    # rarely multiplies back to the cent that actually left the account.
                    cost=Cost(abs(amount), currency, None, None, total=True),
                ),
            )
        # A disposal names no lot, so the booking method picks one and the elided gain
        # posting absorbs whatever the difference turns out to be.
        return (
            Posting(
                account=positions,
                units=Amount(quantity, commodity),
                cost=EMPTY_COST,
                price=Amount(price, currency),
            ),
            Posting(account=cash, units=Amount(amount, currency)),
            Posting(account=gains, units=None),
        )

    return (
        Posting(account=cash, units=Amount(amount, currency)),
        Posting(account=counter, units=None),
    )


def _sibling(account: str, leaf: str) -> str:
    """``Assets:Investments:Degiro:Cash`` -> ``Assets:Investments:Degiro:Flatex``."""
    parent, _, _ = account.rpartition(":")
    return f"{parent}:{leaf}" if parent else f"{account}:{leaf}"


def parse_file(path: str, accounts: Accounts) -> Iterator[Directive]:
    with open(path, encoding="utf-8") as handle:
        yield from parse_from_io(handle, accounts, source_file=path)
