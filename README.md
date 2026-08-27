# finance

Parses exports from Belgian banks, brokers and crypto platforms into
[Beancount](https://beancount.github.io/) ledgers.

This repo is **only an ingest layer**. It does no analysis: reporting, net worth and queries
come from Beancount-side tooling (`bean-check`, Fava, optionally `rledger`) reading the
generated ledger.

## Design

```
data/<ledger>/<institution>/*      raw exports (gitignored)
        │  parsers — pure, deterministic, no AI
        ▼
   Beancount directives, counter-postings unresolved
        │  rules/<ledger>.yml — AI-authored, committed
        ▼
ledger/<ledger>/generated.beancount   build artifact — never hand-edited
        +  accounts.beancount / manual.beancount / prices.beancount (hand-written)
        ▼
ledger/<ledger>/main.beancount  →  bean-check / Fava
```

`finance build` regenerates `generated.beancount` from scratch every run, so the ledger is
reproducible from raw data plus rules. Nothing is appended and there is no import state.

**No LLM ever sees a transaction.** Models write the *rules*; the code applies them
deterministically. See [CLAUDE.md](CLAUDE.md) for that workflow.

## Privacy

**The GitHub remote for this repo is public.** Three directories are therefore gitignored and
must stay that way:

| path | why |
| --- | --- |
| `data/` | raw exports — account numbers, balances, every merchant |
| `rules/` | rules are keyed on IBANs and merchant names (`rules/example.yml` is the exception) |
| `ledger/` | the generated ledger is the *whole* financial history in plain text |

That costs one thing worth knowing about: `generated.beancount` is a build artifact whose diffs
are the natural way to review what a rule change did, and an ignored file has no diffs. If you
want that, keep the ledger in a separate private repo (or make this one private) rather than
relaxing the ignore rules here.

## Usage

```bash
uv sync
uv run finance build prive     # regenerate the ledger
uv run finance review prive    # unmatched transactions, biggest first — the rule-writing queue
uv run finance check prive     # bean-check + transfer clearing report
```

Then browse it:

```bash
uvx fava ledger/prive/main.beancount
```
