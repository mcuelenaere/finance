"""Remove transactions that appear in more than one export file.

Statement exports overlap: the ING directory holds files covering ``20180424 - 20190423`` and
``20190406 - 20190423``, so the same weeks arrive twice. Because the ledger is regenerated
from scratch every build, this is the only place duplicates need handling — there is no
incremental import state to keep straight.

Two tiers, because the sources differ in what they give us:

1. **A stable id** (``meta.transaction_id``, present in Wirex, Trade Republic and Revolut
   exports) — dedup on it. Exact, no guessing.
2. **No id** (ING, Belfius, Edenred) — a heuristic is unavoidable, and the obvious one is
   wrong. Collapsing identical transactions into a set silently deletes *genuine* duplicates:
   two identical coffees on the same day are two real payments. Instead, count how often each
   key occurs within each file and keep the **maximum** across files, never the sum. An
   overlapping file re-reports the same two coffees, so the max is 2 — correct either way.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable
from decimal import Decimal

from finance.models import Balance, Directive, Open, Price, Transaction
from finance.text import normalize_text

#: Key identifying "the same transaction" when the source gives us no id of its own.
_Key = tuple[str | None, object, Decimal | None, str]


def _identity(txn: Transaction) -> tuple[object, ...] | None:
    """The source's own id for this transaction, if it has one."""
    transaction_id = txn.meta.get("transaction_id")
    if not transaction_id:
        return None
    return (txn.meta.get("institution"), str(transaction_id))


def _heuristic_key(txn: Transaction) -> _Key:
    posting = txn.postings[0] if txn.postings else None
    account = posting.account if posting else None
    number = posting.units.number if posting and posting.units else None
    return (account, txn.date, number, normalize_text(txn.narration))


def dedupe(transactions: Iterable[Transaction]) -> list[Transaction]:
    """Drop duplicates introduced by overlapping exports, preserving real repeats."""
    by_identity: dict[tuple[object, ...], Transaction] = {}
    unidentified: list[Transaction] = []

    for txn in transactions:
        identity = _identity(txn)
        if identity is None:
            unidentified.append(txn)
        else:
            by_identity.setdefault(identity, txn)

    # Group the id-less transactions per source file, then keep the highest count seen for
    # each key rather than the total across files.
    per_file: dict[str, dict[_Key, list[Transaction]]] = defaultdict(lambda: defaultdict(list))
    for txn in unidentified:
        source = str(txn.meta.get("source_file", ""))
        per_file[source][_heuristic_key(txn)].append(txn)

    best: dict[_Key, list[Transaction]] = {}
    for source in sorted(per_file):  # sorted so ties resolve deterministically
        for key, group in per_file[source].items():
            if key not in best or len(group) > len(best[key]):
                best[key] = group

    result = list(by_identity.values())
    for group in best.values():
        result.extend(group)
    return result


def _directive_key(directive: Directive) -> tuple[object, ...] | None:
    if isinstance(directive, Balance):
        return ("balance", directive.account, directive.date, directive.amount)
    if isinstance(directive, Open):
        return ("open", directive.account, directive.date, directive.currencies)
    if isinstance(directive, Price):
        return ("price", directive.currency, directive.date, directive.amount)
    return None


def dedupe_directives(directives: Iterable[Directive]) -> list[Directive]:
    """Drop exactly-duplicate non-transaction directives.

    Belfius splits one account's history across several files and repeats the account's
    *current* closing balance in every one, so eight chunk files yield eight identical
    ``Balance`` directives. Beancount rejects duplicate assertions outright, so they have to
    be collapsed.

    The key includes the amount deliberately: two files asserting *different* balances for the
    same account and date is a genuine inconsistency, and it should reach Beancount and be
    reported rather than be silently resolved here.
    """
    seen: set[tuple[object, ...]] = set()
    out: list[Directive] = []
    for directive in directives:
        key = _directive_key(directive)
        if key is None:
            out.append(directive)
        elif key not in seen:
            seen.add(key)
            out.append(directive)
    return out


def duplicate_report(transactions: Iterable[Transaction]) -> Counter[str]:
    """How many transactions each source file contributed that were dropped as duplicates."""
    transactions = list(transactions)
    kept = {id(txn) for txn in dedupe(transactions)}
    dropped: Counter[str] = Counter()
    for txn in transactions:
        if id(txn) not in kept:
            dropped[str(txn.meta.get("source_file", "?"))] += 1
    return dropped
