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


def txn(payee=None, narration="", amount="-10", **meta):
    return Transaction(
        meta={"institution": "test", **meta},
        date=date(2024, 3, 1),
        flag=FLAG_OKAY,
        payee=payee,
        narration=narration,
        tags=frozenset(),
        links=frozenset(),
        postings=(
            Posting(account="Assets:Belfius:Checking", units=Amount(Decimal(amount), "EUR")),
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


def test_own_account_matched_by_either_spelling():
    """Belfius names an ING account by IBAN; ING names it domestically. Both are the account.

    Declaring one spelling and missing the other silently books the account holder's own
    transfers as expenses — the error that inflates net worth with nothing looking broken.
    """
    domestic = ruleset(own_accounts={"123-4567890-12": "Assets:ING:Checking"})
    by_iban = ruleset(own_accounts={"BE53123456789012": "Assets:ING:Checking"})

    for rules in (domestic, by_iban):
        assert rules.account_for_identifier("123-4567890-12") == "Assets:ING:Checking"
        assert rules.account_for_identifier("BE53 1234 5678 9012") == "Assets:ING:Checking"


def test_transfer_detected_when_the_export_uses_the_other_spelling():
    rules = ruleset(
        {"field": "payee", "regex": ".*", "account": "Expenses:Food"},
        own_accounts={"123-4567890-12": "Assets:ING:Checking"},
    )

    match = rules.classify(txn(payee="MY OWN NAME", counterparty_iban="BE53 1234 5678 9012"))
    assert match.reason == REASON_OWN_ACCOUNT


def test_iban_rule_matches_however_the_export_spaces_it():
    """Banks write the same IBAN spaced or unspaced; a rule author cannot know which.

    Without this, an IBAN rule written in one form silently claims nothing when the export
    uses the other — and a silent non-match looks identical to having no rule at all.
    """
    unspaced = ruleset(
        {"field": "meta.counterparty_iban", "value": "BE53123456789012", "account": "Expenses:X"}
    )

    for spelling in ("BE53123456789012", "BE53 1234 5678 9012"):
        assert unspaced.classify(txn(counterparty_iban=spelling)).account == "Expenses:X"


def test_text_field_searches_every_descriptive_field():
    """The same memo lands in a different field depending on the bank.

    A transfer memo is the narration in one export, the description in another and a detail
    block in a third. Without `text`, one fact needs the same rule written three times — and
    the copy that is missing is invisible, since a non-matching rule looks like no rule.
    """
    rules = ruleset({"field": "text", "regex": "(?i)share sale", "account": "Assets:X"})

    assert rules.classify(txn(narration="Share Sale Realo")).account == "Assets:X"
    assert rules.classify(txn(description="Share Sale Realo")).account == "Assets:X"
    assert rules.classify(txn(detail="Mededeling: share sale realo")).account == "Assets:X"
    assert rules.classify(txn(payee="Share Sale Ltd")).account == "Assets:X"
    assert rules.classify(txn(narration="something else")).account == "Expenses:Unknown"


def test_unknown_field_names_text_as_an_option():
    rules = ruleset({"field": "nonsense", "value": "x", "account": "X"})

    with pytest.raises(ValueError, match="'text'"):
        rules.classify(txn(payee="x"))


# --- direction ------------------------------------------------------------------------
# An income rule keyed on a counterparty claims everything that counterparty is involved in,
# including money going the other way, which quietly turns a payment into negative income.
# `direction` lets a rule say which way its money can only ever move.


def test_direction_in_ignores_money_going_out():
    rules = ruleset(
        {
            "field": "payee",
            "value": "EMPLOYER",
            "account": "Income:Salary",
            "direction": "in",
        }
    )

    assert rules.classify(txn(payee="EMPLOYER", amount="2000")).account == "Income:Salary"
    assert rules.classify(txn(payee="EMPLOYER", amount="-50")).account == "Expenses:Unknown"


def test_direction_out_ignores_money_coming_in():
    rules = ruleset(
        {"field": "payee", "value": "SHOP", "account": "Expenses:Food", "direction": "out"}
    )

    assert rules.classify(txn(payee="SHOP", amount="-10")).account == "Expenses:Food"
    # Falls through to the fallback, and an inflow's fallback is on the income side.
    assert rules.classify(txn(payee="SHOP", amount="25")).account == "Income:Unknown"


def test_a_rule_without_direction_still_claims_both_ways():
    """Refunds legitimately reverse the income or expense they came from."""
    rules = ruleset({"field": "payee", "value": "SHOP", "account": "Expenses:Food"})

    assert rules.classify(txn(payee="SHOP", amount="-10")).account == "Expenses:Food"
    assert rules.classify(txn(payee="SHOP", amount="25")).account == "Expenses:Food"


def test_a_constrained_rule_declines_when_the_amount_is_unknown():
    """Better to fall through to review than to guess which way nothing moved."""
    rules = ruleset(
        {"field": "payee", "value": "EMPLOYER", "account": "Income:Salary", "direction": "in"}
    )
    without_amount = txn(payee="EMPLOYER")._replace(
        postings=(Posting(account="Assets:Belfius:Checking", units=None),)
    )

    assert rules.classify(without_amount).account == "Expenses:Unknown"


def test_a_later_rule_can_still_claim_what_a_direction_rule_declined():
    rules = ruleset(
        {
            "field": "payee",
            "value": "EMPLOYER",
            "account": "Income:Salary",
            "direction": "in",
        },
        {"field": "payee", "value": "EMPLOYER", "account": "Expenses:Unknown:Employer"},
    )

    assert rules.classify(txn(payee="EMPLOYER", amount="2000")).account == "Income:Salary"
    assert (
        rules.classify(txn(payee="EMPLOYER", amount="-50")).account == "Expenses:Unknown:Employer"
    )


def test_an_unknown_direction_is_rejected():
    with pytest.raises(ValueError, match="direction"):
        parse_rule(
            {"field": "payee", "value": "X", "account": "Income:Salary", "direction": "sideways"}
        )


# --- the fallback follows the direction too --------------------------------------------
# One fallback for both directions nets unclassified inflow against unclassified outflow, so
# the bucket reads far smaller than the work left in it, and money arriving disappears from
# the income statement as a negative expense.


def test_unclassified_money_out_falls_back_to_expenses():
    rules = ruleset()

    assert rules.classify(txn(payee="MYSTERY", amount="-40")).account == "Expenses:Unknown"


def test_unclassified_money_in_falls_back_to_income():
    rules = ruleset()

    assert rules.classify(txn(payee="MYSTERY", amount="40")).account == "Income:Unknown"


def test_both_fallbacks_still_count_as_unmatched():
    """Either way it is work outstanding, so the review queue must see both."""
    rules = ruleset()

    for amount in ("-40", "40"):
        assert rules.classify(txn(payee="MYSTERY", amount=amount)).reason == REASON_FALLBACK
        assert not rules.classify(txn(payee="MYSTERY", amount=amount)).matched


def test_an_amountless_transaction_keeps_the_expense_fallback():
    """Nothing to judge the direction by, so the historical default stands."""
    rules = ruleset()
    without_amount = txn(payee="MYSTERY")._replace(
        postings=(Posting(account="Assets:Belfius:Checking", units=None),)
    )

    assert rules.classify(without_amount).account == "Expenses:Unknown"


def test_both_fallbacks_are_configurable():
    rules = ruleset()._replace(
        fallback_account="Expenses:TODO", income_fallback_account="Income:TODO"
    )

    assert rules.classify(txn(payee="X", amount="-1")).account == "Expenses:TODO"
    assert rules.classify(txn(payee="X", amount="1")).account == "Income:TODO"
