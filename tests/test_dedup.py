"""Dedup behaviour, including the case a naive implementation gets wrong."""

from datetime import date
from decimal import Decimal

from finance.dedup import dedupe
from finance.models import FLAG_OKAY, Amount, Posting, Transaction

ACCOUNT = "Assets:ING:Checking"


def txn(day: int, amount: str, narration: str, *, source: str, transaction_id: str | None = None):
    meta: dict[str, object] = {"source_file": source, "institution": "ing_be"}
    if transaction_id:
        meta["transaction_id"] = transaction_id
    return Transaction(
        meta=meta,
        date=date(2024, 3, day),
        flag=FLAG_OKAY,
        payee=None,
        narration=narration,
        tags=frozenset(),
        links=frozenset(),
        postings=(
            Posting(account=ACCOUNT, units=Amount(Decimal(amount), "EUR")),
            Posting(account=None, units=None),
        ),
    )


def test_overlapping_exports_are_deduplicated():
    """Two files covering the same week must not double-count."""
    a = txn(1, "-10.00", "COFFEE", source="jan-apr.csv")
    b = txn(1, "-10.00", "COFFEE", source="apr-jun.csv")

    assert len(dedupe([a, b])) == 1


def test_genuine_same_day_duplicates_survive():
    """Two identical coffees on one day are two real payments, not a duplicate.

    This is the case a `set()` over transactions silently destroys.
    """
    first = txn(1, "-3.50", "COFFEE", source="jan-apr.csv")
    second = txn(1, "-3.50", "COFFEE", source="jan-apr.csv")

    assert len(dedupe([first, second])) == 2


def test_max_across_files_not_sum():
    """A file reporting both coffees plus an overlapping file reporting them again → 2."""
    same_file = [
        txn(1, "-3.50", "COFFEE", source="jan-apr.csv"),
        txn(1, "-3.50", "COFFEE", source="jan-apr.csv"),
    ]
    overlapping = [
        txn(1, "-3.50", "COFFEE", source="apr-jun.csv"),
        txn(1, "-3.50", "COFFEE", source="apr-jun.csv"),
    ]

    assert len(dedupe(same_file + overlapping)) == 2


def test_partial_overlap_keeps_the_higher_count():
    """One file saw both, the other only one — trust the higher count."""
    complete = [
        txn(1, "-3.50", "COFFEE", source="a.csv"),
        txn(1, "-3.50", "COFFEE", source="a.csv"),
    ]
    truncated = [txn(1, "-3.50", "COFFEE", source="b.csv")]

    assert len(dedupe(complete + truncated)) == 2
    assert len(dedupe(truncated + complete)) == 2


def test_stable_ids_are_used_when_present():
    a = txn(1, "-10.00", "X", source="a.json", transaction_id="abc")
    b = txn(2, "-99.00", "DIFFERENT", source="b.json", transaction_id="abc")

    # Same id means same transaction, even though every other field differs.
    assert len(dedupe([a, b])) == 1


def test_distinct_transactions_are_left_alone():
    entries = [
        txn(1, "-10.00", "A", source="f.csv"),
        txn(2, "-10.00", "A", source="f.csv"),
        txn(1, "-20.00", "A", source="f.csv"),
        txn(1, "-10.00", "B", source="f.csv"),
    ]

    assert len(dedupe(entries)) == 4


def test_dedup_is_order_independent():
    entries = [
        txn(1, "-3.50", "COFFEE", source="a.csv"),
        txn(1, "-3.50", "COFFEE", source="b.csv"),
        txn(2, "-8.00", "LUNCH", source="a.csv"),
    ]

    assert len(dedupe(entries)) == len(dedupe(list(reversed(entries)))) == 2


def test_identical_balance_directives_are_collapsed():
    """Belfius repeats one account's current balance in every chunk file it exports.

    Eight chunks therefore yield eight identical assertions, which Beancount rejects outright.
    """
    from datetime import date as D

    from finance.dedup import dedupe_directives
    from finance.models import Amount as A
    from finance.models import Balance

    def bal(amount, source):
        return Balance(
            meta={"source_file": source},
            date=D(2026, 8, 27),
            account=ACCOUNT,
            amount=A(Decimal(amount), "EUR"),
        )

    assert len(dedupe_directives([bal("1234.56", f"chunk{i}.csv") for i in range(8)])) == 1


def test_conflicting_balance_directives_are_kept():
    """Two files disagreeing about one account's balance is a real inconsistency.

    Collapsing them here would hide it; passing both through lets Beancount report it.
    """
    from datetime import date as D

    from finance.dedup import dedupe_directives
    from finance.models import Amount as A
    from finance.models import Balance

    directives = [
        Balance(meta={}, date=D(2026, 8, 27), account=ACCOUNT, amount=A(Decimal("1"), "EUR")),
        Balance(meta={}, date=D(2026, 8, 27), account=ACCOUNT, amount=A(Decimal("2"), "EUR")),
    ]

    assert len(dedupe_directives(directives)) == 2
