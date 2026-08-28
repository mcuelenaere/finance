"""Delta portfolio exports.

Unlike every other parser here, the input is a hand transcription rather than a bank's own file:
the first account read this way had a bank that refuses to export anything. The balance
assertions are what make that safe, so they are the thing most worth testing.
"""

import json
from decimal import Decimal

import pytest

from finance.models import Balance, Open, Transaction
from finance.parsers import delta
from finance.parsers.base import UnknownAccountError

ACCOUNTS = {"PENSION": "Assets:Investments:PensionSavings:Cash"}
ACCOUNT_KEY = "pension"
CASH = "Assets:Investments:PensionSavings:Cash"
POSITIONS = "Assets:Investments:PensionSavings:Positions"


def write(tmp_path, transactions, positions=(), as_of=None, account=ACCOUNT_KEY):
    payload = {
        "account": account,
        "transactions": list(transactions),
        "positions": list(positions),
    }
    if as_of:
        payload["as_of"] = as_of
    path = tmp_path / "pension.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def buy(date, base, amount, price, txn_id=1):
    return {
        "id": txn_id,
        "datetime": f"{date}T10:00:00.000Z",
        "way": "BUY",
        "base": base,
        "quote": "EUR",
        "amount": amount,
        "price": {"value": price, "currency": "EUR"},
    }


def parse(path):
    return list(delta.parse_file(path, ACCOUNTS))


def transactions(directives):
    return [d for d in directives if isinstance(d, Transaction)]


def test_a_purchase_books_a_lot_at_what_was_paid(tmp_path):
    (txn,) = transactions(
        parse(write(tmp_path, [buy("2024-04-02", "BE0000000001", 4.741, 174.08)]))
    )

    position, cash = txn.postings
    assert position.account == POSITIONS
    assert position.units.number == Decimal("4.741")
    assert position.units.currency == "BE0000000001"
    # 4.741 * 174.08 = 825.31328, quantized to the cents actually paid.
    assert position.cost.number == Decimal("825.31")
    assert position.cost.total is True
    assert cash.account == CASH
    assert cash.units.number == Decimal("-825.31")


def test_cost_comes_from_amount_times_price_not_worth(tmp_path):
    """Delta's `worth` is the position's CURRENT value, not the transaction's.

    The same purchase came back from the API as 985.37, which is the quantity times *today's*
    price. Trusting it would inflate the cost basis by the entire gain, silently.
    """
    row = buy("2024-04-02", "BE0000000001", 4.741, 174.08)
    row["worth"] = {"value": 985.36944, "currency": "EUR"}
    (txn,) = transactions(parse(write(tmp_path, [row])))

    assert txn.postings[0].cost.number == Decimal("825.31")


def test_the_holdings_become_balance_assertions(tmp_path):
    """Transactions and positions come from different Delta endpoints, so this checks the
    transcription rather than restating it."""
    directives = parse(
        write(
            tmp_path,
            [buy("2024-01-16", "BE0000000001", 0.5, 100.0)],
            positions=[{"shortCode": "BE0000000001", "holdings": "0.5"}],
            as_of="2026-08-28",
        )
    )
    (balance,) = [d for d in directives if isinstance(d, Balance)]

    assert balance.account == POSITIONS
    assert balance.amount == (Decimal("0.5"), "BE0000000001")
    assert balance.date.isoformat() == "2026-08-28"


def test_no_as_of_means_no_assertion(tmp_path):
    """Dating it from the clock would make the build non-reproducible."""
    directives = parse(
        write(
            tmp_path,
            [buy("2024-01-16", "BE0000000001", 0.5, 100.0)],
            positions=[{"shortCode": "BE0000000001", "holdings": "0.5"}],
        )
    )

    assert [d for d in directives if isinstance(d, Balance)] == []


def test_an_assertion_never_predates_the_last_transaction(tmp_path):
    directives = parse(
        write(
            tmp_path,
            [buy("2024-01-16", "BE0000000001", 0.5, 100.0)],
            positions=[{"shortCode": "BE0000000001", "holdings": "0.5"}],
            as_of="2020-01-01",
        )
    )
    (balance,) = [d for d in directives if isinstance(d, Balance)]

    assert balance.date.isoformat() == "2024-01-17"


def test_output_does_not_depend_on_the_order_the_api_returned(tmp_path):
    """The export is newest-first and paginated; the build must be byte-identical regardless."""
    rows = [
        buy("2024-01-16", "BE0000000001", 0.5, 100.0, txn_id=1),
        buy("2023-01-16", "BE0000000001", 0.6, 90.0, txn_id=2),
    ]
    forward = [t.date for t in transactions(parse(write(tmp_path, rows)))]
    backward = [t.date for t in transactions(parse(write(tmp_path, list(reversed(rows)))))]

    assert forward == backward == sorted(forward)


def test_delta_ids_are_kept_so_a_re_import_cannot_double_up(tmp_path):
    (txn,) = transactions(
        parse(write(tmp_path, [buy("2024-01-16", "BE0000000001", 0.5, 100.0, txn_id=987)]))
    )

    assert txn.meta["transaction_id"] == "987"


def test_positions_open_fifo(tmp_path):
    (opened,) = [
        d
        for d in parse(write(tmp_path, [buy("2024-01-16", "BE0000000001", 0.5, 100.0)]))
        if isinstance(d, Open)
    ]

    assert opened.account == POSITIONS
    assert opened.booking == "FIFO"


def test_a_row_that_is_not_a_purchase_is_left_to_the_rules(tmp_path):
    """No disposal exists in this export, so its shape cannot be checked against anything."""
    row = buy("2024-01-16", "BE0000000001", 0.5, 100.0)
    row["way"] = "SELL"
    (txn,) = transactions(parse(write(tmp_path, [row])))

    assert len(txn.postings) == 2
    assert txn.postings[1].account is None


def test_missing_account_is_an_error_not_a_guess(tmp_path):
    path = write(tmp_path, [buy("2024-01-16", "BE0000000001", 0.5, 100.0)])
    with pytest.raises(UnknownAccountError, match="pension"):
        list(delta.parse_file(path, {}))


def test_the_export_names_its_own_account():
    """One tracker holds many portfolios, so the file has to say which account it is."""
    assert delta.ACCOUNT_FIELD == "account"


def test_an_export_without_an_account_falls_back_to_the_default_key(tmp_path):
    path = write(tmp_path, [buy("2024-01-16", "BE0000000001", 0.5, 100.0)], account=None)
    with pytest.raises(UnknownAccountError, match=delta.DEFAULT_ACCOUNT_KEY):
        list(delta.parse_file(path, {"PENSION": "Assets:X:Cash"}))


def test_the_named_account_is_the_one_resolved(tmp_path):
    path = write(
        tmp_path, [buy("2024-01-16", "BE0000000001", 0.5, 100.0)], account="somewhere_else"
    )
    (txn,) = transactions(list(delta.parse_file(path, {"SOMEWHERE_ELSE": "Assets:Other:Cash"})))

    assert txn.postings[1].account == "Assets:Other:Cash"
