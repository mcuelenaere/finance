from decimal import Decimal
from io import StringIO

import pytest

from finance.parsers import ing_be
from finance.parsers.base import UnknownAccountError

ACCOUNTS = {"390-1234567-89": "Assets:ING:Checking"}

CSV_HEADER = (
    "Rekeningnummer;Naam van de rekening;Rekening tegenpartij;Omzetnummer;Boekingsdatum;"
    "Valutadatum;Bedrag;Munteenheid;Omschrijving;Detail van de omzet;Bericht"
)

# Each case is (csv row, expected payee, expected counterparty iban, amount, date).
# The counterparty extraction these exercise is the most valuable logic in the repo — it
# recovers merchant names from free-text that ING provides no structured field for.
TESTCASES = (
    pytest.param(
        r"390-1234567-89;DE H JOHN DOE;;159;01/04/2019;03/04/2019;-10,44;EUR;"
        r"Aankoop Bancontact/Mister Cash                                    "
        r"            03/04 - 12.15 uur - PROXY DELHAIZE K - BRUGGE - BEL       "
        r"                      Kaartnummer 6703 39XX XXXX 1234 5              ;;",
        "PROXY DELHAIZE K - BRUGGE - BEL",
        None,
        Decimal("-10.44"),
        "2019-04-03",
        id="bancontact-card-payment",
    ),
    pytest.param(
        r"390-1234567-89;DE H JOHN DOE;;17;04/09/2009;04/09/2009;-25,12;EUR;"
        r"Europese overschrijving                ;Betalingsopdracht:            "
        r"                                                   Begunstigde:      "
        r"                                                                 "
        r"IBAN : DE12345678901234567890                                        "
        r"           TESSA D. FOWLER                                           ;",
        None,
        "DE12345678901234567890",
        Decimal("-25.12"),
        "2009-09-04",
        id="sepa-transfer-iban-only",
    ),
    pytest.param(
        r"390-1234567-89;DE H JOHN DOE;;131;17/07/2017;17/07/2017;-321,12;EUR;"
        r"Europese overschrijving      ;Overschrijving Home'Bank               "
        r"                                         Ten gunste van: Someone UK  "
        r"                                                             "
        r"IBAN: DE1234567890123456789                                          "
        r"            Mededeling:                     FOOBAR                   ;",
        "Someone UK",
        "DE1234567890123456789",
        Decimal("-321.12"),
        "2017-07-17",
        id="sepa-transfer-name-and-iban",
    ),
    # ING renames its payment channels freely, and the counterparty used to be read only for
    # a hard-coded list of them. These four differ from a listed channel by a colon or a word,
    # and every one of them silently lost its counterparty — which for a transfer to an own
    # account means both legs land and the balance doubles.
    pytest.param(
        r"390-1234567-89;DE H JOHN DOE;;411;21/02/2020;21/02/2020;-10000,00;EUR;"
        r"Overschrijving in euro (SEPA) Home'Bank                              "
        r"           Naar: Stichting DEGIRO - NL12ABNA3456789012               "
        r"                      Mededeling: inboeking degiro                   ;;",
        "Stichting DEGIRO",
        "NL12ABNA3456789012",
        Decimal("-10000.00"),
        "2020-02-21",
        id="sepa-home-bank-without-the-colon",
    ),
    pytest.param(
        r"390-1234567-89;DE H JOHN DOE;;412;05/09/2021;05/09/2021;-2500,00;EUR;"
        r"Overschrijving in euro (SEPA): ING app                               "
        r"           - 2.500,00                                                "
        r"           Naar: John Doe - DE12345678901234567890                   ;;",
        "John Doe",
        "DE12345678901234567890",
        Decimal("-2500.00"),
        "2021-09-05",
        id="sepa-ing-app",
    ),
    pytest.param(
        r"390-1234567-89;DE H JOHN DOE;;413;03/09/2022;03/09/2022;-1000,00;EUR;"
        r"Instantoverschrijving in euro: Home'Bank                             "
        r"           - 1.000,00                                                "
        r"           Naar: Zichtrekening - BE12345678901234                    "
        r"           Instant op 03/09 - 15:53:07                               ;;",
        "Zichtrekening",
        "BE12345678901234",
        Decimal("-1000.00"),
        "2022-09-03",
        id="instant-transfer",
    ),
    pytest.param(
        r"390-1234567-89;DE H JOHN DOE;;414;11/06/2023;11/06/2023;-4,00;EUR;"
        r"Payconiq-betaling                                                    "
        r"           - 4,00                                                    "
        r"           Naar: Jane Roe - BE98765432109876                         "
        r"           Mededeling: By Payconiq                                   ;;",
        "Jane Roe",
        "BE98765432109876",
        Decimal("-4.00"),
        "2023-06-11",
        id="payconiq",
    ),
)


@pytest.mark.parametrize("row,payee,iban,amount,date", TESTCASES)
def test_counterparty_extraction(row, payee, iban, amount, date):
    (txn,) = tuple(ing_be.parse_from_io(StringIO(f"{CSV_HEADER}\n{row}"), ACCOUNTS))

    assert txn.payee == payee
    assert txn.meta.get("counterparty_iban") == iban
    assert txn.date.isoformat() == date

    own, unresolved = txn.postings
    assert own.account == "Assets:ING:Checking"
    assert own.units.number == amount
    assert own.units.currency == "EUR"
    # The counter-posting is left for the rules engine, with an elided amount.
    assert unresolved.account is None
    assert unresolved.units is None


def test_narration_is_the_summary_not_the_raw_detail_block():
    row = (
        r"390-1234567-89;DE H JOHN DOE;;17;04/09/2009;04/09/2009;-25,12;EUR;"
        r"Europese overschrijving   ;Betalingsopdracht:    IBAN : DE12345678901234567890  ;"
    )
    (txn,) = tuple(ing_be.parse_from_io(StringIO(f"{CSV_HEADER}\n{row}"), ACCOUNTS))

    assert txn.narration == "Europese overschrijving"
    assert txn.meta["detail"].startswith("Betalingsopdracht:")


def test_thousands_separator_amount():
    """Amounts over a thousand use `1.234,56`, which the old parser could not read."""
    row = (
        r"390-1234567-89;DE H JOHN DOE;BE12345678901234;7;01/04/2019;03/04/2019;"
        r"-1.234,56;EUR;Europese overschrijving;;"
    )
    (txn,) = tuple(ing_be.parse_from_io(StringIO(f"{CSV_HEADER}\n{row}"), ACCOUNTS))

    assert txn.postings[0].units.number == Decimal("-1234.56")


def test_unknown_account_is_an_error_not_a_guess():
    row = r"999-9999999-99;X;;1;01/04/2019;03/04/2019;-1,00;EUR;Europese overschrijving;;"
    with pytest.raises(UnknownAccountError, match="999-9999999-99"):
        tuple(ing_be.parse_from_io(StringIO(f"{CSV_HEADER}\n{row}"), ACCOUNTS))
