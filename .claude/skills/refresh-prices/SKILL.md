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
uv run finance prices <slug> <snapshot.json> --as-of <YYYY-MM-DD> --portfolio Investments \
  --fx EUR:<rate>:USD
```

This writes `ledger/<slug>/prices.beancount` and prints what it could not price, with reasons.

**`--fx` is not optional when the ledger holds anything bought in another currency**, which
this one does — US stocks at DEGIRO, the ETFs at Lynx, the eToro award. The reason is worth
understanding, because the failure is silent:

**Beancount values a position in the currency it was bought in, and never composes two
conversions.** A dollar-bought holding quoted only in euro is reported as *bare units* — not as
an error and not at cost, but as nothing at all, which on a balance sheet reads as a holding
that is worthless rather than one that is unpriced. A euro quote plus a EUR/USD rate does not
help either; the two are never chained.

So the rate is used twice over: `finance prices` re-expresses each quote in the currency that
holding's own lots are held in (the tracker reports everything in euro, so converting back
reintroduces exactly the rate it used to get there), and the rate itself is written out so a
euro total still works. A commodity bought in two currencies gets a quote in each.

Get the rate from the tracker rather than anywhere else, so it matches the prices:

```
mcp__…__search_assets  { query: "EURUSD", types: ["FOREX"] }
mcp__…__get_asset_details  { assetId: <id> }   ->  marketData.close
```

Expect a rounding difference of a few euro across the whole portfolio: the quotes are kept to
four decimals, so converting them back is not exactly lossless. Measured once at about
0,0015% — visible if you compare totals across a refresh, and not worth chasing.

**5. Verify.**

```bash
uv run finance check <slug>
uvx --quiet --from beanquery bean-query ledger/<slug>/main.beancount \
  "SELECT sum(cost(position)) AS cost, sum(value(position)) AS market
   WHERE account ~ 'Positions'"
```

`market` should now differ from `cost`. If they are identical, the include is missing — check
that `main.beancount` has `include "prices.beancount"`.

Then check that **nothing comes back as bare units**, which is the `--fx` failure above:

```bash
uvx --quiet --from beanquery bean-query ledger/<slug>/main.beancount \
  "SELECT sum(value(position)) WHERE account ~ '^Assets:Investments'"
```

Currency totals are the healthy answer. A list of quantities and tickers means those holdings
are unpriced in every "at market value" view, and the balance sheet is understated by whatever
they are worth.

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
