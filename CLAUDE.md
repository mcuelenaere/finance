# Working in this repo

This repo is an **ingest layer only**: bank/broker exports in, Beancount out. It does no
analysis. Reporting and net worth come from Fava and `bean-check` reading the generated ledger.

## The one rule that shapes everything

**No transaction is ever sent to a model.** Privacy and determinism both depend on it.

An agent's job is to *write the rules*; the code's job is to *apply them*. Rule text is
committed, so a classification decided once is frozen forever and last month's numbers never
change under you. Anything an agent produces — a rule, an extracted CSV, a price directive —
gets materialized as text and committed. Nothing is recomputed by a model at build time.

Concretely: do not add an LLM call to any module under `finance/`. If a task seems to need
one, it belongs in a skill that writes text instead.

## Commands

```bash
uv run finance build <slug>     # regenerate ledger/<slug>/generated.beancount
uv run finance review <slug>    # unmatched transactions, biggest first — the work queue
uv run finance check <slug>     # bean-check + transfer clearing report
uv run pytest && uv run ruff check .
```

Ledgers are addressed by ASCII slug (`prive`). Keep data directory names ASCII too: macOS
stores filenames decomposed (NFD), so an accented path pasted in composed form matches nothing
and fails silently.

## The categorization loop

1. `uv run finance review prive` — unmatched payees, ranked by absolute total, so the largest
   corrections come first.
2. Edit `rules/prive.yml`. Match on `payee`, `narration`, or `meta.<key>`; `value` matches the
   whole field, `regex` is a search. Whitespace runs are collapsed before matching, so write
   rules against the readable form.
3. `uv run finance build prive && uv run finance check prive`.

Repeat. `.claude/skills/categorize-transactions/` has this as an invokable skill.

## Things that will bite you

**Money moving between your own accounts is the dangerous case.** Both banks export it, so
naively both legs land and the balance doubles — silently inflating net worth. Every such
account belongs in `own_accounts`, which routes both legs through `Assets:Transfers`. That
account nets to zero once legs pair, so a persistent non-zero balance means a leg is missing or
double-counted. `finance check` reports it.

**`generated.beancount` is a build artifact.** It is committed so diffs are reviewable, but
never edit it — the next build overwrites it. Fix the rule instead. Hand-written entries
(apartment, mortgage, opening balances) go in `manual.beancount`.

**Investment transfers are not expenses.** A rule sending a broker transfer to `Expenses:*`
makes net worth permanently wrong. Those go to `Assets:*`.

**Amounts are European-formatted.** `1.234,56` means 1234.56. Always use
`finance.parsers.base.parse_decimal`; `text.replace(",", ".")` breaks on any amount over a
thousand, which is how the original parsers were broken.

**The build must be idempotent.** Two runs produce a byte-identical file. Anything that
introduces ordering nondeterminism (dict iteration, unsorted globs, timestamps in output) is a
bug. There is a golden-file test guarding renderer output.

## Adding a parser

Parsers live in `finance/parsers/`, expose
`parse_file(path, accounts) -> Iterator[Directive]`, and are registered in `FORMATS`. They are
pure: file in, directives out, no model calls.

Fill in the postings you can know (the account the statement belongs to) and leave the
counter-posting as `Posting(account=None, units=None)`. The rules engine fills the account, and
Beancount balances the elided amount. Leave at most one posting unresolved.

Put anything a rule might match on into `meta` — `counterparty_iban`, `mcc`, `merchant_id`,
`transaction_id`. Metadata is queryable in BQL and visible in Fava, and a stable
`transaction_id` makes deduplication exact instead of heuristic.

Resolve accounts with `resolve_account`, which raises rather than guessing. That error message
is the main way `own_accounts` gets populated, so keep it paste-ready.
