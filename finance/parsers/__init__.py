"""Format parsers.

Each parser exposes ``parse_file(path, accounts) -> Iterator[Directive]`` and is registered in
:data:`FORMATS` under the name used in a ledger's rules file.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

from finance.models import Directive
from finance.parsers import belfius_be, coinbase_pro, degiro, ing_be
from finance.parsers.base import Accounts, UnknownAccountError

Parser = Callable[[str, Accounts], Iterator[Directive]]

FORMATS: dict[str, Parser] = {
    "belfius_be": belfius_be.parse_file,
    "coinbase_pro": coinbase_pro.parse_file,
    "degiro": degiro.parse_file,
    "ing_be": ing_be.parse_file,
}

__all__ = ["FORMATS", "Accounts", "Parser", "UnknownAccountError"]
