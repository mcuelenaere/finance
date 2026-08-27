from datetime import date
from decimal import Decimal

import pytest

from finance.models import FLAG_OKAY, Amount, Posting, Transaction
from finance.rules import (
    REASON_FALLBACK,
    REASON_OWN_ACCOUNT,
    REASON_RULE,
    Ruleset,
    parse_rule,
)


def txn(payee=None, narration="", **meta):
    return Transaction(
        meta={"institution": "test", **meta},
        date=date(2024, 3, 1),
        flag=FLAG_OKAY,
        payee=payee,
        narration=narration,
        tags=frozenset(),
        links=frozenset(),
        postings=(
            Posting(account="Assets:Belfius:Checking", units=Amount(Decimal("-10"), "EUR")),
            Posting(account=None, units=None),
        ),
    )


def ruleset(*rules, own_accounts=None):
    return Ruleset(
        rules=tuple(parse_rule(rule) for rule in rules),
        own_accounts=own_accounts or {},
        fallback_account="Expenses:Unknown",
        transfer_account="Assets:Transfers",
    )


def test_exact_value_rule():
    rules = ruleset({"field": "payee", "value": "DELHAIZE", "account": "Expenses:Food"})

    assert rules.classify(txn(payee="DELHAIZE")).account == "Expenses:Food"
    assert rules.classify(txn(payee="DELHAIZE GENT")).account == "Expenses:Unknown"


def test_regex_rule():
    rules = ruleset({"field": "payee", "regex": "^F.O.D.", "account": "Expenses:Taxes"})

    assert rules.classify(txn(payee="F.O.D. FINANCIEN")).reason == REASON_RULE
    assert rules.classify(txn(payee="OTHER")).reason == REASON_FALLBACK


def test_rules_match_on_metadata():
    rules = ruleset(
        {"field": "meta.counterparty_iban", "value": "BE99", "account": "Expenses:Home:Rent"}
    )

    assert rules.classify(txn(counterparty_iban="BE99")).account == "Expenses:Home:Rent"


def test_whitespace_is_collapsed_before_matching():
    """Bank exports pad fields; rules are written against the readable form."""
    rules = ruleset({"field": "payee", "value": "PROXY DELHAIZE", "account": "Expenses:Food"})

    assert rules.classify(txn(payee="PROXY    DELHAIZE")).account == "Expenses:Food"


def test_first_matching_rule_wins():
    rules = ruleset(
        {"field": "payee", "regex": "DELHAIZE", "account": "Expenses:Food"},
        {"field": "payee", "regex": "GENT", "account": "Expenses:Other"},
    )

    assert rules.classify(txn(payee="DELHAIZE GENT")).account == "Expenses:Food"


def test_own_accounts_beat_rules():
    """A movement between the user's own accounts must never read as an expense.

    That is the one misclassification that silently inflates net worth, so the own-accounts
    registry is checked before any rule — even a rule that would otherwise match.
    """
    rules = ruleset(
        {"field": "payee", "regex": ".*", "account": "Expenses:Food"},
        own_accounts={"BE12345678901234": "Assets:Belfius:Savings"},
    )

    match = rules.classify(txn(payee="ANYTHING", counterparty_iban="BE12 3456 7890 1234"))
    assert match.account == "Assets:Transfers"
    assert match.reason == REASON_OWN_ACCOUNT


def test_own_account_matching_ignores_iban_spacing():
    rules = ruleset(own_accounts={"BE12345678901234": "Assets:Belfius:Savings"})

    assert rules.account_for_identifier("BE12 3456 7890 1234") == "Assets:Belfius:Savings"
    assert rules.account_for_identifier("BE12345678901234") == "Assets:Belfius:Savings"
    assert rules.account_for_identifier("BE00000000000000") is None


def test_resolve_fills_the_unresolved_posting():
    rules = ruleset({"field": "payee", "value": "DELHAIZE", "account": "Expenses:Food"})

    resolved = rules.resolve(txn(payee="DELHAIZE"))

    assert [p.account for p in resolved.postings] == [
        "Assets:Belfius:Checking",
        "Expenses:Food",
    ]
    # The elided amount is left for Beancount to balance.
    assert resolved.postings[1].units is None


def test_resolve_rejects_ambiguity():
    """Two unresolved postings cannot be filled from a single classification."""
    rules = ruleset()
    ambiguous = txn()._replace(
        postings=(Posting(account=None, units=None), Posting(account=None, units=None))
    )

    with pytest.raises(ValueError, match="2 unresolved postings"):
        rules.resolve(ambiguous)


@pytest.mark.parametrize(
    "config,message",
    [
        ({"field": "payee", "account": "X"}, "either value or regex"),
        ({"field": "payee", "value": "a", "regex": "b", "account": "X"}, "both value and regex"),
        ({"value": "a", "account": "X"}, "missing 'field'"),
        ({"field": "payee", "value": "a"}, "missing 'account'"),
    ],
)
def test_invalid_rule_configs(config, message):
    with pytest.raises(ValueError, match=message):
        parse_rule(config)


def test_unknown_field_is_rejected():
    rules = ruleset({"field": "nonsense", "value": "x", "account": "X"})

    with pytest.raises(ValueError, match="unknown rule field"):
        rules.classify(txn(payee="x"))
