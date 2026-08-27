"""Serialize directives to Beancount text.

Deliberately dumb: :mod:`finance.models` already mirrors Beancount's own model, so there is
nothing to translate here. Every decision in this module is about *stability* — the same
directives must always produce byte-identical output, because ``generated.beancount`` is a
committed build artifact and its diffs are how changes get reviewed.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import date as Date
from decimal import Decimal

from finance.models import (
    Amount,
    Balance,
    Cost,
    Directive,
    Open,
    Posting,
    Price,
    Transaction,
)

#: Accounts are padded to this column so amounts line up, as `bean-format` would do.
ACCOUNT_WIDTH = 50

#: Beancount metadata keys must start lowercase and hold only word characters or dashes.
_META_KEY = re.compile(r"^[a-z][A-Za-z0-9_-]*$")

#: Ordering of directive kinds within a single date, so output is deterministic.
_KIND_ORDER = {Open: 0, Balance: 1, Price: 2, Transaction: 3}


def _number(value: Decimal) -> str:
    """Render a Decimal without ever falling back to scientific notation.

    Note that the quantization the parser chose is preserved verbatim — ``Decimal("4.40")``
    stays ``4.40``. Do not call ``normalize()`` here: it turns ``Decimal("1000")`` into
    ``1E+3``, which Beancount will not parse.
    """
    return format(value, "f")


def _string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _amount(amount: Amount) -> str:
    return f"{_number(amount.number)} {amount.currency}"


def _cost(cost: Cost) -> str:
    parts = [_amount(Amount(cost.number, cost.currency))]
    if cost.date is not None:
        parts.append(cost.date.isoformat())
    if cost.label is not None:
        parts.append(_string(cost.label))
    return "{" + ", ".join(parts) + "}"


def _meta_value(value: object) -> str:
    if isinstance(value, str):
        return _string(value)
    if isinstance(value, Decimal):
        return _number(value)
    if isinstance(value, Date):
        return value.isoformat()
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    return _string(str(value))


def _meta_lines(meta: dict[str, object] | None, indent: str) -> list[str]:
    if not meta:
        return []
    lines = []
    for key in sorted(meta):
        if not _META_KEY.match(key):
            raise ValueError(
                f"invalid Beancount metadata key {key!r}: must match {_META_KEY.pattern}"
            )
        lines.append(f"{indent}{key}: {_meta_value(meta[key])}")
    return lines


def _posting_lines(posting: Posting) -> list[str]:
    if posting.account is None:
        raise ValueError(
            "cannot render a posting with no account — the rules engine left it unresolved"
        )

    head = f"  {posting.flag} {posting.account}" if posting.flag else f"  {posting.account}"

    tail_parts = []
    if posting.units is not None:
        tail_parts.append(_amount(posting.units))
        if posting.cost is not None:
            tail_parts.append(_cost(posting.cost))
        if posting.price is not None:
            tail_parts.append(f"@ {_amount(posting.price)}")

    if tail_parts:
        line = f"{head.ljust(ACCOUNT_WIDTH)} {' '.join(tail_parts)}".rstrip()
    else:
        # Elided amount: Beancount balances it for us.
        line = head

    return [line, *_meta_lines(posting.meta, "    ")]


def render_directive(directive: Directive) -> str:
    """Render a single directive, without a trailing newline."""
    date = directive.date.isoformat()

    if isinstance(directive, Open):
        currencies = f" {','.join(directive.currencies)}" if directive.currencies else ""
        lines = [f"{date} open {directive.account}{currencies}"]
        lines.extend(_meta_lines(directive.meta, "  "))
        return "\n".join(lines)

    if isinstance(directive, Balance):
        account = f"{date} balance {directive.account}"
        lines = [f"{account.ljust(ACCOUNT_WIDTH)} {_amount(directive.amount)}".rstrip()]
        lines.extend(_meta_lines(directive.meta, "  "))
        return "\n".join(lines)

    if isinstance(directive, Price):
        lines = [f"{date} price {directive.currency} {_amount(directive.amount)}"]
        lines.extend(_meta_lines(directive.meta, "  "))
        return "\n".join(lines)

    if isinstance(directive, Transaction):
        header = f"{date} {directive.flag}"
        # Beancount reads a lone string as the narration, so a payee-less transaction must
        # not emit an empty first string.
        if directive.payee:
            header += f" {_string(directive.payee)} {_string(directive.narration)}"
        else:
            header += f" {_string(directive.narration)}"
        for tag in sorted(directive.tags):
            header += f" #{tag}"
        for link in sorted(directive.links):
            header += f" ^{link}"

        lines = [header, *_meta_lines(directive.meta, "  ")]
        for posting in directive.postings:
            lines.extend(_posting_lines(posting))
        return "\n".join(lines)

    raise TypeError(f"cannot render {type(directive).__name__}")


def _sort_key(directive: Directive) -> tuple:
    kind = _KIND_ORDER[type(directive)]
    account = getattr(directive, "account", "") or ""
    if isinstance(directive, Transaction):
        first = directive.postings[0] if directive.postings else None
        account = (first.account or "") if first else ""
        amount = first.units.number if first and first.units else Decimal(0)
        return (directive.date, kind, account, amount, directive.narration)
    if isinstance(directive, Balance):
        return (directive.date, kind, account, directive.amount.number, "")
    if isinstance(directive, Price):
        return (directive.date, kind, directive.currency, directive.amount.number, "")
    return (directive.date, kind, account, Decimal(0), "")


def render(directives: Iterable[Directive], *, header: str | None = None) -> str:
    """Render directives to a full Beancount file, in a stable order."""
    lines = []
    if header:
        lines.extend(f";; {line}".rstrip() for line in header.splitlines())
        lines.append("")

    for directive in sorted(directives, key=_sort_key):
        lines.append(render_directive(directive))
        lines.append("")

    return "\n".join(lines).rstrip("\n") + "\n"
