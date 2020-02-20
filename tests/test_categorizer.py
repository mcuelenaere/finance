import pytest

from finance.categorizer import parse_rule_from_config, Categorizer, Rule
from finance.models import Account, Currency, Transaction


def test_parse_simple_rule():
    rule = parse_rule_from_config({
        'field': 'foo',
        'value': 'bar',
        'category': 'FOOBAR'
    })
    assert isinstance(rule, Rule)
    assert rule.category == 'FOOBAR'
    assert rule.field == 'foo'
    assert rule.regex.pattern == '^bar$'


def test_parse_regex_rule():
    rule = parse_rule_from_config({
        'field': 'foo',
        'regex': 'ba.r{2}',
        'category': 'FOOBAR'
    })
    assert isinstance(rule, Rule)
    assert rule.category == 'FOOBAR'
    assert rule.field == 'foo'
    assert rule.regex.pattern == 'ba.r{2}'


def test_parse_invalid_rule():
    with pytest.raises(ValueError, match='config does not contain either value or regex'):
        parse_rule_from_config({
            'foo': 'bar'
        })


def test_categorizer_simple():
    rule1 = parse_rule_from_config({
        'field': 'currency',
        'value': 'EUR',
        'category': 'EUR_CURRENCY'
    })
    rule2 = parse_rule_from_config({
        'field': 'source.name',
        'regex': '^a',
        'category': 'NAME_STARTS_WITH_A',
    })
    categorizer = Categorizer([rule1, rule2], fallback_category='OTHER')

    transaction = Transaction(
        source=None,
        destination=None,
        timestamp=None,
        amount=None,
        currency=Currency.EUR,
        description=None,
    )
    assert categorizer.categorize(transaction) == 'EUR_CURRENCY'

    transaction = Transaction(
        source=Account(name='aba', number='1234'),
        destination=None,
        timestamp=None,
        amount=None,
        currency=None,
        description=None,
    )
    assert categorizer.categorize(transaction) == 'NAME_STARTS_WITH_A'

    transaction = Transaction(
        source=None,
        destination=None,
        timestamp=None,
        amount=None,
        currency=None,
        description=None,
    )
    assert categorizer.categorize(transaction) == 'OTHER'
