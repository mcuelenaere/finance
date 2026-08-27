"""Matching ledger commodities to Delta assets.

The ledger identifies an instrument by ISIN and Delta by ticker, so there is no shared key and
the quantity has to do the work. These tests pin down when that is trustworthy and when the
answer should be "unpriced" instead of a guess.
"""

from datetime import date
from decimal import Decimal

from finance.prices import match, render


def asset(ticker, name, holdings, price="10.00", currency="EUR"):
    return {
        "shortCode": ticker,
        "name": name,
        "holdings": holdings,
        "price": {"value": price, "currency": currency} if price else {"value": None},
    }


def test_a_unique_quantity_is_enough():
    quotes, unpriced = match(
        {"IE00B4L5Y983": Decimal("3368")},
        {"IE00B4L5Y983": "ISHARES CORE MSCI WORLD UCITS ETF USD"},
        [asset("IWDA.AS", "iShares Core MSCI World UCITS ETF", "3368", "127.14")],
    )

    assert unpriced == []
    assert quotes[0].commodity == "IE00B4L5Y983"
    assert quotes[0].price == Decimal("127.14")
    assert quotes[0].ticker == "IWDA.AS"


def test_the_name_breaks_a_tie_between_equal_quantities():
    """Plenty of positions are 2 or 5 shares, so quantity alone is often ambiguous."""
    quotes, unpriced = match(
        {"US67066G1040": Decimal("9")},
        {"US67066G1040": "NVIDIA CORPORATION"},
        [asset("AAPL", "Apple", "9"), asset("NVDA", "NVIDIA", "9", "195.61")],
    )

    assert unpriced == []
    assert quotes[0].ticker == "NVDA"


def test_an_unresolvable_tie_is_left_unpriced():
    """A wrong price is worse than a missing one: it misstates net worth silently."""
    quotes, unpriced = match(
        {"US0000000001": Decimal("2")},
        {"US0000000001": "SOME COMPANY"},
        [asset("AAA", "Alpha", "2"), asset("BBB", "Beta", "2")],
    )

    assert quotes == []
    assert unpriced[0].reason == "2 assets share it"


def test_a_holding_delta_does_not_track_is_unpriced():
    quotes, unpriced = match(
        {"DE0001141802": Decimal("8305")},
        {"DE0001141802": "BUNDESOBL.V.19/24"},
        [asset("IWDA.AS", "iShares Core MSCI World", "3368")],
    )

    assert quotes == []
    assert unpriced[0].reason == "no holding of that size"


def test_an_asset_delta_cannot_quote_is_unpriced():
    """Delisted and acquired holdings sit in the portfolio with a null price."""
    quotes, unpriced = match(
        {"US57778K1051": Decimal("5")},
        {"US57778K1051": "MAXAR TECHNOLOGIES INC"},
        [asset("MAXR", "Maxar Technologies", "5", price=None)],
    )

    assert quotes == []
    assert unpriced[0].reason == "Delta has no quote"


def test_render_lists_what_could_not_be_priced():
    """Dropping them silently would read as a portfolio that shrank."""
    quotes, unpriced = match(
        {"A1": Decimal("1"), "B2": Decimal("7")},
        {"A1": "Alpha", "B2": "Beta"},
        [asset("AAA", "Alpha", "1", "12.5")],
    )
    out = render(quotes, unpriced, as_of=date(2026, 8, 28), portfolio="Investments")

    assert "2026-08-28 price A1 12.5 EUR" in out
    assert "; AAA — Alpha" in out
    assert ";;   B2" in out
    assert "no holding of that size" in out
    assert "NOT by `finance build`" in out


def test_render_is_stable_for_the_same_snapshot():
    quotes, unpriced = match(
        {"B2": Decimal("2"), "A1": Decimal("1")},
        {},
        [asset("AAA", "Alpha", "1", "12.5"), asset("BBB", "Beta", "2", "7.25")],
    )
    once = render(quotes, unpriced, as_of=date(2026, 8, 28), portfolio="Investments")
    twice = render(quotes, unpriced, as_of=date(2026, 8, 28), portfolio="Investments")

    assert once == twice
    assert once.index("price A1") < once.index("price B2")  # sorted, not input order
