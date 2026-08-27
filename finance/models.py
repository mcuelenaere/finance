"""Intermediate representation of a ledger, mirroring ``beancount.core.data``.

Field names and their order match Beancount's own namedtuples. That is deliberate:

* the renderer stays a dumb serializer with no logic of its own,
* the structs map 1:1 onto the output format (and onto rustledger, which is
  format-compatible),
* converting to real ``beancount`` objects later is a rename, not a rewrite.

Because Beancount puts ``meta`` first and defines no defaults, **construct these with
keyword arguments**. That keeps call sites readable without sacrificing the field order.

Two conventions carry meaning beyond Beancount's own:

* ``Posting.account is None`` means *unresolved* — the parser could not know which account
  belongs on the other side, and the rules engine is expected to fill it in.
* ``Posting.units is None`` means *elided*. Beancount auto-balances a single missing amount
  within a transaction, so the renderer usually does not need to compute it.
"""

from __future__ import annotations

from datetime import date as Date
from decimal import Decimal
from typing import NamedTuple

# Beancount entry/posting flags.
FLAG_OKAY = "*"
FLAG_PENDING = "!"

#: Beancount metadata. Values are rendered as strings, numbers or dates.
type Meta = dict[str, object]


class Amount(NamedTuple):
    number: Decimal
    currency: str


class Cost(NamedTuple):
    #: None renders as Beancount's empty cost spec ``{}``, which asks the booking method to
    #: choose the lot rather than naming it. Required on disposals: a broker export says what
    #: was sold, never which lot it came from.
    number: Decimal | None
    currency: str | None
    date: Date | None
    label: str | None
    #: True renders Beancount's ``{{total}}`` rather than ``{per-unit}``. A quoted unit price
    #: times a quantity often misses the cash actually paid by a fraction of a cent, and a
    #: transaction that does not balance to the cent is rejected outright.
    total: bool = False


class Posting(NamedTuple):
    account: str | None
    units: Amount | None
    cost: Cost | None = None
    price: Amount | None = None
    flag: str | None = None
    meta: Meta | None = None


class Transaction(NamedTuple):
    meta: Meta
    date: Date
    flag: str
    payee: str | None
    narration: str
    tags: frozenset[str]
    links: frozenset[str]
    postings: tuple[Posting, ...]


class Balance(NamedTuple):
    meta: Meta
    date: Date
    account: str
    amount: Amount
    #: Permitted divergence, rendered as Beancount's ``~ tolerance`` suffix. Beancount's own
    #: Balance carries this field too; it defaults to None here purely for call-site brevity.
    tolerance: Decimal | None = None


class Open(NamedTuple):
    meta: Meta
    date: Date
    account: str
    currencies: tuple[str, ...]
    #: Lot-matching method for disposals. Beancount defaults to STRICT, which requires a sale
    #: to name a lot exactly; a broker export never does, so position accounts need "FIFO".
    booking: str | None = None


class Price(NamedTuple):
    meta: Meta
    date: Date
    currency: str
    amount: Amount


#: Disposal at whatever lot the booking method selects.
EMPTY_COST = Cost(None, None, None, None)

#: Anything the renderer knows how to serialize.
type Directive = Transaction | Balance | Open | Price


def is_resolved(txn: Transaction) -> bool:
    """True when every posting has an account assigned."""
    return all(posting.account is not None for posting in txn.postings)


def posting_amounts(txn: Transaction) -> list[tuple[str | None, Amount]]:
    """Return every posting's ``(account, amount)``, filling in a single elided amount.

    Most generated transactions leave the counter-posting's amount to Beancount, which balances
    it automatically. Anything that wants to *sum* postings — the transfer clearing report, for
    instance — therefore has to reconstruct it, or it silently sees nothing and reports a total
    of zero.

    Only the unambiguous case is inferred: exactly one elided posting, and the remaining
    postings all in one currency. Anything else is left out rather than guessed at.
    """
    out: list[tuple[str | None, Amount]] = []
    elided = [i for i, posting in enumerate(txn.postings) if posting.units is None]

    for posting in txn.postings:
        if posting.units is not None:
            out.append((posting.account, posting.units))

    if len(elided) == 1:
        currencies = {amount.currency for _, amount in out}
        if len(currencies) == 1:
            currency = currencies.pop()
            residual = sum((amount.number for _, amount in out), start=Decimal(0))
            out.append((txn.postings[elided[0]].account, Amount(-residual, currency)))

    return out
