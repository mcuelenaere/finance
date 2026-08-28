"""Wirex activity export (the JSON the app's activity feed returns).

A crypto-funded debit card: the balance is held in ETH, and every card purchase sells however
much ETH the settlement costs. So a "card payment" here is two facts at once — a disposal of
crypto and an ordinary household expense — and the export states both sides, which is why these
rows can carry a merchant name *and* book a lot.

**Which is why one such row becomes two transactions.** The obvious reading is one row in, one
transaction out, but that puts a realised capital gain and a hardware-shop bill in the same
entry, and then every merchant the card ever paid appears in the capital-gains account. Salary
is not attached to the things it buys; neither is this. The pair pivots on the euro pocket Wirex
converts through — the sale lands proceeds there, the purchase spends them — and the two net to
nothing in it, which is why its stated balances still reconcile. They share a link so the
relationship stays visible instead of merely implied, and the metadata is split so the crypto
half carries no merchant and the merchant half no blockchain.

``amount_from`` is what left the funding account; ``amount_to`` is what the merchant received.
They are frequently different currencies and **no euro value is given for the pair**, so the
expense is booked as whichever of the two is fiat: what actually left the account when that is
euro, and what the merchant charged when the account paid in ether. Converting between them
would need a rate this export does not quote for that pair; the other figure is kept in
``meta.foreign_amount`` rather than dropped.

**Rows that moved money are identified by whether they name an amount, not by their status.**
Some rows are marked ``Failed`` and still debited the account, each refunded later at the ETH
rate of the refund day rather than the purchase day. Reading ``status`` instead drops them and
leaves the derived ETH balance adrift by a growing sum — invisibly, because nothing else in the
file contradicts it. Other ``Failed`` rows name no amount at all (insufficient funds, a deposit
rejected by the sender's risk check): nothing left the account, so nothing is booked. One-way
rows are the exception and *are* decided on status, because they name an amount whether or not
the transfer completed.

**The export replays exactly against its own stated balances, from a zero opening.** Only rows
whose counterparty *is* an account carry a real running balance; on a card row the nested
balance is a placeholder that is always zero and must not be read as one. Every one of those
anchors agrees with the amounts to the last decimal, which is what makes the
missing-``Failed``-row fault detectable at all rather than a slow silent drift.

**Cost basis: units arriving here are booked at zero, and that is a decision rather than a
gap.** The ETH arrives from a self-custody wallet, and the owner's standing instruction is to
treat everything coming out of it as pure profit: the wallet's history is too tangled to
reconstruct — many years of trades — and the original investment was withdrawn long ago, so
what is left there has no meaningful basis to carry forward. A zero cost therefore recognises
the *whole* value of the ETH as realised gain when it is spent, which is the intended reading,
not an approximation of one. Net worth is unaffected either way, since the gain offsets the
spending to the cent.

A rule names the wallet as the source, so the far side of an arrival is recorded even though
its amount is zero. That is the same treatment both Coinbase exports get, which keeps one crypto
gain figure comparable across every venue instead of one per basis convention.

That zero basis is also what lets a purchase settled in a foreign currency balance: the lot
contributes nothing to the euro group, so the elided gain posting has only the settlement
currency left to absorb. A lot with a real cost disposed of against foreign proceeds would need
a rate this export does not give, and Beancount rejects it outright — loudly, which is right.

**Cashback is recorded but not booked.** Every purchase pays a fraction of a percent back in
WXT, Wirex's own token; the export never values it and the WXT account's stated balance is a
placeholder, so there is nothing to reconcile and no price to book it at. The amount goes into
``meta.reward_wxt`` so it stays queryable. Booking it would add a transaction per purchase, and
a commodity with no price, for a token now worth a fraction of a cent per unit.

The accompanying ``Statement_All_accounts_*.csv`` files are deliberately **not** parsed. They
cover only part of the span this feed does, round the amounts to the displayed precision, and
carry no stable row id, no merchant name and no category. They are also UTF-16 little-endian
with **no byte-order mark**, so anything sniffing for one reads them as Latin-1 and sees a null
between every character.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import date as Date
from datetime import datetime, timedelta
from decimal import Decimal
from typing import NamedTuple

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

INSTITUTION = "wirex"

#: Key under which the Wirex fiat account is expected in ``own_accounts``.
ACCOUNT_KEY = "wirex"

#: Optional ``own_accounts`` key for where units are held. Defaults to a sibling of the cash
#: account, which is only right when the two live in one branch — so a ledger that files cash
#: and holdings under different roots must say where this one goes.
POSITIONS_ACCOUNT_KEY = "wirex_positions"
POSITIONS_LEAF = "Positions"

#: Optional ``own_accounts`` key for where realised gains land. Necessarily a parser choice: a
#: disposal's gain is derived from the lots, so there is no row for a rule to claim.
GAINS_ACCOUNT_KEY = "wirex_gains"
DEFAULT_GAINS_ACCOUNT = "Income:CapitalGains:Crypto"

BOOKING = "FIFO"

#: Settlement currencies this card has been used in. Anything else is a commodity.
FIAT = frozenset({"EUR", "USD", "GBP"})

#: Cost assigned to units arriving from somewhere that does not record what they cost. See the
#: module docstring for what that does to the gain.
UNKNOWN_COST = Decimal(0)
UNKNOWN_COST_CURRENCY = "EUR"

#: Row types that debit the funding account to pay someone else.
SPEND = frozenset({"PurchasePos", "PurchaseEPos", "CardOrderFee", "DeliveryPayment"})

#: Row types carrying a single ``amount`` that only moved if the transfer completed.
ONE_WAY = frozenset({"CryptoTransferIn", "CryptoTransferOut", "SepaOut", "SepaIn"})

#: One-way types that take money out rather than bring it in.
OUTBOUND = frozenset({"CryptoTransferOut", "SepaOut"})

STAMPS = ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ")

#: The counterparty shape that marks a real Wirex account, as opposed to a card or a merchant.
ACCOUNT_SUBJECT = "AccountTransactionSubjectDto"

#: Row types whose stated account balance is a real running balance.
#:
#: Structurally indistinguishable from the rest — a card-order fee also names the account as
#: its counterparty, complete with a ``balance`` block — but on a fee row that block reads zero
#: whatever the account actually holds. Asserting it would claim the account had been emptied.
#: Only transfers and exchanges, where the account is the counterparty of record, state a
#: balance that means anything.
BALANCE_STATED = ONE_WAY | {"Exchange"}


class Money(NamedTuple):
    number: Decimal
    currency: str


class Movement(NamedTuple):
    """What one row does, split by which posting each part belongs on."""

    row: dict
    date: Date
    kind: str
    #: Signed change in units held, and whether they arrive with a known cost.
    held: Money | None = None
    #: Signed change in the Wirex fiat account.
    cash: Money | None = None
    #: Amount for the posting the rules engine fills in — a merchant, or a bank on the far side.
    counter: Money | None = None
    #: Cost of arriving units, when the export actually states one (a fiat-funded exchange).
    cost: Money | None = None


def _instant(text: str) -> datetime | None:
    text = (text or "").strip()
    for fmt in STAMPS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _stamp(text: str) -> Date | None:
    moment = _instant(text)
    return moment.date() if moment else None


def _moment(text: str) -> int:
    """The row's timestamp as microseconds, for the renderer's ordering.

    Deliberately absolute rather than a per-file counter. This export arrives as overlapping
    pagination pages, so a position in one page says nothing about position in another — and
    which page's copy of a row survives deduplication is arbitrary. A counter would therefore
    order a purchase before the transfer that funded it and leave the disposal with no lot to
    reduce, depending on nothing more than which file was read first.
    """
    moment = _instant(text)
    if moment is None:
        return 0
    return int(moment.timestamp() * 1_000_000)


def _decimal(value: object) -> Decimal | None:
    """A JSON number as an exact Decimal, via ``str`` so binary float noise is not inherited."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, str)):
        text = str(value).strip()
        if not text:
            return None
        try:
            return Decimal(text)
        except ArithmeticError:
            return None
    return None


def _money(block: object) -> Money | None:
    """Read one of the export's ``{"amount": ..., "currency": ...}`` blocks."""
    if not isinstance(block, dict):
        return None
    number = _decimal(block.get("amount"))
    currency = (block.get("currency") or "").strip()
    if number is None or not currency:
        return None
    return Money(number, currency)


def _fee(row: dict, currency: str) -> Decimal:
    """The row's fee, but only when charged in the currency being debited."""
    fee = _money(row.get("fee"))
    return fee.number if fee and fee.currency == currency else Decimal(0)


def _classify(row: dict, date: Date) -> Movement | None:
    """Decide what a row moved, or ``None`` when it moved nothing."""
    kind = (row.get("type") or "").strip()
    status = (row.get("status") or "").strip()
    base = Movement(row=row, date=date, kind=kind)

    if kind in ONE_WAY:
        single = _money(row.get("amount"))
        if status != "Success" or single is None:
            return None
        signed = -single.number if kind in OUTBOUND else single.number
        if single.currency in FIAT:
            return base._replace(cash=Money(signed, single.currency))
        return base._replace(held=Money(signed, single.currency))

    out = _money(row.get("amount_from"))
    if out is None:
        # No amount named: nothing was debited, whatever the status says.
        return None
    got = _money(row.get("amount_to"))

    if kind in SPEND:
        charged = out.number + _fee(row, out.currency)
        if out.currency in FIAT:
            # Euro left the account, so that is the expense; what the merchant was charged in
            # its own currency is metadata, since converting needs a rate not quoted here.
            return base._replace(cash=Money(-charged, out.currency))
        if got is None or got.currency not in FIAT:
            return base._replace(held=Money(-charged, out.currency))
        return base._replace(held=Money(-charged, out.currency), counter=got)

    if kind == "Refund":
        # Reads backwards from the purchase it reverses: the merchant returns fiat and the
        # account receives whatever it originally paid with.
        if got is None:
            return None
        if got.currency in FIAT:
            return base._replace(cash=got)
        return base._replace(held=got, counter=Money(-out.number, out.currency))

    if kind == "Exchange":
        if got is None:
            return None
        charged = out.number + _fee(row, out.currency)
        if out.currency in FIAT and got.currency not in FIAT:
            # The one direction where a real cost is stated, so the units get it.
            return base._replace(
                cash=Money(-charged, out.currency),
                held=got,
                cost=Money(charged, out.currency),
            )
        if out.currency not in FIAT and got.currency in FIAT:
            return base._replace(held=Money(-charged, out.currency), cash=got)
        if out.currency in FIAT and got.currency in FIAT:
            return base._replace(cash=got, counter=Money(-charged, out.currency))
        return base._replace(held=Money(-charged, out.currency))

    return None


def _party(side: object) -> tuple[str | None, str | None]:
    """A counterparty's display name and IBAN, whichever shape it arrives in."""
    if not isinstance(side, dict):
        return None, None
    information = side.get("information")
    name = None
    if isinstance(information, dict) and information.get("name"):
        name = normalize_text(str(information["name"]))
    if not name and side.get("owner_name"):
        name = normalize_text(str(side["owner_name"]))
    return name, (side.get("iban") or "").strip() or None


def _meta(move: Movement, source_file: str) -> dict[str, object]:
    row = move.row
    meta: dict[str, object] = {"institution": INSTITUTION, "type": move.kind}
    if source_file:
        meta["source_file"] = source_file
    # The row id, not `transaction_id`: eight rows carry a different value in each, and it is
    # the id that is unique per row. Deduplication across overlapping pagination pages rests
    # entirely on this.
    if row.get("id"):
        meta["transaction_id"] = str(row["id"]).strip()
    status = (row.get("status") or "").strip()
    if status:
        meta["status"] = status
    if row.get("status_reason"):
        meta["status_reason"] = str(row["status_reason"]).strip()

    for side in ("source", "target"):
        block = row.get(side)
        if not isinstance(block, dict):
            continue
        _, iban = _party(block)
        if iban:
            # Names the far side for the rules engine, which routes a movement between two of
            # the owner's own accounts through the clearing account before any rule sees it.
            meta["counterparty_iban"] = iban
        for key, field in (
            ("card_id", "card_id"),
            ("merchant_id", "merchant_id"),
            ("processing_category", "processing_category"),
            ("crypto_address", "address"),
        ):
            if block.get(field):
                meta[key] = str(block[field]).strip()
        location = block.get("location")
        if isinstance(location, dict) and location.get("full_address"):
            meta["merchant_address"] = normalize_text(str(location["full_address"]))

    category = row.get("original_category")
    if isinstance(category, dict) and category.get("name"):
        meta["category"] = normalize_text(str(category["name"]))

    details = row.get("details")
    if isinstance(details, dict):
        for key, field in (("blockchain", "blockchain"), ("tx_hash", "hash")):
            if details.get(field):
                meta[key] = str(details[field]).strip()

    # Whichever of the pair did not become a posting; see the module docstring.
    booked = {m.currency for m in (move.cash, move.counter) if m is not None}
    for block in (row.get("amount_from"), row.get("amount_to")):
        stated = _money(block)
        if stated and stated.currency in FIAT and stated.currency not in booked:
            meta["foreign_amount"] = f"{stated.number} {stated.currency}"

    # Recorded rather than booked; see the module docstring.
    reward = row.get("reward")
    if isinstance(reward, dict):
        paid = _money(reward.get("amount"))
        if paid and paid.number:
            meta["reward_wxt"] = (
                paid.number if paid.currency == "WXT" else f"{paid.number} {paid.currency}"
            )

    fee = _money(row.get("fee"))
    if fee and fee.number:
        meta["fee"] = f"{fee.number} {fee.currency}"
    if row.get("comment"):
        meta["comment"] = normalize_text(str(row["comment"]))
    return meta


#: Metadata that describes the crypto leg, and belongs only on it.
CRYPTO_META = frozenset({"blockchain", "tx_hash", "crypto_address", "fee"})

#: Metadata that describes who was paid, and belongs only on the merchant or bank leg.
COUNTERPARTY_META = frozenset(
    {
        "card_id",
        "category",
        "comment",
        "counterparty_iban",
        "foreign_amount",
        "merchant_address",
        "merchant_id",
        "processing_category",
        "reward_wxt",
    }
)


def _for_side(meta: dict[str, object], side: str, suffix: str) -> dict[str, object]:
    """The metadata belonging on one half of a split movement.

    Splitting it matters for more than tidiness: a rule keyed on a merchant identifier would
    otherwise see both halves. It cannot misroute the crypto half — that half leaves no posting
    for the rules engine to fill — but a merchant id sitting on a transaction about selling
    ether reads as though the ether had been sold *to* the merchant.
    """
    out = dict(meta)
    if suffix and out.get("transaction_id"):
        out["transaction_id"] = f"{out['transaction_id']}{suffix}"
    if side == "crypto":
        drop = COUNTERPARTY_META
    elif side == "merchant":
        drop = CRYPTO_META
    else:
        return out
    for key in drop:
        out.pop(key, None)
    return out


def _narration(move: Movement) -> tuple[str, str]:
    """Payee and narration, naming the asset so a rule can tell euro from ether."""
    name, _ = _party(move.row.get("target"))
    if not name:
        name, _ = _party(move.row.get("source"))

    asset = move.held.currency if move.held else ""
    if move.kind in ("PurchasePos", "PurchaseEPos"):
        narration = "Card payment"
    elif move.kind == "Refund":
        narration = "Refund"
    elif move.kind == "CryptoTransferIn":
        narration = f"Crypto transfer in {asset}".strip()
    elif move.kind == "CryptoTransferOut":
        narration = f"Crypto transfer out {asset}".strip()
    elif move.kind in ("SepaOut", "SepaIn"):
        narration = "Sepa transfer"
    else:
        # Exchange, CardOrderFee, DeliveryPayment, and whatever Wirex adds later.
        narration = "".join(f" {c}" if c.isupper() else c for c in move.kind).strip()

    status = (move.row.get("status") or "").strip()
    if status and status != "Success":
        # A failed row that still debited must not read like an ordinary purchase, or a rule
        # files it as spending and its later refund looks like income.
        narration = f"{narration} ({status.lower()})"
    return name or "Wirex", narration


class Entry(NamedTuple):
    """One transaction to emit. A movement becomes one of these, or two."""

    postings: tuple[Posting, ...]
    payee: str
    narration: str
    #: Distinguishes the two transactions of a split movement, in the id and the link.
    suffix: str = ""
    #: Which half of a split this is, so the right metadata lands on it.
    side: str = "both"


def _holding(move: Movement, positions: str) -> Posting:
    """The posting that changes what is held."""
    assert move.held is not None
    if move.held.number > 0:
        # Arriving. A fiat-funded exchange states the cost; anything else does not, and gets
        # zero — see the module docstring for why that is a decision and not a gap.
        stated = move.cost or Money(UNKNOWN_COST, UNKNOWN_COST_CURRENCY)
        cost = Cost(stated.number, stated.currency, None, None, total=True)
    else:
        # Leaving: the booking method picks the lot and the gain falls out of it. The currency
        # is named because a purchase settled in dollars would otherwise have Beancount infer a
        # dollar cost basis and match none of the euro-cost lots.
        cost = Cost(None, UNKNOWN_COST_CURRENCY, None, None)
    return Posting(
        account=positions,
        units=Amount(move.held.number, move.held.currency),
        cost=cost,
    )


def _entries(move: Movement, *, cash: str, positions: str, gains: str) -> list[Entry]:
    """One movement as one transaction, or as two when it is really two events.

    **A card purchase funded by crypto is two events, not one**: ether is sold, and euro is
    spent at a merchant. Booking them as a single transaction is what a naive reading of the
    row suggests — one row in, one transaction out — but it puts a realised capital gain and a
    grocery bill in the same entry, so every merchant the card ever paid shows up in the
    capital-gains account. Salary is not attached to the things it buys, and neither is this.

    So the pair is split, pivoting on the euro pocket that Wirex converts through: the sale
    lands the proceeds there, and the purchase spends them. The two net to nothing in that
    account, which is why its stated balances still reconcile, and they carry a shared link so
    the relationship stays visible rather than merely implied.

    A refund is the same shape backwards: the merchant returns euro, and ether is bought with
    it, reversing the gain the original purchase recognised.
    """
    held, fiat, counter = move.held, move.cash, move.counter

    if held is not None and counter is not None:
        # Crypto changed hands against a stated merchant amount: two events.
        proceeds = Amount(counter.number, counter.currency)
        asset = held.currency
        verb = "Sold" if held.number < 0 else "Bought"
        return [
            Entry(
                postings=(
                    _holding(move, positions),
                    Posting(account=cash, units=proceeds),
                    # The difference from the lot's cost is the realised gain.
                    Posting(account=gains, units=None),
                ),
                payee="Wirex",
                narration=f"{verb} {asset}",
                suffix=":funding",
                side="crypto",
            ),
            Entry(
                postings=(
                    Posting(account=cash, units=Amount(-counter.number, counter.currency)),
                    Posting(account=None, units=Amount(counter.number, counter.currency)),
                ),
                payee="",  # filled in by the caller from the merchant
                narration="",
                side="merchant",
            ),
        ]

    postings: list[Posting] = []
    if held is not None:
        postings.append(_holding(move, positions))
    if fiat is not None:
        postings.append(Posting(account=cash, units=Amount(fiat.number, fiat.currency)))

    priced = held is not None and fiat is not None
    if priced and move.cost is None:
        # An exchange: units sold for cash in one step, with nobody else involved.
        postings.append(Posting(account=gains, units=None))
    elif move.cost is None:
        # Nothing names the far side, so the rules engine does. One elided posting absorbs
        # whatever is left, which for a pure transfer is the lot's own cost.
        #
        # **Written as an explicit zero when there is nothing for it to absorb.** Units arriving
        # here cost zero by policy, so the transaction already balances without this posting —
        # and Beancount responds to an elided posting it cannot give a number by *deleting it*,
        # silently and without an error. The account it names then has no posting at all, so the
        # wallet the units came from goes unrecorded in the very transaction that exists to
        # record it. An explicit zero survives.
        empty = held is not None and held.number > 0
        units = Amount(UNKNOWN_COST, UNKNOWN_COST_CURRENCY) if empty else None
        postings.append(Posting(account=None, units=units))

    return [Entry(postings=tuple(postings), payee="", narration="")]


def _rows(payload: object) -> list[dict]:
    """The activity rows, from the ``{"page": {"data": [...]}}`` envelope the feed returns."""
    if isinstance(payload, dict):
        page = payload.get("page")
        if isinstance(page, dict) and isinstance(page.get("data"), list):
            return [row for row in page["data"] if isinstance(row, dict)]
        if isinstance(payload.get("data"), list):
            return [row for row in payload["data"] if isinstance(row, dict)]
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    return []


def _tolerance(block: dict) -> Decimal:
    """How far a stated balance may sit from the amounts, given how it is written.

    Wirex rounds a balance to the ``display_precision`` it declares — six places for ether —
    while the amounts themselves carry full precision, so the two disagree in the seventh
    decimal as soon as any row does. One unit in the last stated place absorbs that without
    weakening the check that matters: the smallest real purchase here is three orders of
    magnitude larger, so a dropped row still fails. The handful of one-satoshi test transfers
    are the exception, and are smaller than the tolerance by construction.
    """
    precision = block.get("display_precision")
    places = precision if isinstance(precision, int) and 0 <= precision <= 18 else 6
    return Decimal(1).scaleb(-places)


def _touches(move: Movement, account_id: str) -> bool:
    """Whether this row moved the named Wirex account."""
    for side in ("source", "target"):
        block = move.row.get(side)
        if isinstance(block, dict) and (block.get("account_id") or "").strip() == account_id:
            return True
        if isinstance(block, dict):
            nested = block.get("account")
            if isinstance(nested, dict) and (nested.get("account_id") or "").strip() == account_id:
                return True
    return False


def _anchors(moves: list[Movement]) -> list[tuple[Date, Decimal, str, Decimal]]:
    """Stated balances this file can prove, as ``(date, balance, currency, tolerance)``.

    The export paginates, the pages overlap, and a page boundary can fall inside a day — so a
    stated balance is only assertable when this file holds every row that touched the account
    that day. Anything on the file's own first or last day is skipped for exactly that reason:
    the rest of that day may be in the neighbouring page.
    """
    if not moves:
        return []
    days = [move.date for move in moves]
    first, last = min(days), max(days)

    account_ids = {
        (block.get("account_id") or "").strip()
        for move in moves
        for block in (move.row.get("source"), move.row.get("target"))
        if isinstance(block, dict) and block.get("discriminator") == ACCOUNT_SUBJECT
    } - {""}

    latest: dict[tuple[str, Date], int] = {}
    for index, move in enumerate(moves):
        for account_id in account_ids:
            if _touches(move, account_id):
                latest[(account_id, move.date)] = index

    out: list[tuple[Date, Decimal, str, Decimal]] = []
    for index, move in enumerate(moves):
        if move.date in (first, last) or move.kind not in BALANCE_STATED:
            continue
        for side in ("source", "target"):
            block = move.row.get(side)
            if not isinstance(block, dict) or block.get("discriminator") != ACCOUNT_SUBJECT:
                continue
            account_id = (block.get("account_id") or "").strip()
            if not account_id or latest.get((account_id, move.date)) != index:
                continue
            stated = _money(block.get("balance"))
            if stated is not None:
                out.append((move.date, stated.number, stated.currency, _tolerance(block)))
    return out


def parse_from_payload(
    payload: object, accounts: Accounts, *, source_file: str = ""
) -> Iterator[Directive]:
    cash = resolve_account(accounts, ACCOUNT_KEY, source=source_file or INSTITUTION)
    positions = accounts.get(
        POSITIONS_ACCOUNT_KEY.upper(), f"{cash.rpartition(':')[0]}:{POSITIONS_LEAF}"
    )
    gains = accounts.get(GAINS_ACCOUNT_KEY.upper(), DEFAULT_GAINS_ACCOUNT)

    moves: list[Movement] = []
    for row in _rows(payload):
        date = _stamp(str(row.get("created_at") or ""))
        if date is None:
            continue
        move = _classify(row, date)
        if move is not None:
            moves.append(move)

    # Source order, oldest first: the feed returns newest first, and a disposal rendered before
    # the acquisition that funded it has no lot to reduce.
    moves.sort(key=lambda move: (str(move.row.get("created_at") or ""), str(move.row.get("id"))))

    booked = False
    first: Date | None = None

    for move in moves:
        entries = _entries(move, cash=cash, positions=positions, gains=gains)
        entries = [entry for entry in entries if entry.postings]
        if not entries:
            continue
        payee, narration = _narration(move)
        meta = _meta(move, source_file)
        # Rows are not in balance order — a holding is sold the same day it arrives — so the
        # renderer is told when each row happened rather than left to sort on amount, which
        # puts every disposal before its own acquisition.
        meta["sequence"] = _moment(str(move.row.get("created_at") or ""))
        # A split movement's two halves share a link, so the sale and the purchase it funded
        # can be read as the one event they came from rather than merely coinciding.
        row_id = str(move.row.get("id") or "").strip()
        links = frozenset({f"wirex-{row_id}"}) if len(entries) > 1 and row_id else frozenset()

        if move.held is not None:
            booked = True
            first = min(first or move.date, move.date)

        for entry in entries:
            yield Transaction(
                meta=_for_side(meta, entry.side, entry.suffix),
                date=move.date,
                flag=FLAG_OKAY,
                payee=entry.payee or payee,
                narration=entry.narration or narration,
                tags=frozenset(),
                links=links,
                postings=entry.postings,
            )

    if booked and first:
        yield Open(
            meta={"institution": INSTITUTION},
            date=first,
            account=positions,
            currencies=(),
            booking=BOOKING,
        )

    # Stated by Wirex and independent of the amounts, so each one checks that every row
    # affecting that account up to that day was read. This is what catches a dropped row.
    for date, balance, currency, tolerance in _anchors(moves):
        yield Balance(
            meta={"institution": INSTITUTION, "source_file": source_file},
            date=date + timedelta(days=1),
            account=cash if currency in FIAT else positions,
            amount=Amount(balance, currency),
            tolerance=tolerance,
        )


def parse_file(path: str, accounts: Accounts) -> Iterator[Directive]:
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    yield from parse_from_payload(payload, accounts, source_file=path)
