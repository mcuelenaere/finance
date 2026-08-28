"""The clearing-account report in `finance check`."""

from datetime import date
from decimal import Decimal

from finance.cli import _manual_postings, _transfer_report
from finance.models import FLAG_OKAY, Amount, Posting, Transaction
from finance.rules import Ruleset

TRANSFERS = "Assets:Clearing:Transfers"


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


# --- hand-written legs ------------------------------------------------------------------
# The report is built from the parsed exports, which is where the pairing logic lives. A leg no
# export contains can only be recovered by hand, and then the report goes on flagging a gap that
# is already closed — a detector that cannot be satisfied is one people learn to ignore.


def write_manual(tmp_path, *lines):
    path = tmp_path / "manual.beancount"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def test_a_hand_written_leg_is_summed(tmp_path):
    path = write_manual(
        tmp_path,
        '2019-09-04 * "ING" "Closing transfer, leg missing from the export"',
        "  Assets:Savings:ING:Second   -1355.82 EUR",
        "  Assets:Clearing:Transfers       1355.82 EUR",
    )
    totals, unreadable = _manual_postings(path, TRANSFERS)

    assert totals == {"EUR": Decimal("1355.82")}
    assert unreadable == 0


def test_currencies_are_kept_apart(tmp_path):
    path = write_manual(
        tmp_path,
        "  Assets:Clearing:Transfers    100.00 EUR",
        "  Assets:Clearing:Transfers     -40.00 USD",
        "  Assets:Clearing:Transfers      25.50 EUR",
    )
    totals, _ = _manual_postings(path, TRANSFERS)

    assert totals == {"EUR": Decimal("125.50"), "USD": Decimal("-40.00")}


def test_a_commented_out_posting_is_ignored(tmp_path):
    """The file documents alternatives in comments; they are not entries."""
    path = write_manual(
        tmp_path,
        ";; Assets:Clearing:Transfers    999.00 EUR",
        "  Assets:Clearing:Transfers      10.00 EUR",
    )
    totals, unreadable = _manual_postings(path, TRANSFERS)

    assert totals == {"EUR": Decimal("10.00")}
    assert unreadable == 0


def test_another_account_is_not_counted(tmp_path):
    """`Assets:Clearing:TransfersOld` contains the name but is a different account."""
    path = write_manual(
        tmp_path,
        "  Assets:Clearing:TransfersOld   500.00 EUR",
        "  Assets:Clearing:Transfers       10.00 EUR",
    )
    totals, _ = _manual_postings(path, TRANSFERS)

    assert totals == {"EUR": Decimal("10.00")}


def test_an_unreadable_line_is_counted_not_swallowed(tmp_path):
    """Silence is the risk of scanning instead of parsing, so it has to be visible."""
    path = write_manual(
        tmp_path,
        "  Assets:Clearing:Transfers      10.00 EUR",
        "  Assets:Clearing:Transfers   {something the scanner cannot read}",
    )
    totals, unreadable = _manual_postings(path, TRANSFERS)

    assert totals == {"EUR": Decimal("10.00")}
    assert unreadable == 1


def test_a_missing_file_is_not_an_error(tmp_path):
    totals, unreadable = _manual_postings(tmp_path / "nope.beancount", TRANSFERS)

    assert totals == {}
    assert unreadable == 0
