---
name: import-delta-portfolio
description: Import a Delta portfolio's transactions over MCP into a JSON export the `delta` parser reads, for accounts whose own institution will not export anything. Use when a broker, fund or pension provider gives no usable export but the holdings were tracked in Delta, or when the user asks to import a Delta portfolio, add a portfolio Delta tracks, or refresh an existing Delta-sourced export.
---

# Importing a Delta portfolio

Some institutions will not export an account at all. Where the holdings were entered into Delta
by hand, that tracker becomes the only record — so this fetches it once, writes it to `data/`,
and a deterministic parser takes over from there.

**You fetch; the code decides.** The same split as `refresh-prices`, and for the same reason: a
build that performed a lookup would make last month's numbers change under the user. Your job
ends when the file is written and it reconciles.

**This is a transcription, not a statement.** That is the thing to keep in mind throughout. No
bank stands behind these numbers, so the export carries two independent views of the same facts
and the parser turns the disagreement into a build failure. Do not skip the verification step —
it is the only thing standing between a typo and a wrong ledger.

## Steps

**1. Find the portfolio.**

```
mcp__…__list_portfolios
```

Note the id. Several portfolios are excluded from the user's own totals (`isExcluded`) and are
old or scratch copies — read the names rather than picking by size.

**2. Fetch every transaction, following the pages.**

```
mcp__…__list_transactions  { portfolioId: <id>, batchSize: 50 }
```

Keep passing the returned `continuationToken` until it comes back `null`. Stopping early is
silent: you get a shorter history that still parses.

**3. Fetch the positions separately.**

```
mcp__…__get_portfolio  { portfolioId: <id> }
```

This is a *different endpoint* from step 2, which is what makes it worth having — the two agree
only if the transactions were copied correctly.

**4. Write `data/<slug>/delta/<portfolio-name>.json`:**

```json
{
  "_source": "Delta MCP, portfolio '<name>' (id <id>), fetched <YYYY-MM-DD>",
  "account": "<own_accounts key>",
  "as_of": "<YYYY-MM-DD>",
  "positions": [
    {"shortCode": "<ISIN or ticker>", "name": "<name>", "holdings": "<qty>",
     "price": {"value": "<price>", "currency": "EUR"}}
  ],
  "transactions": [
    {"id": <id>, "datetime": "<ISO>", "way": "BUY", "base": "<ISIN>", "quote": "EUR",
     "amount": <qty>, "price": {"value": <unit price>, "currency": "EUR"}, "fee": null}
  ]
}
```

`account` names the `own_accounts` key, because one tracker holds many portfolios and the parser
cannot tell them apart. `as_of` dates the balance assertions — without it none are emitted,
since dating them from the clock would make the build non-reproducible.

**DROP THE `worth` FIELD.** Delta reports it as the position's *current market value*, not the
transaction's: a purchase of 4.741 units at 174.08 comes back as 985.37, which is 4.741 times
*today's* price. Cost is `amount * price`. Keeping the field in a data file invites that mistake
and makes the file change with the market.

**5. Wire it up** — `own_accounts` gets the key from step 4 pointing at a `:Cash` leaf, and
`files` gets the path with `format: delta`. The parser derives a `:Positions` sibling.

**6. Verify. Do not skip this.**

```bash
uv run finance build <slug> && uv run finance check <slug>
```

`bean-check` compares the holdings you fetched in step 3 against the units summed from step 2.
A single mistyped digit fails the build — which is the whole point of fetching both.

Then look for an independent cross-check outside the export. There usually is one: the bank
statements show money going *into* the account, and the cost of the units should land close to
it. The first use of this found 5.918,72 of units against 5.960,00 of standing orders — the
difference being entry fees, two sources sharing no data agreeing to a fraction of a percent.
If they disagree by more than fees would explain, something is wrong; say so rather than
shipping it.

**7. Prices, if the holdings are still open.**

The export's `positions` block has the same shape `finance prices` wants, so pass it straight
in — *alongside* the other snapshots, never alone:

```bash
uv run finance prices <slug> <other-snapshot.json> data/<slug>/delta/<name>.json --as-of <date>
```

That command rewrites the whole price file. Passing one portfolio drops every other
portfolio's prices, which is easy to do and looks like it worked.

## Notes

- **Cash flows are usually absent.** A tracker records what was bought, not the deposits that
  paid for it. So the cash leaf ends up holding the difference between money in (from the bank
  side, claimed by a rule) and units bought — which is the entry fee, and a useful figure. A
  *large* balance there means a payment bought nothing and wants investigating.
- **Only purchases are booked as lots.** A tracker's disposals have not been seen yet, so their
  shape cannot be checked against anything; they fall through to review instead of being guessed
  at. If one appears, model it like the DEGIRO or BUX disposals and give it a test.
- **Sorting is the parser's job**, not yours. The API answers newest-first and paginated; the
  parser sorts on the way in so the build cannot depend on what order it replied.
- Delta's `search_assets` takes an ISIN and returns every listing, if a holding needs
  identifying.
