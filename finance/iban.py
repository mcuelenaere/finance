"""Belgian account identifiers, which come in two spellings for the same account.

A Belgian account has a domestic number (``123-4567890-12``) and an IBAN
(``BE53 1234 5678 9012``) built from it. Which one an export uses is not the account holder's
choice: ING names its own accounts domestically while Belfius names the very same accounts by
IBAN, so the two spellings turn up in different statements for one account.

That matters more than it looks. ``own_accounts`` is what distinguishes a transfer between your
own accounts from an expense, and a registry that knows only one spelling silently misses the
other — booking your own money as spending, which is the error that inflates net worth without
anything appearing broken.
"""

from __future__ import annotations

import re

#: Domestic form: three-digit bank, seven-digit account, two-digit national check.
BBAN = re.compile(r"^(\d{3})-?(\d{7})-?(\d{2})$")

#: Belgian IBAN: BE, two check digits, then the twelve BBAN digits.
IBAN = re.compile(r"^BE(\d{2})(\d{12})$")


def _iban_check_digits(bban_digits: str) -> int:
    """The two IBAN check digits for a Belgian BBAN, per the ISO 13616 mod-97 rule."""
    # Move the country code and placeholder check to the end, with B=11 and E=14.
    return 98 - int(f"{bban_digits}111400") % 97


def to_iban(identifier: str) -> str | None:
    """Domestic account number to IBAN, or None if it is not one."""
    match = BBAN.match(identifier)
    if not match:
        return None
    digits = "".join(match.groups())
    return f"BE{_iban_check_digits(digits):02d}{digits}"


def to_bban(identifier: str) -> str | None:
    """Belgian IBAN to domestic account number, or None if it is not one.

    The IBAN's own check digits are verified, so a foreign IBAN that happens to start ``BE``
    cannot be turned into a domestic number that means nothing.
    """
    match = IBAN.match(identifier)
    if not match:
        return None
    check, digits = match.groups()
    if int(check) != _iban_check_digits(digits):
        return None
    return f"{digits[:3]}-{digits[3:10]}-{digits[10:]}"


def variants(identifier: str) -> list[str]:
    """Every spelling of ``identifier``, most specific first.

    Returns just the input for anything that is not a Belgian account, so callers can use this
    unconditionally.
    """
    found = [identifier]
    for alternate in (to_iban(identifier), to_bban(identifier)):
        if alternate and alternate not in found:
            found.append(alternate)
            # The domestic form appears both with and without its separators.
            stripped = alternate.replace("-", "")
            if stripped != alternate and stripped not in found:
                found.append(stripped)
    return found
