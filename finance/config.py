"""Per-ledger configuration.

One YAML file per ledger, at ``rules/<slug>.yml``. It holds everything specific to that
ledger: which files to read, the user's own accounts, and the categorization rules. Ledgers are
addressed by an ASCII *slug* (``prive``). Keep data directory names ASCII too: macOS stores
filenames decomposed (NFD), so an accented path pasted in composed form silently matches
nothing.
"""

from __future__ import annotations

from glob import glob
from pathlib import Path
from typing import NamedTuple

import yaml

from finance.rules import (
    DEFAULT_FALLBACK,
    DEFAULT_INCOME_FALLBACK,
    Ruleset,
    parse_rule,
)
from finance.text import normalize_identifier

RULES_DIR = Path("rules")
LEDGER_DIR = Path("ledger")


class Source(NamedTuple):
    path: str
    format: str


class LedgerConfig(NamedTuple):
    slug: str
    ruleset: Ruleset
    sources: tuple[Source, ...]

    @property
    def output_dir(self) -> Path:
        return LEDGER_DIR / self.slug

    @property
    def generated_path(self) -> Path:
        return self.output_dir / "generated.beancount"

    @property
    def main_path(self) -> Path:
        return self.output_dir / "main.beancount"


def config_path(slug: str) -> Path:
    return RULES_DIR / f"{slug}.yml"


def load(slug: str, *, root: Path | None = None) -> LedgerConfig:
    root = root or Path.cwd()
    path = root / config_path(slug)
    if not path.exists():
        raise FileNotFoundError(
            f"no rules file for ledger {slug!r} at {path}; "
            f"copy rules/example.yml to {config_path(slug)} to start one"
        )

    config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    ruleset = Ruleset(
        rules=tuple(parse_rule(rule) for rule in (config.get("rules") or [])),
        own_accounts={
            normalize_identifier(str(identifier)): str(account)
            for identifier, account in (config.get("own_accounts") or {}).items()
        },
        fallback_account=str(config.get("fallback_account", DEFAULT_FALLBACK)),
        transfer_account=str(config.get("transfer_account", "Assets:Transfers")),
        income_fallback_account=str(config.get("income_fallback_account", DEFAULT_INCOME_FALLBACK)),
    )

    sources = []
    for entry in config.get("files") or []:
        pattern = str(entry["path"])
        matches = sorted(glob(str(root / pattern)))
        if not matches:
            raise FileNotFoundError(f"no files matched {pattern!r} for ledger {slug!r}")
        sources.extend(Source(path=match, format=str(entry["format"])) for match in matches)

    return LedgerConfig(slug=slug, ruleset=ruleset, sources=tuple(sources))
