"""Belgian account identifiers.

The two spellings of one account are not interchangeable in the data: ING names its own
accounts domestically while Belfius names the same accounts by IBAN. A registry that knows only
one spelling books the account holder's own transfers as expenses.
"""

import pytest

from finance.iban import to_bban, to_iban, variants


@pytest.mark.parametrize(
    "bban,iban",
    [
        # Fake but check-digit-valid, computed the same way a bank would.
        ("123-4567890-12", "BE53123456789012"),
        ("063-1234567-89", "BE46063123456789"),
    ],
)
def test_round_trip(bban, iban):
    assert to_iban(bban) == iban
    assert to_bban(iban) == bban


def test_iban_check_digits_are_verified():
    """A foreign IBAN starting BE must not be turned into a meaningless domestic number."""
    valid = to_iban("123-4567890-12")
    assert to_bban(valid) == "123-4567890-12"

    # Same digits, wrong check — must be refused rather than silently converted.
    broken = "BE00" + valid[4:]
    assert to_bban(broken) is None


@pytest.mark.parametrize("value", ["", "NOTANIBAN", "DE21500110012345678901", "12345"])
def test_non_belgian_identifiers_are_left_alone(value):
    assert to_iban(value) is None
    assert to_bban(value) is None
    assert variants(value) == [value]


def test_variants_covers_both_spellings_and_the_unseparated_form():
    both = variants("123-4567890-12")

    assert both[0] == "123-4567890-12"
    assert "BE53123456789012" in both


def test_variants_from_the_iban_side():
    both = variants("BE53123456789012")

    assert both[0] == "BE53123456789012"
    assert "123-4567890-12" in both
    # Exports also write the domestic number without separators.
    assert "123456789012" in both


def test_unseparated_domestic_number_is_recognized():
    assert to_iban("123456789012") == "BE53123456789012"
