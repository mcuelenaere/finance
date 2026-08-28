"""Wirex activity exports.

Every fixture here is invented. The traps being covered are real, though, and each one is the
kind that produces a ledger that looks fine and is wrong: a ``Failed`` row that nevertheless
took the money, a balance field that is a placeholder on some row types and authoritative on
others, and an ordering key that only means anything within one pagination page.
"""

from datetime import date
from decimal import Decimal

import pytest

from finance.models import Balance, Open, Transaction
from finance.parsers import wirex
from finance.parsers.base import UnknownAccountError

ACCOUNTS = {
    "WIREX": "Assets:Cash:Wirex",
    "WIREX_POSITIONS": "Assets:Investments:Brokers:Wirex",
}
CASH = "Assets:Cash:Wirex"
POSITIONS = "Assets:Investments:Brokers:Wirex"
GAINS = "Income:CapitalGains:Crypto"

ETH_ACCOUNT = "aaaa11112222333344445555666677770"[:32]
EUR_ACCOUNT = "bbbb11112222333344445555666677770"[:32]

_UNSET = object()


def money(amount, currency, precision=6):
    return {"amount": amount, "currency": currency, "display_precision": precision}


def account_side(account_id, balance=None, currency="ETH", precision=6):
    side = {
        "account_id": account_id,
        "subject_type": "Account",
        "discriminator": "AccountTransactionSubjectDto",
    }
    if balance is not None:
        side["balance"] = money(balance, currency, precision)
    return side


def card_side(account_id=ETH_ACCOUNT, currency="ETH"):
    """A card row: the nested balance is always a zero placeholder, never a real balance."""
    return {
        "card_id": "cccc1111222233334444555566667777",
        "account": account_side(account_id, balance=0.0, currency=currency),
        "subject_type": "Card",
        "discriminator": "CardTransactionSubjectDto",
    }


def merchant_side(name="Widget Shop", merchant_id="dddd1111222233334444555566667777"):
    return {
        "merchant_id": merchant_id,
        "information": {"name": name},
        "location": {"full_address": "1 Example Street, 1000 Brussels, Belgium"},
        "processing_category": "GeneralRetail",
        "subject_type": "Merchant",
        "discriminator": "MerchantTransactionSubjectDto",
    }


def purchase(
    row_id,
    when,
    spent,
    spent_currency,
    charged,
    charged_currency,
    status="Success",
    reason="Cleared",
    funding=ETH_ACCOUNT,
    **extra,
):
    row = {
        "id": row_id,
        "transaction_id": row_id,
        "type": "PurchaseEPos",
        "created_at": when,
        "source": card_side(funding, "ETH" if spent_currency == "ETH" else spent_currency),
        "target": merchant_side(),
        "status": status,
        "discriminator": "TwoWayTransactionalActivityDto",
    }
    if reason is not None:
        row["status_reason"] = reason
    if spent is not None:
        row["amount_from"] = money(spent, spent_currency)
    if charged is not None:
        row["amount_to"] = money(charged, charged_currency, 2)
    row.update(extra)
    return row


def payload(*rows):
    return {"page": {"data": list(rows)}, "count": len(rows), "errors": []}


def parse(*rows, accounts=_UNSET):
    resolved = ACCOUNTS if accounts is _UNSET else accounts
    return list(wirex.parse_from_payload(payload(*rows), resolved, source_file="tx_1.json"))


def transactions(directives):
    return [d for d in directives if isinstance(d, Transaction)]


def balances(directives):
    return [d for d in directives if isinstance(d, Balance)]


def sale(directives):
    """The crypto half of a split movement — selling or buying the units."""
    (found,) = [t for t in transactions(directives) if t.narration.split()[0] in ("Sold", "Bought")]
    return found


def spend(directives):
    """The half naming who was paid."""
    (found,) = [
        t for t in transactions(directives) if t.narration.split()[0] not in ("Sold", "Bought")
    ]
    return found


# --- the trap that made the balance drift ---------------------------------------------


def test_a_failed_row_that_names_an_amount_still_debits():
    """Rows in the real export are marked ``Failed`` and took the money anyway, each refunded
    later. Skipping them on status leaves the derived balance adrift by a growing sum, and
    nothing else in the file contradicts it — the drift is invisible until it is compared with
    a stated balance."""
    row = purchase(
        "r1",
        "2024-05-02T10:00:00.000Z",
        0.01529,
        "ETH",
        38.65,
        "EUR",
        status="Failed",
        reason=None,
    )

    directives = parse(row)

    held, proceeds, gain = sale(directives).postings
    assert held.units == (Decimal("-0.01529"), "ETH")
    assert proceeds.units == (Decimal("38.65"), "EUR")
    assert gain.account == GAINS
    paid, expense = spend(directives).postings
    assert paid.units == (Decimal("-38.65"), "EUR")
    assert expense.account is None
    # And it must not read like an ordinary purchase, or a rule files it as spending while its
    # refund reads as income.
    assert spend(directives).narration == "Card payment (failed)"


def test_a_failed_row_with_no_amount_books_nothing():
    row = purchase(
        "r2",
        "2024-05-02T10:00:00.000Z",
        None,
        "ETH",
        38.65,
        "EUR",
        status="Failed",
        reason=None,
    )

    assert transactions(parse(row)) == []


def test_insufficient_funds_books_nothing():
    row = purchase(
        "r3",
        "2024-05-02T10:00:00.000Z",
        None,
        "ETH",
        12.00,
        "EUR",
        status="Failed",
        reason="InsufficientFunds",
    )

    assert transactions(parse(row)) == []


def test_a_one_way_row_is_decided_on_status_not_on_naming_an_amount():
    """A rejected deposit still states its amount, so the rule that saves the failed purchases
    would wrongly credit this one."""
    row = {
        "id": "r4",
        "type": "SepaIn",
        "created_at": "2024-06-06T05:49:16.178Z",
        "amount": money(10.0, "EUR", 2),
        "source": {
            "iban": "BE53123456789012",
            "owner_name": "Example Bank",
            "subject_type": "International",
            "discriminator": "InternationalRequisitesTransactionSubjectDto",
        },
        "target": account_side(EUR_ACCOUNT, balance=18.57, currency="EUR", precision=2),
        "status": "Failed",
        "status_reason": "RisksValidationFailure",
        "discriminator": "OneWayTransactionalActivityDto",
    }

    assert transactions(parse(row)) == []


# --- what a card purchase means --------------------------------------------------------


def test_a_card_purchase_is_two_transactions_a_sale_and_a_purchase():
    """One row, two economic events. Booked as one transaction, a realised capital gain and a
    grocery bill share an entry — so every merchant the card ever paid turns up in the
    capital-gains account. Salary is not attached to the things it buys, and nor is this."""
    row = purchase("r5", "2024-05-02T10:00:00.000Z", 0.015213, "ETH", 37.71, "EUR")

    directives = parse(row)
    assert len(transactions(directives)) == 2

    held, proceeds, gain = sale(directives).postings
    assert held.account == POSITIONS
    assert held.units == (Decimal("-0.015213"), "ETH")
    assert held.cost.number is None, "the booking method picks the lot"
    assert proceeds.account == CASH
    assert proceeds.units == (Decimal("37.71"), "EUR")
    assert gain.account == GAINS
    assert gain.units is None, "derived from the lot, not stated"
    assert sale(directives).payee == "Wirex", "nobody was paid in this half"

    paid, expense = spend(directives).postings
    assert paid.account == CASH
    assert paid.units == (Decimal("-37.71"), "EUR"), "the two halves net to nothing here"
    assert expense.account is None, "the merchant is the rules engine's to decide"
    assert expense.units == (Decimal("37.71"), "EUR")
    assert spend(directives).payee == "Widget Shop"


def test_the_two_halves_share_a_link_so_the_pair_stays_readable():
    row = purchase("r5b", "2024-05-02T10:00:00.000Z", 0.015213, "ETH", 37.71, "EUR")

    directives = parse(row)

    assert sale(directives).links == spend(directives).links == frozenset({"wirex-r5b"})


def test_the_crypto_half_carries_no_merchant_and_the_merchant_half_no_blockchain():
    """A merchant id on a transaction about selling ether reads as though the ether had been
    sold *to* the merchant."""
    row = purchase("r5c", "2024-05-02T10:00:00.000Z", 0.015213, "ETH", 37.71, "EUR")

    directives = parse(row)

    assert "merchant_id" not in sale(directives).meta
    assert "merchant_id" in spend(directives).meta
    assert sale(directives).meta["transaction_id"] == "r5c:funding"
    assert spend(directives).meta["transaction_id"] == "r5c"


def test_a_disposal_pins_the_cost_currency():
    """Proceeds in dollars would otherwise have Beancount infer a dollar cost basis, which
    matches none of the euro-cost lots and fails with 'no position matches'."""
    row = purchase("r6", "2024-05-02T10:00:00.000Z", 0.046397, "ETH", 109.99, "USD")

    directives = parse(row)

    held, proceeds, _ = sale(directives).postings
    assert held.cost.number is None
    assert held.cost.currency == "EUR"
    assert proceeds.units == (Decimal("109.99"), "USD")
    assert spend(directives).postings[1].units == (Decimal("109.99"), "USD")


def test_a_euro_funded_purchase_books_what_left_the_account():
    """The account paid euro and the merchant charged pounds, and no rate is quoted for that
    pair — so the euro is the expense and the pound figure is kept as metadata."""
    row = purchase("r7", "2024-05-02T10:00:00.000Z", 1.39, "EUR", 1.20, "GBP", funding=EUR_ACCOUNT)

    (txn,) = transactions(parse(row))

    cash, expense = txn.postings
    assert cash.account == CASH
    assert cash.units == (Decimal("-1.39"), "EUR")
    assert expense.account is None
    assert expense.units is None, "elided, so the rules engine's account takes the euro"
    # As the JSON writes it: a number, so no trailing zero to preserve.
    assert txn.meta["foreign_amount"] == "1.2 GBP"


def test_a_fee_charged_on_top_is_added_to_what_left():
    row = purchase(
        "r8",
        "2024-05-02T10:00:00.000Z",
        1.00,
        "EUR",
        1.00,
        "EUR",
        funding=EUR_ACCOUNT,
        fee=money(0.25, "EUR", 2),
    )

    (txn,) = transactions(parse(row))

    cash, _ = txn.postings
    assert cash.units == (Decimal("-1.25"), "EUR")
    assert txn.meta["fee"] == "0.25 EUR"


# --- arriving units, and what they cost ------------------------------------------------


def test_units_arriving_from_outside_are_booked_at_zero():
    """Nothing in this export says what they cost — the wallet they came from has no export
    here. The docstring states what that does to the gain."""
    row = {
        "id": "r9",
        "type": "CryptoTransferIn",
        "created_at": "2024-05-01T10:00:00.000Z",
        "amount": money(0.5, "ETH"),
        "source": {
            "address": "0x0000000000000000000000000000000000000001",
            "subject_type": "CryptoRequisites",
            "discriminator": "CryptoRequisitesTransactionSubjectDto",
        },
        "target": account_side(ETH_ACCOUNT, balance=0.5),
        "status": "Success",
        "details": {"hash": "0xabc", "blockchain": "Ethereum"},
        "discriminator": "OneWayTransactionalActivityDto",
    }

    (txn,) = transactions(parse(row))

    held, counter = txn.postings
    assert held.units == (Decimal("0.5"), "ETH")
    assert held.cost.number == Decimal(0)
    assert held.cost.currency == "EUR"
    assert held.cost.total is True
    assert counter.account is None
    # An explicit zero rather than an elided amount. The units cost nothing, so the transaction
    # already balances — and Beancount answers an elided posting it cannot give a number by
    # DELETING it, with no error at all. The wallet the units came from would then have no
    # posting in the one transaction that exists to record where they came from.
    assert counter.units == (Decimal(0), "EUR")
    assert txn.meta["blockchain"] == "Ethereum"
    assert txn.meta["crypto_address"].startswith("0x")


def test_a_refund_returns_units_and_credits_the_spending():
    """It reads backwards from the purchase it reverses: the merchant gives back fiat and the
    account receives ether, at the rate of the refund day rather than the purchase day."""
    row = {
        "id": "r10",
        "type": "Refund",
        "created_at": "2024-05-20T10:00:00.000Z",
        "amount_from": money(38.65, "EUR", 2),
        "amount_to": money(0.015163, "ETH"),
        "source": merchant_side(),
        "target": card_side(),
        "status": "Success",
        "discriminator": "TwoWayTransactionalActivityDto",
    }

    directives = parse(row)

    held, spent, gain = sale(directives).postings
    assert held.units == (Decimal("0.015163"), "ETH")
    assert held.cost.number == Decimal(0)
    assert spent.units == (Decimal("-38.65"), "EUR"), "the euro buys the ether back"
    assert gain.account == GAINS
    assert sale(directives).narration == "Bought ETH"

    received, credit = spend(directives).postings
    assert received.units == (Decimal("38.65"), "EUR")
    assert credit.account is None
    assert credit.units == (Decimal("-38.65"), "EUR"), "credits the original spending"


def test_an_exchange_of_units_for_euro_lands_the_proceeds_in_cash():
    row = {
        "id": "r11",
        "type": "Exchange",
        "created_at": "2024-05-21T10:00:00.000Z",
        "amount_from": money(0.3, "ETH"),
        "amount_to": money(1220.18, "EUR", 2),
        "fee": money(0.0045, "ETH"),
        "source": account_side(ETH_ACCOUNT, balance=0.659919),
        "target": account_side(EUR_ACCOUNT, balance=1220.18, currency="EUR", precision=2),
        "status": "Success",
        "status_reason": "Done",
        "discriminator": "TwoWayTransactionalActivityDto",
    }

    (txn,) = transactions(parse(row))

    held, cash, gain = txn.postings
    assert held.units == (Decimal("-0.3045"), "ETH"), "the fee leaves as well"
    assert cash.account == CASH
    assert cash.units == (Decimal("1220.18"), "EUR")
    assert gain.account == GAINS


def test_a_sepa_transfer_out_names_the_far_side_for_the_rules_engine():
    row = {
        "id": "r12",
        "type": "SepaOut",
        "created_at": "2024-05-15T10:00:00.000Z",
        "amount": money(150.0, "EUR", 2),
        "source": account_side(EUR_ACCOUNT, balance=0.0, currency="EUR", precision=2),
        "target": {
            "iban": "BE53123456789012",
            "owner_name": "A Person",
            "subject_type": "International",
            "discriminator": "InternationalRequisitesTransactionSubjectDto",
        },
        "status": "Success",
        "discriminator": "OneWayTransactionalActivityDto",
    }

    (txn,) = transactions(parse(row))

    cash, counter = txn.postings
    assert cash.units == (Decimal("-150.0"), "EUR")
    assert counter.account is None
    # own_accounts routes a movement between two of the owner's accounts through the clearing
    # account before any rule is consulted, which is what this key is for.
    assert txn.meta["counterparty_iban"] == "BE53123456789012"


# --- balance assertions, and which stated balances can be trusted ----------------------


def test_a_stated_balance_becomes_an_assertion_with_the_precision_as_tolerance():
    """Wirex rounds the balance to the precision it declares while the amounts carry full
    precision, so demanding exactness fails in the seventh decimal."""
    first = purchase("a", "2024-05-01T09:00:00.000Z", 0.001, "ETH", 2.50, "EUR")
    anchor = {
        "id": "b",
        "type": "CryptoTransferIn",
        "created_at": "2024-05-02T10:00:00.000Z",
        "amount": money(0.5, "ETH"),
        "source": {"address": "0x1", "discriminator": "CryptoRequisitesTransactionSubjectDto"},
        "target": account_side(ETH_ACCOUNT, balance=0.499, precision=6),
        "status": "Success",
        "discriminator": "OneWayTransactionalActivityDto",
    }
    last = purchase("c", "2024-05-03T09:00:00.000Z", 0.002, "ETH", 5.00, "EUR")

    (balance,) = balances(parse(first, anchor, last))

    assert balance.account == POSITIONS
    assert balance.date == date(2024, 5, 3), "asserted the day after"
    assert balance.amount == (Decimal("0.499"), "ETH")
    assert balance.tolerance == Decimal("0.000001")


def test_a_fee_rows_zero_balance_is_not_asserted():
    """A card-order fee names the account as its counterparty, complete with a balance block —
    but that block reads zero whatever the account holds. Asserting it claims the account was
    emptied."""
    first = purchase("a", "2024-05-01T09:00:00.000Z", 0.001, "ETH", 2.50, "EUR")
    fee = {
        "id": "b",
        "type": "CardOrderFee",
        "created_at": "2024-05-02T10:00:00.000Z",
        "amount_from": money(0.01741, "ETH"),
        "amount_to": money(45.0, "EUR", 2),
        "source": account_side(ETH_ACCOUNT, balance=0.0),
        "status": "Success",
        "details": {"card_format": "Metal"},
        "discriminator": "TwoWayTransactionalActivityDto",
    }
    last = purchase("c", "2024-05-03T09:00:00.000Z", 0.002, "ETH", 5.00, "EUR")

    directives = parse(first, fee, last)

    assert balances(directives) == []
    # Still booked: the fee really was charged. Each row is a sale plus a payment.
    assert len(transactions(directives)) == 6


def test_a_balance_on_the_files_own_edge_is_not_asserted():
    """The export paginates and a page boundary can fall inside a day, so the rest of that day
    may be in the neighbouring file. Only days this file holds entirely are assertable."""
    edge = {
        "id": "a",
        "type": "CryptoTransferIn",
        "created_at": "2024-05-01T10:00:00.000Z",
        "amount": money(0.5, "ETH"),
        "source": {"address": "0x1", "discriminator": "CryptoRequisitesTransactionSubjectDto"},
        "target": account_side(ETH_ACCOUNT, balance=0.5),
        "status": "Success",
        "discriminator": "OneWayTransactionalActivityDto",
    }
    later = purchase("b", "2024-05-02T09:00:00.000Z", 0.002, "ETH", 5.00, "EUR")

    assert balances(parse(edge, later)) == []


# --- identity and ordering -------------------------------------------------------------


def test_the_row_id_is_the_transaction_id_not_the_transaction_id_field():
    """Some rows carry a different value in each, and it is ``id`` that is unique per row.
    Deduplicating the overlapping pagination pages rests entirely on this."""
    row = purchase("row-id", "2024-05-02T10:00:00.000Z", 0.001, "ETH", 2.50, "EUR")
    row["transaction_id"] = "shared-id"

    assert spend(parse(row)).meta["transaction_id"] == "row-id"


def test_ordering_is_absolute_so_it_survives_being_split_across_pages():
    """A per-file counter says nothing about position in another page, and which page's copy
    survives deduplication is arbitrary — so a purchase could sort before the transfer that
    funded it and find no lot to reduce."""
    early = purchase("a", "2024-05-02T10:00:00.000Z", 0.001, "ETH", 2.50, "EUR")
    late = purchase("b", "2024-05-02T11:00:00.000Z", 0.002, "ETH", 5.00, "EUR")

    # Parsed alone, each is the only row in its file — a counter would give both sequence 0.
    assert spend(parse(early)).meta["sequence"] < spend(parse(late)).meta["sequence"]


def test_the_positions_account_is_opened_for_fifo_at_the_earliest_holding():
    rows = [
        purchase("a", "2024-05-02T10:00:00.000Z", 0.001, "ETH", 2.50, "EUR"),
        purchase("b", "2024-06-02T10:00:00.000Z", 0.002, "ETH", 5.00, "EUR"),
    ]

    (opened,) = [d for d in parse(*rows) if isinstance(d, Open)]

    assert opened.account == POSITIONS
    assert opened.booking == "FIFO"
    assert opened.date == date(2024, 5, 2)


def test_a_euro_only_export_opens_no_positions_account():
    row = purchase("a", "2024-05-02T10:00:00.000Z", 1.00, "EUR", 1.00, "EUR", funding=EUR_ACCOUNT)

    assert [d for d in parse(row) if isinstance(d, Open)] == []


def test_the_positions_account_defaults_to_a_sibling_of_the_cash_account():
    """Still tested because nothing else exercises it now that this ledger declares the key."""
    row = purchase("a", "2024-05-02T10:00:00.000Z", 0.001, "ETH", 2.50, "EUR")

    directives = parse(row, accounts={"WIREX": "Assets:Crypto:Wirex:Cash"})

    assert sale(directives).postings[0].account == "Assets:Crypto:Wirex:Positions"


def test_an_undeclared_account_raises_rather_than_being_guessed():
    with pytest.raises(UnknownAccountError, match="wirex"):
        parse(
            purchase("a", "2024-05-02T10:00:00.000Z", 0.001, "ETH", 2.50, "EUR"),
            accounts={},
        )


def test_merchant_detail_is_kept_for_rules_to_match_on():
    row = purchase(
        "a",
        "2024-05-02T10:00:00.000Z",
        0.001,
        "ETH",
        2.50,
        "EUR",
        original_category={"id": 3, "name": "Groceries"},
        comment="WIDGET SHOP,BRUSSELS,BEL",
        reward={
            "type": "CardTransactionCashback",
            "status": "Active",
            "amount": money(89.0, "WXT"),
        },
    )

    txn = spend(parse(row))

    assert txn.meta["category"] == "Groceries"
    assert txn.meta["merchant_address"].endswith("Belgium")
    assert txn.meta["processing_category"] == "GeneralRetail"
    assert txn.meta["comment"] == "WIDGET SHOP,BRUSSELS,BEL"
    # Recorded, not booked: nothing values it and the WXT balance is a placeholder.
    assert txn.meta["reward_wxt"] == Decimal("89.0")
    assert all(
        posting.units is None or posting.units.currency != "WXT"
        for entry in transactions(parse(row))
        for posting in entry.postings
    )
