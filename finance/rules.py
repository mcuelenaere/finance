"""Deterministic assignment of Beancount accounts to unresolved postings.

This is the successor to the old ``categorizer`` module. It keeps that module's
regex-over-a-named-field design, which worked well, and changes two things: rules resolve to
a Beancount *account* rather than a freeform category string, and an ``own_accounts``
registry routes transfers between the user's own accounts automatically.

**No model is ever consulted here.** Rules are authored ahead of time (by a human or, more
usually, by an agent reading ``finance review``) and committed as text; this module only
applies them. That is what makes the ledger reproducible.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from decimal import Decimal
from typing import NamedTuple

import yaml

from finance.iban import variants
from finance.models import Posting, Transaction
from finance.text import normalize_identifier, normalize_text

#: Reason a posting ended up with the account it did — surfaced by ``finance review``.
REASON_RULE = "rule"
REASON_OWN_ACCOUNT = "own-account"
REASON_FALLBACK = "fallback"

#: Fallback accounts, split by direction — see :attr:`Ruleset.income_fallback_account`.
DEFAULT_FALLBACK = "Expenses:Unknown"
DEFAULT_INCOME_FALLBACK = "Income:Unknown"


#: A rule may require the money to be moving a particular way. ``in`` claims only what the
#: account received, ``out`` only what it paid.
DIRECTION_IN = "in"
DIRECTION_OUT = "out"
DIRECTIONS = (DIRECTION_IN, DIRECTION_OUT)


class Rule(NamedTuple):
    field: str
    regex: re.Pattern[str]
    account: str
    direction: str | None = None


class Match(NamedTuple):
    account: str
    reason: str
    rule: Rule | None = None

    @property
    def matched(self) -> bool:
        """False only when nothing claimed the transaction and the fallback was used."""
        return self.reason != REASON_FALLBACK


def parse_rule(config: Mapping[str, object]) -> Rule:
    """Build a Rule from its config form.

    ``value`` matches the whole (whitespace-collapsed) field exactly; ``regex`` is applied
    as a search. Exactly one must be given.
    """
    value, regex = config.get("value"), config.get("regex")
    if value and regex:
        raise ValueError(f"rule has both value and regex: {config!r}")
    if value:
        pattern = re.compile(rf"^{re.escape(normalize_text(str(value)))}$")
    elif regex:
        pattern = re.compile(str(regex))
    else:
        raise ValueError(f"rule needs either value or regex: {config!r}")

    for key in ("field", "account"):
        if not config.get(key):
            raise ValueError(f"rule is missing {key!r}: {config!r}")

    direction = config.get("direction")
    if direction is not None and direction not in DIRECTIONS:
        raise ValueError(
            f"rule has direction {direction!r}, expected one of {DIRECTIONS}: {config!r}"
        )

    return Rule(
        field=str(config["field"]),
        regex=pattern,
        account=str(config["account"]),
        direction=str(direction) if direction else None,
    )


def movement(txn: Transaction) -> Decimal | None:
    """How much the account this statement belongs to gained (+) or paid (-).

    Read off the first posting carrying an amount, which every parser fills with the leg
    belonging to the account the file came from. That holds for anything a rule can be asked
    about: a transaction with no unresolved posting never reaches :func:`Ruleset.classify`.
    Returns ``None`` when there is no such amount, which a direction-constrained rule treats
    as "not mine" rather than guessing.
    """
    for posting in txn.postings:
        if posting.units is not None:
            return posting.units.number
    return None


def _direction_holds(rule: Rule, txn: Transaction) -> bool:
    """Whether ``txn`` moves money the way ``rule`` requires. Unconstrained rules always pass."""
    if rule.direction is None:
        return True
    amount = movement(txn)
    if amount is None:
        return False
    return amount > 0 if rule.direction == DIRECTION_IN else amount < 0


#: Fields that ``text`` searches, in the order a human would read them.
TEXT_FIELDS = ("payee", "narration", "description", "detail", "message")


def field_value(txn: Transaction, field: str) -> str | None:
    """Read a matchable field off a transaction.

    Supported: ``payee``, ``narration``, ``meta.<key>`` for anything a parser recorded, and
    ``text`` for all of the descriptive fields at once.

    ``text`` exists because the same information lands in a different field depending on the
    bank: a transfer memo is the narration in one export, the description in another and a
    detail block in a third. Without it, one fact needs the same rule written three times, and
    the copy that is missing is invisible.
    """
    if field == "text":
        parts = [txn.payee or "", txn.narration or ""]
        parts += [str(txn.meta.get(key, "")) for key in TEXT_FIELDS[2:]]
        joined = normalize_text(" ".join(parts))
        return joined or None

    if field == "payee":
        raw = txn.payee
    elif field == "narration":
        raw = txn.narration
    elif field.startswith("meta."):
        raw = txn.meta.get(field[len("meta.") :])
    else:
        raise ValueError(
            f"unknown rule field {field!r}: expected 'payee', 'narration', 'text' or 'meta.<key>'"
        )

    if raw is None:
        return None
    return normalize_text(str(raw))


def _matches(rule: Rule, value: str) -> bool:
    """Whether ``rule`` claims ``value``, ignoring how an identifier happens to be spaced.

    Banks write the same IBAN both spaced and unspaced, and a rule author has no way to know
    which a given export used. Trying the space-free form too means a rule written either way
    matches either way, rather than silently claiming nothing.
    """
    if rule.regex.search(value):
        return True
    compact = value.replace(" ", "")
    return compact != value and bool(rule.regex.search(compact))


class Ruleset(NamedTuple):
    rules: tuple[Rule, ...]
    own_accounts: Mapping[str, str]
    fallback_account: str
    transfer_account: str
    #: Where unclassified money *arriving* goes. A single fallback for both directions nets
    #: inflow against outflow, so the bucket reads far smaller than the work left in it and
    #: unattributed income vanishes from the income statement entirely — booked as a negative
    #: expense, which is the one place nobody looks for income.
    income_fallback_account: str = DEFAULT_INCOME_FALLBACK

    def account_for_identifier(self, identifier: str | None) -> str | None:
        """Resolve one of the user's own account identifiers to its Beancount account.

        Every spelling of a Belgian account is tried, because which one an export uses is the
        bank's choice rather than the account holder's — see :mod:`finance.iban`.
        """
        if not identifier:
            return None
        for spelling in variants(normalize_identifier(identifier)):
            account = self.own_accounts.get(spelling)
            if account is not None:
                return account
        return None

    def classify(self, txn: Transaction) -> Match:
        """Decide which account belongs on the unresolved side of ``txn``.

        ``own_accounts`` is checked *first*, deliberately. A movement between two accounts
        the user owns must never be classified as an expense — that is the one error class
        that silently inflates net worth, so it takes precedence over any rule.
        """
        counterparty = txn.meta.get("counterparty_iban")
        if isinstance(counterparty, str) and self.account_for_identifier(counterparty):
            return Match(self.transfer_account, REASON_OWN_ACCOUNT)

        for rule in self.rules:
            if not _direction_holds(rule, txn):
                continue
            value = field_value(txn, rule.field)
            if value and _matches(rule, value):
                return Match(rule.account, REASON_RULE, rule)

        amount = movement(txn)
        if amount is not None and amount > 0:
            return Match(self.income_fallback_account, REASON_FALLBACK)
        return Match(self.fallback_account, REASON_FALLBACK)

    def resolve(self, txn: Transaction) -> Transaction:
        """Return ``txn`` with its unresolved posting filled in."""
        unresolved = [i for i, p in enumerate(txn.postings) if p.account is None]
        if not unresolved:
            return txn
        if len(unresolved) > 1:
            raise ValueError(
                f"{txn.date} {txn.narration!r} has {len(unresolved)} unresolved postings; "
                "a parser must leave at most one for the rules engine to fill"
            )

        index = unresolved[0]
        account = self.classify(txn).account
        postings = list(txn.postings)
        postings[index] = postings[index]._replace(account=account)
        return txn._replace(postings=tuple(postings))


def load_ruleset(path: str) -> Ruleset:
    with open(path, encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}

    own_accounts = {
        normalize_identifier(str(identifier)): str(account)
        for identifier, account in (config.get("own_accounts") or {}).items()
    }

    return Ruleset(
        rules=tuple(parse_rule(rule) for rule in (config.get("rules") or [])),
        own_accounts=own_accounts,
        fallback_account=str(config.get("fallback_account", DEFAULT_FALLBACK)),
        transfer_account=str(config.get("transfer_account", "Assets:Transfers")),
        income_fallback_account=str(config.get("income_fallback_account", DEFAULT_INCOME_FALLBACK)),
    )


def resolve_all(ruleset: Ruleset, transactions: Iterable[Transaction]) -> list[Transaction]:
    return [ruleset.resolve(txn) for txn in transactions]


def needs_rule(txn: Transaction) -> bool:
    """Whether any posting is still waiting for the rules engine.

    A parser that already knows both sides of a transaction — a DEGIRO cash sweep, say —
    leaves nothing to classify. Counting those as unmatched pads the review queue with
    hundreds of rows no rule can improve.
    """
    return any(posting.account is None for posting in txn.postings)


def unmatched(
    ruleset: Ruleset, transactions: Iterable[Transaction]
) -> list[tuple[Transaction, Posting | None]]:
    """Transactions that fell through to the fallback account — the rule-writing queue."""
    out = []
    for txn in transactions:
        if needs_rule(txn) and not ruleset.classify(txn).matched:
            own = next((p for p in txn.postings if p.account is not None), None)
            out.append((txn, own))
    return out
