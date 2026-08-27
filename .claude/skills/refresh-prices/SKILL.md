---
name: refresh-prices
description: Refresh market prices for ledger holdings from a Delta portfolio via MCP, writing them as Beancount price directives. Use when net worth shows cost basis instead of market value, when prices.beancount is stale, or when the user asks to update prices, revalue holdings, or see what the portfolio is worth today.
---

# Refreshing market prices from Delta

Holdings are booked at cost. Without `price` directives, Fava and `bean-query` report what was
paid rather than what it is worth, so net worth drifts further from reality the longer it has
been since the last refresh.

**You fetch; the code decides.** Your only job is to call the MCP tools and save the snapshot.
Matching and rendering happen in `finance prices`, which is deterministic and tested — so the
same snapshot always produces the same file, and no build ever performs a lookup. That split is
the whole point: a price fetched at build time would make last month's numbers change under the
user.

## Steps

**1. Find the portfolio.**

```
mcp__…__list_portfolios
```

Use the one the user tracks their holdings in — `Investments` unless they say otherwise. Note
there are several others (`OG Crypto`, `Pension fund`, `Options`), so do not aggregate blindly.

**2. Fetch the snapshot.**

```
mcp__…__get_portfolio  { portfolioId: <id> }
```

**3. Save it verbatim** to a scratch file as JSON — the whole response, with its `assets` list
unmodified. Do not hand-edit values into it; transcription is where errors enter.

**4. Write the directives.**

```bash
uv run finance prices <slug> <snapshot.json> --as-of <YYYY-MM-DD> --portfolio Investments
```

This writes `ledger/<slug>/prices.beancount` and prints what it could not price, with reasons.

**5. Verify.**

```bash
uv run finance check <slug>
uvx --quiet --from beanquery bean-query ledger/<slug>/main.beancount \
  "SELECT sum(cost(position)) AS cost, sum(value(position)) AS market
   WHERE account ~ 'Positions'"
```

`market` should now differ from `cost`. If they are identical, the include is missing — check
that `main.beancount` has `include "prices.beancount"`.

## How matching works, and when it gives up

There is no shared key: the ledger identifies an instrument by ISIN, Delta by ticker. The
quantity bridges them — when Delta tracks the same portfolio it reports the same holding, so an
exact quantity match is a strong signal. Where several assets share a quantity (plenty of
positions are 2 or 5 shares) the name breaks the tie, and anything still ambiguous is reported
unpriced rather than guessed at. A wrong price is worse than a missing one: it misstates net
worth without looking broken.

Unpriced holdings are listed in the file as comments, not dropped. Their cost basis still counts
toward net worth, so a silently missing quote would read as a portfolio that shrank.

Expect a tail of unmatched holdings that are delisted, acquired or matured — Delta carries them
with a null price. That is not a bug to chase.

**If a specific holding matters and did not match**, Delta's `search_assets` accepts an ISIN
directly and returns every listing of that instrument across exchanges. Pick the listing whose
quantity matches the ledger, and add it to the snapshot's `assets` list before re-running.

## Notes

- `prices.beancount` is generated but **not** by `finance build`; that command never touches it
  and will not refresh it. It is a separate file precisely so the two sources stay distinct.
- Prices are dated the day of the snapshot. Re-running on a later date appends a newer price
  rather than replacing history, which is what Beancount wants for a value-over-time chart.
- Only commodities currently held are priced. Fully disposed holdings need no quote.
