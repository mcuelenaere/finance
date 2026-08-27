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
from typing import NamedTuple

import yaml

from finance.models import Posting, Transaction
from finance.text import normalize_identifier, normalize_text

#: Reason a posting ended up with the account it did — surfaced by ``finance review``.
REASON_RULE = "rule"
REASON_OWN_ACCOUNT = "own-account"
REASON_FALLBACK = "fallback"


class Rule(NamedTuple):
    field: str
    regex: re.Pattern[str]
    account: str


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

    return Rule(field=str(config["field"]), regex=pattern, account=str(config["account"]))


def field_value(txn: Transaction, field: str) -> str | None:
    """Read a matchable field off a transaction.

    Supported: ``payee``, ``narration``, and ``meta.<key>`` for anything a parser recorded.
    """
    if field == "payee":
        raw = txn.payee
    elif field == "narration":
        raw = txn.narration
    elif field.startswith("meta."):
        raw = txn.meta.get(field[len("meta.") :])
    else:
        raise ValueError(
            f"unknown rule field {field!r}: expected 'payee', 'narration' or 'meta.<key>'"
        )

    if raw is None:
        return None
    return normalize_text(str(raw))


class Ruleset(NamedTuple):
    rules: tuple[Rule, ...]
    own_accounts: Mapping[str, str]
    fallback_account: str
    transfer_account: str

    def account_for_identifier(self, identifier: str | None) -> str | None:
        """Resolve one of the user's own account identifiers to its Beancount account."""
        if not identifier:
            return None
        return self.own_accounts.get(normalize_identifier(identifier))

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
            value = field_value(txn, rule.field)
            if value and rule.regex.search(value):
                return Match(rule.account, REASON_RULE, rule)

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
        fallback_account=str(config.get("fallback_account", "Expenses:Unknown")),
        transfer_account=str(config.get("transfer_account", "Assets:Transfers")),
    )


def resolve_all(ruleset: Ruleset, transactions: Iterable[Transaction]) -> list[Transaction]:
    return [ruleset.resolve(txn) for txn in transactions]


def unmatched(
    ruleset: Ruleset, transactions: Iterable[Transaction]
) -> list[tuple[Transaction, Posting | None]]:
    """Transactions that fell through to the fallback account — the rule-writing queue."""
    out = []
    for txn in transactions:
        if not ruleset.classify(txn).matched:
            own = next((p for p in txn.postings if p.account is not None), None)
            out.append((txn, own))
    return out
