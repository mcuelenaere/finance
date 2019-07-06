import re
from finance.models import Transaction
from typing import Dict, Iterable, NamedTuple


class Rule(NamedTuple):
    field: str
    regex: re.Pattern
    category: str


def parse_rule_from_config(config: Dict) -> Rule:
    if config.get('value', None):
        regex = re.compile(r'^' + re.escape(config['value']) + r'$')
    elif config.get('regex', None):
        regex = re.compile(config['regex'])
    else:
        raise ValueError('config does not contain either value or regex')

    return Rule(
        field=config['field'],
        regex=regex,
        category=config['category']
    )


def _recursive_getattr(o: object, name: str):
    for part in name.split('.'):
        if o is None:
            return None

        o = getattr(o, part)
    return o


class Categorizer(object):
    def __init__(self, rules: Iterable[Rule], fallback_category: str):
        self.rules = tuple(rules)
        self.fallback_category = fallback_category

    def categorize(self, transaction: Transaction) -> str:
        for rule in self.rules:
            value = _recursive_getattr(transaction, rule.field)
            if value and rule.regex.search(str(value)):
                return rule.category

        return self.fallback_category
