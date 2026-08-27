from decimal import Decimal

import pytest

from finance.parsers import trade_republic
from finance.parsers.base import UnknownAccountError

ACCOUNTS = {"TRADE_REPUBLIC": "Assets:Investments:TradeRepublic:Cash"}
CASH = "Assets:Investments:TradeRepublic:Cash"

COLUMNS = [
    "datetime",
    "date",
    "account_type",
    "category",
    "type",
    "asset_class",
    "name",
    "symbol",
    "shares",
    "price",
    "amount",
    "fee",
    "tax",
    "currency",
    "original_amount",
    "original_currency",
    "fx_rate",
    "description",
    "transaction_id",
    "counterparty_name",
    "counterparty_iban",
    "payment_reference",
    "mcc_code",
]


def write(tmp_path, *rows):
    lines = [",".join(COLUMNS)]
    for row in rows:
        lines.append(",".join(str(row.get(c, "")) for c in COLUMNS))
    path = tmp_path / "transactions.csv"
    path.write_text("\n".join(lines), encoding="utf-8")
    return str(path)


def row(**overrides):
    base = {
        "date": "2024-01-04",
        "account_type": "DEFAULT",
        "category": "CASH",
        "type": "CUSTOMER_INBOUND",
        "amount": "1000.000000",
        "currency": "EUR",
        "description": "Some description",
        "transaction_id": "63748ca0-13aa-4a29-a69a-0513c01dd166",
    }
    return {**base, **overrides}


def test_signed_amount_and_elided_counter(tmp_path):
    (txn,) = list(trade_republic.parse_file(write(tmp_path, row()), ACCOUNTS))

    assert txn.date.isoformat() == "2024-01-04"
    own, counter = txn.postings
    assert own.account == CASH
    assert own.units.number == Decimal("1000.00")
    assert counter.account is None and counter.units is None


def test_six_decimal_amounts_are_quantized_to_cents(tmp_path):
    """Amounts arrive as `1000.000000`; carrying that through fills the ledger with noise."""
    (txn,) = list(trade_republic.parse_file(write(tmp_path, row(amount="24051.350000")), ACCOUNTS))

    assert str(txn.postings[0].units.number) == "24051.35"


def test_fee_is_separate_from_the_amount(tmp_path):
    """A card top-up charges 5035 and deducts a 35 fee, so 5000 in cash actually arrives."""
    (txn,) = list(
        trade_republic.parse_file(
            write(
                tmp_path,
                row(
                    type="CUSTOMER_INPAYMENT",
                    amount="5035.000000",
                    fee="-35.00",
                    description="Card Top up",
                ),
            ),
            ACCOUNTS,
        )
    )

    cash, fee, counter = txn.postings
    assert cash.units.number == Decimal("5000.00")
    assert fee.account == "Expenses:Financial:Fees"
    assert fee.units.number == Decimal("35.00")
    # Beancount balances the counter to the gross -5035.
    assert counter.account is None


def test_a_fee_only_row_balances_without_a_counter(tmp_path):
    """`CARD_ORDERING_FEE` has amount 0 and the fee is the whole transaction.

    Emitting an elided posting here would leave the rules engine to classify a zero, so the
    transaction is complete as it stands.
    """
    (txn,) = list(
        trade_republic.parse_file(
            write(
                tmp_path,
                row(
                    type="CARD_ORDERING_FEE",
                    amount="0.000000",
                    fee="-50.00",
                    description="Trade Republic Card",
                ),
            ),
            ACCOUNTS,
        )
    )

    assert len(txn.postings) == 2
    assert txn.postings[0].units.number == Decimal("-50.00")
    assert txn.postings[1].units.number == Decimal("50.00")
    assert all(posting.account is not None for posting in txn.postings)


def test_tax_is_treated_like_a_fee(tmp_path):
    (txn,) = list(
        trade_republic.parse_file(
            write(tmp_path, row(type="INTEREST_PAYMENT", amount="100.000000", tax="-30.00")),
            ACCOUNTS,
        )
    )

    cash, tax, _counter = txn.postings
    assert cash.units.number == Decimal("70.00")
    assert tax.account == "Expenses:Taxes"
    assert tax.units.number == Decimal("30.00")


def test_metadata_that_rules_and_dedup_depend_on(tmp_path):
    (txn,) = list(
        trade_republic.parse_file(
            write(
                tmp_path,
                row(
                    type="CARD_TRANSACTION",
                    amount="-1264.000000",
                    name="EXTERIOO LOCHRISTI",
                    mcc_code="5712",
                    description="TR Card Transaction",
                ),
            ),
            ACCOUNTS,
        )
    )

    assert txn.meta["transaction_id"] == "63748ca0-13aa-4a29-a69a-0513c01dd166"
    assert txn.meta["mcc"] == "5712"
    assert txn.meta["type"] == "CARD_TRANSACTION"
    assert txn.payee == "EXTERIOO LOCHRISTI"


def test_counterparty_iban_is_recorded_so_transfers_are_detected(tmp_path):
    (txn,) = list(
        trade_republic.parse_file(
            write(
                tmp_path,
                row(counterparty_name="Cuelenaere Maurus", counterparty_iban="BE53123456789012"),
            ),
            ACCOUNTS,
        )
    )

    assert txn.meta["counterparty_iban"] == "BE53123456789012"
    assert txn.payee == "Cuelenaere Maurus"


def test_placeholder_description_falls_back_to_the_type(tmp_path):
    """ "No SEPA description provided" is noise; the transaction type is the useful label."""
    (txn,) = list(
        trade_republic.parse_file(
            write(tmp_path, row(description="No SEPA description provided")), ACCOUNTS
        )
    )

    assert txn.narration == "CUSTOMER_INBOUND"


def test_position_columns_are_recorded_when_present(tmp_path):
    """The export is cash-only today. If it stops being so, the omission must be visible."""
    (txn,) = list(
        trade_republic.parse_file(
            write(tmp_path, row(asset_class="STOCK", symbol="AAPL", shares="3", price="180.00")),
            ACCOUNTS,
        )
    )

    assert txn.meta["symbol"] == "AAPL"
    assert txn.meta["shares"] == "3"


def test_rows_that_move_nothing_are_skipped(tmp_path):
    path = write(tmp_path, row(amount="0.000000"), row())
    assert len(list(trade_republic.parse_file(path, ACCOUNTS))) == 1


@pytest.mark.parametrize("key", ["TRADE_REPUBLIC_FEE", "TRADE_REPUBLIC_TAX"])
def test_fee_and_tax_accounts_can_be_overridden(tmp_path, key):
    accounts = {**ACCOUNTS, key: "Expenses:Custom"}
    column = "fee" if key.endswith("FEE") else "tax"
    (txn,) = list(trade_republic.parse_file(write(tmp_path, row(**{column: "-10.00"})), accounts))

    assert any(p.account == "Expenses:Custom" for p in txn.postings)


def test_missing_account_is_an_error_not_a_guess(tmp_path):
    with pytest.raises(UnknownAccountError, match="trade_republic"):
        list(trade_republic.parse_file(write(tmp_path, row()), {}))
