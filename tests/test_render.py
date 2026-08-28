"""Renderer output, including the golden-file check.

Because ``generated.beancount`` is a committed build artifact, silent changes to this output
would show up as noisy diffs across the whole ledger. The golden test below is the guard.
"""

from datetime import date
from decimal import Decimal

import pytest

from finance.models import (
    FLAG_OKAY,
    FLAG_PENDING,
    Amount,
    Balance,
    Cost,
    Open,
    Posting,
    Price,
    Transaction,
)
from finance.render import render, render_directive


def simple_txn(**overrides):
    defaults = dict(
        meta={},
        date=date(2024, 3, 1),
        flag=FLAG_OKAY,
        payee="DELHAIZE",
        narration="Groceries",
        tags=frozenset(),
        links=frozenset(),
        postings=(
            Posting(account="Assets:Belfius:Checking", units=Amount(Decimal("-12.34"), "EUR")),
            Posting(account="Expenses:Food", units=None),
        ),
    )
    return Transaction(**{**defaults, **overrides})


GOLDEN = """\
;; test ledger

2024-03-01 open Assets:Belfius:Checking EUR

2024-03-01 * "DELHAIZE" "Groceries"
  Assets:Belfius:Checking                          -12.34 EUR
  Expenses:Food

2024-03-02 balance Assets:Belfius:Checking         100.00 EUR

2024-03-02 price VWCE 98.42 EUR
"""


def test_golden_file():
    directives = [
        simple_txn(),
        Open(
            meta={}, date=date(2024, 3, 1), account="Assets:Belfius:Checking", currencies=("EUR",)
        ),
        Balance(
            meta={},
            date=date(2024, 3, 2),
            account="Assets:Belfius:Checking",
            amount=Amount(Decimal("100.00"), "EUR"),
        ),
        Price(
            meta={}, date=date(2024, 3, 2), currency="VWCE", amount=Amount(Decimal("98.42"), "EUR")
        ),
    ]

    assert render(directives, header="test ledger") == GOLDEN


def test_render_is_order_independent():
    """Input order must not affect output — otherwise diffs are meaningless."""
    a = simple_txn(narration="A")
    b = simple_txn(narration="B", date=date(2024, 3, 2))

    assert render([a, b]) == render([b, a])


def test_large_numbers_never_use_scientific_notation():
    """`Decimal.normalize()` would turn 1000 into 1E+3, which Beancount cannot parse."""
    txn = simple_txn(
        postings=(
            Posting(account="Assets:X", units=Amount(Decimal("1000"), "EUR")),
            Posting(account="Expenses:Y", units=None),
        )
    )

    assert "1000 EUR" in render_directive(txn)
    assert "E+" not in render_directive(txn)


def test_quantization_is_preserved():
    txn = simple_txn(
        postings=(
            Posting(account="Assets:X", units=Amount(Decimal("4.40"), "EUR")),
            Posting(account="Expenses:Y", units=None),
        )
    )

    assert "4.40 EUR" in render_directive(txn)


def test_quotes_in_payee_are_escaped():
    txn = simple_txn(payee='CAFE "THE SPOT"', narration="a \\ b")
    out = render_directive(txn)

    assert r'"CAFE \"THE SPOT\""' in out
    assert r'"a \\ b"' in out


def test_payee_less_transaction_emits_one_string():
    """Beancount reads a lone string as the narration, so an empty payee must be omitted."""
    out = render_directive(simple_txn(payee=None, narration="Interest"))

    assert out.splitlines()[0] == '2024-03-01 * "Interest"'


def test_metadata_is_sorted_and_typed():
    txn = simple_txn(
        meta={"source_file": "a.csv", "entry_number": "7", "amount-seen": Decimal("1.5")}
    )
    lines = render_directive(txn).splitlines()

    assert lines[1:4] == [
        "  amount-seen: 1.5",
        '  entry_number: "7"',
        '  source_file: "a.csv"',
    ]


def test_cost_and_price():
    txn = simple_txn(
        postings=(
            Posting(
                account="Assets:Investments:VWCE",
                units=Amount(Decimal("12"), "VWCE"),
                cost=Cost(Decimal("98.42"), "EUR", date(2024, 3, 1), None),
                price=Amount(Decimal("99.00"), "EUR"),
            ),
            Posting(account="Assets:Investments:Cash", units=None),
        )
    )
    out = render_directive(txn)

    assert "12 VWCE {98.42 EUR, 2024-03-01} @ 99.00 EUR" in out


def test_posting_flag_is_emitted():
    txn = simple_txn(
        postings=(
            Posting(
                account="Assets:Edenred:Meal",
                units=Amount(Decimal("-4.40"), "EUR"),
                flag=FLAG_PENDING,
            ),
            Posting(account="Expenses:Food", units=None),
        )
    )

    assert "  ! Assets:Edenred:Meal" in render_directive(txn)


def test_unresolved_posting_is_a_render_error():
    """Rendering must fail loudly rather than emit a ledger with a missing account."""
    txn = simple_txn(
        postings=(
            Posting(account=None, units=Amount(Decimal("-1"), "EUR")),
            Posting(account="Expenses:Food", units=None),
        )
    )

    with pytest.raises(ValueError, match="unresolved"):
        render_directive(txn)


def test_invalid_metadata_key_is_rejected():
    with pytest.raises(ValueError, match="invalid Beancount metadata key"):
        render_directive(simple_txn(meta={"Not Valid": "x"}))


def test_posting_amounts_fills_in_the_elided_amount():
    """Anything summing postings must reconstruct elided amounts or it sees nothing.

    The transfer clearing report hit exactly this: every rules-assigned counter-posting has
    `units=None`, so the report totalled zero and always claimed the account had settled.
    """
    from finance.models import posting_amounts

    txn = simple_txn(
        postings=(
            Posting(account="Assets:Belfius:Checking", units=Amount(Decimal("-1000"), "EUR")),
            Posting(account="Assets:Transfers", units=None),
        )
    )

    amounts = dict(posting_amounts(txn))
    assert amounts["Assets:Belfius:Checking"].number == Decimal("-1000")
    assert amounts["Assets:Transfers"].number == Decimal("1000")


def test_posting_amounts_declines_to_guess_when_ambiguous():
    """Two elided postings, or a multi-currency residual, cannot be inferred."""
    from finance.models import posting_amounts

    two_elided = simple_txn(
        postings=(
            Posting(account="A", units=Amount(Decimal("-10"), "EUR")),
            Posting(account="B", units=None),
            Posting(account="C", units=None),
        )
    )
    assert len(posting_amounts(two_elided)) == 1

    multi_currency = simple_txn(
        postings=(
            Posting(account="A", units=Amount(Decimal("-10"), "EUR")),
            Posting(account="B", units=Amount(Decimal("11"), "USD")),
            Posting(account="C", units=None),
        )
    )
    assert len(posting_amounts(multi_currency)) == 2


def test_open_renders_a_booking_method():
    """Position accounts need FIFO: a broker export never names the lot being sold."""
    out = render_directive(
        Open(
            meta={},
            date=date(2024, 3, 1),
            account="Assets:Investments:Brokers:Degiro:Positions",
            currencies=(),
            booking="FIFO",
        )
    )

    assert out == '2024-03-01 open Assets:Investments:Brokers:Degiro:Positions "FIFO"'


def test_open_without_a_booking_method_is_unchanged():
    out = render_directive(
        Open(meta={}, date=date(2024, 3, 1), account="Assets:X", currencies=("EUR",))
    )

    assert out == "2024-03-01 open Assets:X EUR"


def test_a_source_sequence_orders_transactions_within_a_day():
    """A same-day buy and sell of one holding sort by the first posting's amount, which puts the
    negative one first — so the disposal renders before the acquisition and there is no lot to
    reduce. The export's own order has to survive, and it cannot be derived from the postings.
    """

    def trade(quantity, sequence):
        return Transaction(
            meta={"institution": "test", "sequence": sequence},
            date=date(2023, 8, 2),
            flag=FLAG_OKAY,
            payee="Broker",
            narration=f"trade {quantity}",
            tags=frozenset(),
            links=frozenset(),
            postings=(
                Posting(account="Assets:Broker:Positions", units=Amount(Decimal(quantity), "XYZ")),
                Posting(account="Assets:Broker:Cash", units=Amount(Decimal("1"), "EUR")),
            ),
        )

    out = render([trade("-5", 2), trade("10", 1)])

    assert out.index("trade 10") < out.index("trade -5")


def test_transactions_without_a_sequence_keep_their_previous_order():
    """Everything already in the ledger has no sequence, so nothing may move."""

    def plain(amount, narration):
        return Transaction(
            meta={"institution": "test"},
            date=date(2023, 8, 2),
            flag=FLAG_OKAY,
            payee="X",
            narration=narration,
            tags=frozenset(),
            links=frozenset(),
            postings=(
                Posting(account="Assets:A", units=Amount(Decimal(amount), "EUR")),
                Posting(account="Expenses:B", units=None),
            ),
        )

    out = render([plain("10", "second"), plain("-5", "first")])

    assert out.index("first") < out.index("second")
