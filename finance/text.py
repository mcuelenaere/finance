"""Small text normalizers shared by the parsers and the rules engine."""

from __future__ import annotations

import re


def normalize_identifier(value: str) -> str:
    """Canonicalize an account identifier.

    IBANs appear both spaced (``BE12 3456 7890 1234``, as in Belfius filenames and rows) and
    unspaced, so they have to be folded before comparison.
    """
    return re.sub(r"\s+", "", value).upper()


def normalize_text(value: str) -> str:
    """Collapse whitespace runs so rules can be written against readable text.

    Bank exports pad fields with long runs of spaces; matching on the raw value makes rules
    brittle and unreadable, so rules are written against the collapsed form.
    """
    return re.sub(r"\s+", " ", value).strip()
