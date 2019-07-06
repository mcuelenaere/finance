from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import NamedTuple, Optional


class Currency(Enum):
    EUR = 'EUR'
    USD = 'USD'


class Account(NamedTuple):
    number: Optional[str]
    name: Optional[str]


class Transaction(NamedTuple):
    source: Account
    destination: Account
    timestamp: Optional[datetime]
    amount: Decimal
    currency: Currency
    description: Optional[str]
