"""Dedup behaviour, including the case a naive implementation gets wrong."""

from datetime import date
from decimal import Decimal

from finance.dedup import dedupe, dedupe_directives
from finance.models import (
    FLAG_OKAY,
    Amount,
    Balance,
    Open,
    Posting,
    Transaction,
)

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

    directives = [
        Balance(meta={}, date=D(2026, 8, 27), account=ACCOUNT, amount=A(Decimal("1"), "EUR")),
        Balance(meta={}, date=D(2026, 8, 27), account=ACCOUNT, amount=A(Decimal("2"), "EUR")),
    ]

    assert len(dedupe_directives(directives)) == 2


# --- the key must survive a bank rewording its own descriptions -------------------------
# This is the bug that made the key what it is. ING re-exported a decade already on file and
# reworded every description in it — `Aankoop Bancontact/Mister Cash` became `Betaling
# Bancontact`, `Europese overschrijving` became `Overschrijving in euro`. With the description
# in the key, 619 rows matched 127 of their own twins and ten years of history silently
# doubled. Nothing failed; the numbers were simply twice as large.


def reworded(date_, amount, narration, *, source, entry=None):
    meta = {"institution": "ing_be", "source_file": source}
    if entry is not None:
        meta["entry_number"] = str(entry)
    return Transaction(
        meta=meta,
        date=date_,
        flag=FLAG_OKAY,
        payee=None,
        narration=narration,
        tags=frozenset(),
        links=frozenset(),
        postings=(
            Posting(account="Assets:ING:Checking", units=Amount(Decimal(amount), "EUR")),
            Posting(account=None, units=None),
        ),
    )


def test_a_reworded_description_is_still_the_same_transaction():
    day = date(2014, 1, 2)
    rows = [
        reworded(day, "-19.95", "Aankoop Bancontact/Mister Cash - 19,95 COLRUYT", source="old.csv"),
        reworded(day, "-19.95", "Betaling Bancontact - 19,95 COLRUYT", source="new.csv"),
    ]

    assert len(dedupe(rows)) == 1


def test_the_banks_own_row_reference_separates_two_same_day_amounts():
    """Two distinct transfers of the same amount on the same day are two payments."""
    day = date(2014, 1, 7)
    rows = [
        reworded(day, "-260.00", "Overschrijving MyING.be", source="old.csv", entry=5),
        reworded(day, "-260.00", "Overschrijving MyING.be", source="old.csv", entry=9),
    ]

    assert len(dedupe(rows)) == 2


def test_a_reference_pairs_the_same_row_across_a_rewording():
    day = date(2014, 1, 7)
    rows = [
        reworded(day, "-260.00", "Overschrijving MyING.be", source="old.csv", entry=5),
        reworded(
            day, "-260.00", "Overschrijving in euro (SEPA): MyING.be", source="new.csv", entry=5
        ),
    ]

    assert len(dedupe(rows)) == 1


def test_genuine_same_day_repeats_still_survive_without_any_reference():
    """Counting per file, not the key's precision, is what protects real duplicates."""
    day = date(2014, 3, 1)
    rows = [
        reworded(day, "-3.20", "Aankoop", source="old.csv"),
        reworded(day, "-3.20", "Aankoop", source="old.csv"),
        reworded(day, "-3.20", "Betaling", source="new.csv"),
        reworded(day, "-3.20", "Betaling", source="new.csv"),
    ]

    assert len(dedupe(rows)) == 2


def open_directive(day: int, *, source: str, account: str = "Assets:Investments:Brokers:Wirex"):
    return Open(
        meta={"institution": "wirex", "source_file": source},
        date=date(2024, 5, day),
        account=account,
        currencies=(),
        booking="FIFO",
    )


def test_an_account_is_opened_once_at_the_earliest_date_any_file_implies():
    """An export arriving as several files — paginated pages, or one per statement period — has
    each file derive an opening date from the rows it happens to hold. Beancount rejects the
    second directive whatever date it carries, and keeping whichever file was read first would
    open the account after its own earliest transaction."""
    kept = dedupe_directives(
        [
            open_directive(9, source="page_1.json"),
            open_directive(2, source="page_2.json"),
            open_directive(20, source="page_3.json"),
        ]
    )

    (opened,) = kept
    assert opened.date == date(2024, 5, 2)


def test_opens_for_different_accounts_both_survive():
    kept = dedupe_directives(
        [
            open_directive(2, source="a.json", account="Assets:Investments:Brokers:Wirex"),
            open_directive(3, source="b.json", account="Assets:Investments:Brokers:BUX"),
        ]
    )

    assert len(kept) == 2
