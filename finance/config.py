from finance.categorizer import Categorizer, Rule, parse_rule_from_config
from finance.models import Transaction
from finance.parsers import formats as finance_formats
from glob import iglob
from yaml import safe_load
from typing import NamedTuple, Tuple, Callable, Iterable


class File(NamedTuple):
    path: str
    parser: Callable[[str], Iterable[Transaction]]


class Config(NamedTuple):
    rules: Tuple[Rule]
    fallback_category: str
    files: Tuple[File]

    def categorizer(self):
        return Categorizer(self.rules, self.fallback_category)

    def transactions(self):
        transactions = []
        for file in self.files:
            for transaction in file.parser(file.path):
                transactions.append(transaction)
        return tuple(transactions)


def parse_config(path: str):
    with open(path, 'r') as f:
        cfg = safe_load(f)

    rules = []
    if cfg['rules']:
        for rule in cfg['rules']:
            rules.append(parse_rule_from_config(rule))

    files = []
    if cfg['files']:
        for file in cfg['files']:
            for path in iglob(file['path']):
                files.append(File(
                    path=path,
                    parser=finance_formats[file['format']]
                ))

    return Config(
        rules=tuple(rules),
        fallback_category=cfg.get('fallback_category', 'UNKNOWN'),
        files=tuple(files)
    )
