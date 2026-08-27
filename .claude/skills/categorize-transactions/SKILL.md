---
name: categorize-transactions
description: Write Beancount categorization rules for unmatched bank transactions in this repo. Use when transactions are falling through to Expenses:Unknown, after importing a new export or adding a parser, or when the user asks to categorize, classify, or "sort out" transactions for a ledger.
---

# Categorizing transactions

Turn unmatched transactions into committed rules. You write rules; the code applies them.

**Never send transaction data to a model — including yourself in a later step.** You read an
aggregated report of payee strings, and you write rule text. Amounts, IBANs and per-transaction
detail stay on disk. The output of this skill is always committed text, so every decision is
frozen and reproducible.

## Loop

```bash
uv run finance review <slug> --limit 40
```

Output is unmatched payees ranked by absolute total, largest first — work top-down, since the
top entries move the numbers most.

For each payee, add a rule to `rules/<slug>.yml`:

```yaml
- field: payee          # payee | narration | meta.<key>
  regex: ^DELHAIZE      # or `value:` for a whole-field exact match
  account: Expenses:Food:Groceries
```

Then verify:

```bash
uv run finance build <slug> && uv run finance check <slug>
```

Repeat until `review` is short enough to ignore. Expect several passes.

## Classify in this order

Work through these before reaching for an expense account — the first two are correctness
issues, not preferences.

**1. Is it a transfer between the user's own accounts?** Watch for the user's own name as
payee, and for known brokers (DEGIRO, Trade Republic, BUX, Lynx, Revolut, Wirex). These do
*not* get a rule. Add the account to `own_accounts` instead:

```yaml
own_accounts:
  "BE12 3456 7890 1234": Assets:Investments:TradeRepublic:Cash
```

Both legs then route through `Assets:Transfers`. Getting this wrong double-counts the money and
inflates net worth — the worst failure mode here, because nothing looks broken.

**2. Is it an investment or a loan repayment rather than spending?** Money moved into a broker
or against a mortgage principal is not an expense:

- broker deposits → `Assets:Investments:*` (or `own_accounts`, per above)
- mortgage → split: principal to `Liabilities:Mortgage`, interest to `Expenses:Home:Interest`
  (Belgian statements say `WOONKREDIET`)

**3. Otherwise it is income or expense.** Match the existing account tree rather than inventing
parallel names; run `grep -o 'account: .*' rules/<slug>.yml | sort -u` first.

## Writing rules that hold up

- Prefer `regex` anchored at the start (`^DELHAIZE`) over `value`. Merchant strings carry
  branch names and city suffixes that vary per transaction.
- One rule can cover many payees: `^(PROXY )?DELHAIZE|^CARREFOUR|^BIO-PLANET`.
- Whitespace runs are collapsed before matching, so write `PROXY DELHAIZE`, never
  `PROXY      DELHAIZE`.
- First match wins, so put specific rules above general ones.
- `meta.counterparty_iban` is stabler than a payee name when the counterparty is a company that
  renames itself.
- Migrated `narration` rules that no longer match may need `meta.detail`: for ING, the old
  `description` field was `Detail van de omzet`, which is now `meta.detail`.

## Done when

- `finance review <slug>` shows only a long tail of small one-offs.
- `finance check <slug>` reports `bean-check ... is valid`.
- `Assets:Transfers` settles to zero, or every remaining leg is genuinely in flight (< 5 days).
- Two consecutive `finance build` runs produce a byte-identical file.

`rules/<slug>.yml` is gitignored, so this loop produces nothing to commit. See CLAUDE.md
before adding anything tracked.
