"""The clearing-account report in `finance check`."""

from datetime import date
from decimal import Decimal

from finance.cli import _transfer_report
from finance.models import FLAG_OKAY, Amount, Posting, Transaction
from finance.rules import Ruleset

TRANSFERS = "Assets:Transfers"


def ruleset(own_accounts=None):
    return Ruleset(
        rules=(),
        own_accounts=own_accounts or {},
        fallback_account="Expenses:Unknown",
        transfer_account=TRANSFERS,
    )


def leg(own_account, amount, counterparty_iban, day=1):
    """One side of an internal transfer, as a parser plus the rules engine would emit it."""
    return Transaction(
        meta={"counterparty_iban": counterparty_iban},
        date=date(2024, 3, day),
        flag=FLAG_OKAY,
        payee=None,
        narration="Transfer",
        tags=frozenset(),
        links=frozenset(),
        postings=(
            Posting(account=own_account, units=Amount(Decimal(amount), "EUR")),
            # Elided, exactly as the rules engine leaves it — the report has to reconstruct it.
            Posting(account=TRANSFERS, units=None),
        ),
    )


OWN = {"BE11": "Assets:A", "BE22": "Assets:B"}


def test_a_fully_paired_transfer_settles(capsys):
    rules = ruleset(OWN)
    directives = [leg("Assets:A", "-100", "BE22"), leg("Assets:B", "100", "BE11", day=3)]

    assert _transfer_report(rules, directives) == 0
    assert "settles to zero" in capsys.readouterr().out


def test_balanced_route_is_omitted_from_the_breakdown(capsys):
    """A paired route must not be listed as two scary-looking halves.

    Keying the breakdown on the counterparty rather than the route did exactly that: an
    internal transfer is reported once from each side, so a perfectly balanced pair showed up
    as +100 and -100 against two different counterparties.
    """
    rules = ruleset({**OWN, "BE33": "Assets:C"})
    directives = [
        # balanced pair — should not appear
        leg("Assets:A", "-100", "BE22"),
        leg("Assets:B", "100", "BE11", day=3),
        # unpaired single leg — should appear
        leg("Assets:A", "-40", "BE33", day=5),
    ]

    assert _transfer_report(rules, directives) == 1
    out = capsys.readouterr().out

    assert "Assets:A <-> Assets:C" in out
    assert "Assets:A <-> Assets:B" not in out
    assert "1 balanced route(s) omitted" in out


def test_unpaired_leg_is_attributed_to_its_route(capsys):
    rules = ruleset({**OWN, "BE33": "Assets:C"})

    assert _transfer_report(rules, [leg("Assets:A", "-40.00", "BE33")]) == 1
    out = capsys.readouterr().out

    assert "40.00 EUR" in out
    assert "(1 leg)" in out  # singular


def test_unknown_counterparty_still_reported(capsys):
    """A leg whose counterparty is not in own_accounts must not vanish from the report."""
    assert _transfer_report(ruleset(OWN), [leg("Assets:A", "-40", "BE99 8888 7777 6666")]) == 1

    assert "BE998…666" in capsys.readouterr().out
