"""Helpers shared by the parsers.

Parsers are pure and deterministic: a file in, directives out, no model calls and no I/O
beyond reading the file. Each one leaves exactly one posting unresolved
(``account=None``) for :mod:`finance.rules` to fill.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date as Date
from datetime import datetime
from decimal import Decimal

from finance.iban import variants
from finance.text import normalize_identifier

#: Maps an identifier appearing in an export (an IBAN, or a fixed key like ``degiro``) to the
#: Beancount account it represents. Comes from ``own_accounts`` in the ruleset.
Accounts = Mapping[str, str]


class UnknownAccountError(LookupError):
    """An export references an account that is not declared in ``own_accounts``.

    Raised rather than guessed: silently inventing an account name would produce a ledger
    that looks fine and is wrong. The message names the identifier so it can be pasted
    straight into the rules file.
    """


def resolve_account(accounts: Accounts, identifier: str, *, source: str = "") -> str:
    # Every spelling is tried: a Belgian account may be named domestically in one export and
    # by IBAN in another. See finance.iban.
    key = normalize_identifier(identifier)
    for spelling in variants(key):
        if spelling in accounts:
            return accounts[spelling]

    where = f" (seen in {source})" if source else ""
    raise UnknownAccountError(
        f"account {identifier!r}{where} is not in own_accounts; "
        f"add an entry keyed {key!r} to the ledger's rules file"
    )


def parse_decimal(text: str) -> Decimal:
    """Parse an amount written in Belgian/European notation.

    Handles ``-9,68``, ``1.234,56`` and plain ``1234.56``. The naive
    ``text.replace(",", ".")`` that the original parsers used turns ``1.234,56`` into
    ``1.234.56`` and raises — so every amount over a thousand was broken.
    """
    value = text.strip().replace("\xa0", "").replace(" ", "").lstrip("+")
    if not value:
        raise ValueError("empty amount")
    if "," in value:
        # A comma means European notation, so any dots are thousands separators.
        value = value.replace(".", "").replace(",", ".")
    return Decimal(value)


def parse_date(text: str) -> Date:
    """Parse ``dd/mm/yyyy``, the format every Belgian bank export here uses."""
    return datetime.strptime(text.strip(), "%d/%m/%Y").date()


def quantize_money(value: float | Decimal, places: str = "0.01") -> Decimal:
    """Round a float amount to cents, via str to avoid inheriting binary noise.

    Edenred stores ``-4.4000000000000004`` for what is really ``-4.40``; feeding that
    straight into ``Decimal`` puts the noise in the ledger.
    """
    return Decimal(str(value)).quantize(Decimal(places))
