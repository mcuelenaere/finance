"""Turn a Delta portfolio snapshot into Beancount price directives.

The split here matters. An agent fetches the snapshot over MCP — code cannot, and must not,
call a model or a network service at build time. Everything after that is deterministic: the
matching and the rendering live here, are tested, and produce the same file from the same
snapshot. The result is committed text, so the ledger stays reproducible and no price lookup
ever happens during a build.

Matching is the only interesting part. The ledger identifies an instrument by ISIN, while Delta
identifies it by ticker, so there is no shared key. Quantity is the bridge: when Delta tracks
the same portfolio it reports the same holding, and an exact quantity match is a strong signal.
Where several instruments share a quantity — plenty of positions are 2 or 5 shares — the name
breaks the tie, and anything still ambiguous is left unpriced rather than guessed at.
"""

from __future__ import annotations

import re
from datetime import date as Date
from decimal import Decimal
from typing import NamedTuple

#: Quotes are kept to four decimals: more is noise, less loses cheap instruments.
QUOTE = Decimal("0.0001")


class Quote(NamedTuple):
    commodity: str
    price: Decimal
    currency: str
    ticker: str
    name: str


class Rate(NamedTuple):
    """An exchange rate, for the currencies the ledger holds besides its own.

    Needed because **Beancount does not chain conversions.** A holding bought in dollars is
    valued by looking for a price in *dollars*; a price in euro does not help, and neither does
    a euro price plus a EUR/USD rate — the two are never composed. So a foreign-quoted holding
    needs a quote in its own currency, and then this to bring the total back to the ledger's.
    Verified directly: with only a euro quote, "at market value" reports the position as bare
    units, which reads as a holding that is worth nothing rather than one that is unpriced.
    """

    base: str
    rate: Decimal
    quote: str


class Unpriced(NamedTuple):
    commodity: str
    quantity: Decimal
    name: str
    reason: str


def _key(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _looks_like(one: str, other: str) -> bool:
    """Whether two instrument names plausibly describe the same thing."""
    a, b = _key(one), _key(other)
    return bool(a and b and (a[:8] in b or b[:8] in a))


def match(
    holdings: dict[str, Decimal],
    names: dict[str, str],
    assets: list[dict],
) -> tuple[list[Quote], list[Unpriced]]:
    """Pair ledger commodities with Delta assets.

    ``assets`` is the ``assets`` list from Delta's ``get_portfolio``, unmodified.
    """
    quotes: list[Quote] = []
    unpriced: list[Unpriced] = []

    for commodity, quantity in holdings.items():
        name = names.get(commodity, commodity)
        candidates = [
            asset
            for asset in assets
            if asset.get("holdings") and Decimal(str(asset["holdings"])) == quantity
        ]

        if not candidates:
            unpriced.append(Unpriced(commodity, quantity, name, "no holding of that size"))
            continue

        if len(candidates) == 1:
            chosen = candidates[0]
        else:
            named = [a for a in candidates if _looks_like(a.get("name", ""), name)]
            if len(named) != 1:
                unpriced.append(
                    Unpriced(commodity, quantity, name, f"{len(candidates)} assets share it")
                )
                continue
            chosen = named[0]

        value = (chosen.get("price") or {}).get("value")
        if not value:
            unpriced.append(Unpriced(commodity, quantity, name, "Delta has no quote"))
            continue

        quotes.append(
            Quote(
                commodity=commodity,
                price=Decimal(str(value)).quantize(QUOTE).normalize(),
                currency=(chosen.get("price") or {}).get("currency", "EUR"),
                ticker=chosen.get("shortCode", ""),
                name=chosen.get("name", ""),
            )
        )

    return sorted(quotes), sorted(unpriced)


def restate(
    quotes: list[Quote],
    lot_currencies: dict[str, set[str]],
    rates: list[Rate],
) -> list[Quote]:
    """Re-express each quote in the currency the holding's own lots are held in.

    **Beancount values a position in the currency it was bought in, and never composes two
    conversions.** A dollar-bought holding quoted only in euro is therefore reported as bare
    units — not as an error, and not as its cost, but as nothing at all, which reads on a
    balance sheet as a holding that is worthless rather than one that is unpriced.

    The snapshot arrives entirely in euro, because that is the only currency the tracker's
    portfolio endpoint reports. Converting back with the supplied rate reintroduces exactly the
    rate the tracker used to get there, so nothing is lost that was not already lost.

    A commodity bought in two currencies gets a quote in each: Beancount keys a price on the
    pair, so both are needed and neither is redundant.
    """
    factors = {(rate.base, rate.quote): rate.rate for rate in rates}
    factors.update({(rate.quote, rate.base): 1 / rate.rate for rate in rates})

    out: list[Quote] = []
    for quote in quotes:
        wanted = lot_currencies.get(quote.commodity) or {quote.currency}
        for currency in sorted(wanted):
            if currency == quote.currency:
                out.append(quote)
                continue
            factor = factors.get((quote.currency, currency))
            if factor is None:
                # No rate supplied, so the euro quote is kept rather than dropped: it is still
                # right for anything asking in euro, and inventing a rate would be worse.
                out.append(quote)
                continue
            restated = (quote.price * factor).quantize(QUOTE)
            out.append(quote._replace(price=restated, currency=currency))
    return out


def render(
    quotes: list[Quote],
    unpriced: list[Unpriced],
    *,
    as_of: Date,
    portfolio: str,
    rates: list[Rate] | None = None,
) -> str:
    """The prices file, including what could not be priced.

    Unpriced holdings are listed rather than dropped: their cost basis still counts toward net
    worth, so a silently missing quote reads as a portfolio that shrank.
    """
    lines = [
        ";; Market prices, fetched from Delta.",
        ";;",
        ";; Generated — but NOT by `finance build`, which never touches this file and will not",
        ";; refresh it. An agent fetches the snapshot over MCP and `finance prices` writes it",
        ";; here as text, so a build never performs a lookup and the ledger stays reproducible.",
        ";; Refresh with the `refresh-prices` skill.",
        ";;",
        f";; Portfolio: {portfolio}.  As of {as_of}.",
        f";; {len(quotes)} priced, {len(unpriced)} not.",
        "",
    ]
    if rates:
        lines += [
            ";; Exchange rates. Not decoration: Beancount values a holding in the currency it",
            ";; was bought in and never composes two conversions, so a dollar-quoted holding",
            ";; needs a dollar quote, and this is what brings the total back to euro.",
        ]
        for rate in rates:
            lines.append(f"{as_of} price {rate.base} {rate.rate} {rate.quote}")
        lines.append("")

    width = max((len(q.commodity) for q in quotes), default=0)
    for quote in quotes:
        label = f"{quote.ticker} — {quote.name}" if quote.ticker else quote.name
        lines.append(
            f"{as_of} price {quote.commodity:<{width}} {quote.price} {quote.currency}"
            f"  ; {label[:46]}"
        )

    if unpriced:
        lines += [
            "",
            ";; Not priced. Cost basis still counts toward net worth; only market value is",
            ";; missing, so these read as flat rather than as gone.",
        ]
        for item in unpriced:
            lines.append(
                f";;   {item.commodity:<14} {item.quantity:>10}  "
                f"{item.name[:38]:<38}  ({item.reason})"
            )

    return "\n".join(lines) + "\n"
