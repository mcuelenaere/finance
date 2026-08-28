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

## The second rule: nothing private leaves this repo

**The GitHub remote is public.** `data/`, `rules/` and `ledger/` are gitignored, but that only
covers whole files. Private data reaches tracked content through the gaps around them, and
each of these routes is easy to walk into:

- **Commit messages.** A message body is exactly as public as the code. Balances, clearing
  totals, account-tree names, transaction counts and date ranges all read naturally in a
  commit message, and all of them are private.
- **Docstrings and comments.** An amount or IBAN reached for as an illustrative example is
  usually a real one, copied from whatever happened to be on screen.
- **Test fixtures.** Same reflex — the real exports are the nearest source of realistic test
  data, and a fixture is forever.
- **`rules/example.yml`.** Tempting to seed from the real rules file, which brings real
  account numbers and the real data layout along with it.

So: **never write a real figure or identifier anywhere git tracks.** That covers account
numbers, balances, amounts, account-tree names, transaction counts, date ranges, merchant
names specific to the user, and file paths under `data/`.

When something concrete is genuinely needed:

- Fake IBANs in the `BE12 3456 7890 1234` / `BE12345678901234` style (`123-4567890-12` for
  ING-format numbers). Keep them obviously sequential so nobody mistakes them for real.
- Describe magnitudes qualitatively — "reduced the outstanding balance by two orders of
  magnitude", not the figures.
- Format examples can use `1.234,56`, which illustrates European notation without being
  anyone's transaction.

Before committing, check the staged content *and* the message you are about to use:

```bash
# account-number shapes in staged content (Belgian IBAN, and ING's 123-4567890-12 format)
git diff --cached | grep -nE '[A-Z]{2}[0-9]{2} ?[0-9]{4}|[0-9]{3}-[0-9]{7}-[0-9]{2}'

# any staged file that should never be tracked
git diff --cached --name-only | grep -E '^(data|ledger)/|^rules/' | grep -v '^rules/example.yml'
```

The second must print nothing. The first is a *review prompt*, not a pass/fail gate: it also
matches the deliberate fakes, so read what it prints and confirm each hit is one of those
rather than assuming an empty result means safe. (It avoids a negative lookahead on purpose —
BSD `grep` and `ugrep` both reject `(?!…)`, hence the second `grep -v`.)

Then read the message you are about to commit and confirm it carries no amounts, balances or
account names. No tooling catches that part, and it is where the leaks actually happened.

If something does get committed, note that **force-pushing does not remove it from GitHub** —
the orphaned commit stays reachable by SHA. Fixing it properly means making the repo private,
asking GitHub Support to garbage-collect, or recreating the repo. Say so rather than implying
a rewrite was sufficient.

## Commands

CI runs all three of these, so `ruff format --check` failing is a red build even when
`ruff check` passes — they are different tools and the linter does not imply the formatter.

```bash
uv run finance build <slug>     # regenerate ledger/<slug>/generated.beancount
uv run finance review <slug>    # unmatched transactions, biggest first — the work queue
uv run finance check <slug>     # bean-check + transfer clearing report
uv run pytest && uv run ruff check . && uv run ruff format --check .
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

Two other skills cover the agent-fetches-code-applies split: `refresh-prices` writes market
prices from a portfolio snapshot, and `import-delta-portfolio` writes a whole transaction
history for accounts whose own institution exports nothing. Both materialize text and commit
it; neither lets a build perform a lookup.

**Income rules keyed on a counterparty need `direction: in`.** Such a rule claims everything
that counterparty is involved in, both ways, so a payment *back* to an employer lands as
negative salary — which reads as a small pay cut rather than the unclassified payment it is.
`direction` is opt-in rather than the default because a refund legitimately reverses the
income or expense it came from, and most rules should keep claiming both ways. `finance check`
lists the rules currently matching against their own account's direction, so the ones worth
constraining do not have to be guessed at.

## Things that will bite you

**Money moving between your own accounts is the dangerous case.** Both banks export it, so
naively both legs land and the balance doubles — silently inflating net worth. Every such
account belongs in `own_accounts`, which routes both legs through `Assets:Clearing:Transfers`. That
account nets to zero once legs pair, so a persistent non-zero balance means a leg is missing or
double-counted. `finance check` reports it.

**`generated.beancount` is a build artifact.** Never edit it — the next build overwrites it.
Fix the rule instead. Hand-written entries (apartment, mortgage, opening balances) go in
`manual.beancount`, which the build never touches.

It is *not* committed, despite diffs being the natural way to review what a rule change did:
`ledger/` is gitignored because this remote is public and the file is the entire financial
history in plain text. Getting that review loop back means a separate private repo for the
ledger, not relaxing the ignore rules.

**There are two fallback accounts, split by direction.** Unclassified money out lands in
`Expenses:Unknown`, unclassified money in on `Income:Unknown`. A single bucket for both nets
the two against each other, so it reads as a fraction of the work actually left in it — and
money arriving disappears from the income statement as a negative expense, which is the one
place nobody thinks to look for income. Both are `own_accounts`-level config keys.

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
