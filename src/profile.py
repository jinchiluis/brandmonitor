"""Client profiles: what a customer wants found, loaded from clients/<slug>/.

Collection is source-specific; analysis is client-specific. This module is the
analysis side of that rule — it holds no brands of its own, so adding a customer
is a new directory rather than a code change.

A profile is deliberately **self-contained**. Composing one from shared topic
files would let an edit to the shared file change a client's assessments without
changing their ``profile_version``, which is exactly what the traceability rule
in CLAUDE.md forbids. Duplicating a few dozen lines per client is the cheaper
side of that trade until composition is worth hashing a resolved profile for.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from src.config import ROOT

CLIENTS_DIR = ROOT / "clients"
GROUPS = ("brands", "topics", "keywords")


@dataclass(frozen=True)
class Rule:
    """One named matcher. ``group`` prefixes the reason: brand:, topic:, keyword:."""
    group: str
    name: str
    pattern: re.Pattern[str]

    @property
    def reason(self) -> str:
        return f"{self.group}:{self.name}"


@dataclass(frozen=True)
class ClientProfile:
    slug: str
    name: str
    profile_version: str
    rules: tuple[Rule, ...]
    # How many broad keywords must co-occur in a body before it counts on its
    # own. A title is short and curated, so one keyword there means something; a
    # body is long and diffuse, so one keyword there is usually an accident.
    # Measured on 342 bodies: requiring two halves the selections (156 -> 77)
    # while keeping every brand and topic find.
    body_min_keywords: int = 2

    def reasons_for(self, text: str) -> tuple[str, ...]:
        """Every rule that matches. Plain OR - no rule needs a second term."""
        if not text:
            return ()
        return tuple(dict.fromkeys(
            rule.reason for rule in self.rules if rule.pattern.search(text)))

    def body_reasons(self, text: str) -> tuple[str, ...]:
        """Matches in a body, after the co-occurrence rule.

        A brand or a narrow topic stands alone wherever it appears - finding
        "Temu" in paragraph twelve is the entire point of reading bodies. Broad
        keywords do not: on their own they select supply-chain thinkpieces,
        telecoms rulings and, thanks to German homonyms, iPad reviews ("13-Zoll-
        Display" is inches, not customs) and telecom bundles ("im Paket").
        """
        found = self.reasons_for(text)
        if not found:
            return ()
        strong = [r for r in found if not r.startswith("keyword:")]
        keywords = [r for r in found if r.startswith("keyword:")]
        if strong or len(keywords) >= self.body_min_keywords:
            return found
        return ()


def compile_term(term: str, *, boundaries: bool = True) -> str:
    """Turn one plain term into a regex fragment.

    Two conventions, both there because German and slug text need them:

    * a trailing ``*`` allows a word suffix, so ``zollfreigrenz*`` matches
      Zollfreigrenze and Zollfreigrenzen;
    * spaces and hyphens inside a term are interchangeable and optional, because
      ``normalized()`` turns slug hyphens into spaces while prose may hyphenate.
      So ``de minimis`` matches "de-minimis", "de minimis" and "deminimis".
    """
    term = term.strip()
    if not term:
        raise ValueError("empty term")
    open_suffix = term.endswith("*")
    if open_suffix:
        term = term[:-1]
    tokens = [re.escape(token) for token in re.split(r"[\s\-]+", term) if token]
    if not tokens:
        raise ValueError(f"term has no usable characters: {term!r}")
    body = r"[\s\-]*".join(tokens)
    if not boundaries:
        return body
    tail = r"\w*" if open_suffix else r"(?!\w)"
    return rf"(?<!\w){body}{tail}"


def _compile_entry(group: str, entry: dict[str, Any]) -> Rule:
    name = entry.get("name")
    if not name:
        raise ValueError(f"{group} entry needs a name: {entry}")
    boundaries = entry.get("boundaries", True)
    fragments: list[str] = []
    if entry.get("pattern"):
        fragments.append(entry["pattern"])
    for term in entry.get("terms", ()):
        fragments.append(compile_term(term, boundaries=boundaries))
    if not fragments:
        raise ValueError(f"{group}:{name} has neither terms nor pattern")
    try:
        pattern = re.compile("|".join(fragments), re.IGNORECASE)
    except re.error as exc:
        raise ValueError(f"{group}:{name} does not compile: {exc}") from exc
    return Rule(group=group.rstrip("s"), name=name, pattern=pattern)


def load_profile(source: str | Path) -> ClientProfile:
    """Load a client profile by slug (``jt-express``) or explicit path."""
    path = Path(source)
    if not path.suffix:
        path = CLIENTS_DIR / str(source) / "profile.json"
    if not path.exists():
        raise FileNotFoundError(
            f"no client profile at {path}. Profiles live in clients/<slug>/profile.json")
    data = json.loads(path.read_text(encoding="utf-8"))

    for field in ("slug", "profile_version"):
        if not data.get(field):
            raise ValueError(f"{path} is missing {field}")

    rules: list[Rule] = []
    for group in GROUPS:
        for entry in data.get(group, ()):
            rules.append(_compile_entry(group, entry))
    if not rules:
        raise ValueError(f"{path} defines no brands, topics or keywords")

    threshold = data.get("body_min_keywords", 2)
    if not isinstance(threshold, int) or threshold < 1:
        raise ValueError(f"{path}: body_min_keywords must be a positive integer")

    return ClientProfile(
        slug=data["slug"],
        name=data.get("name", data["slug"]),
        profile_version=data["profile_version"],
        rules=tuple(rules),
        body_min_keywords=threshold,
    )


def available_profiles() -> list[str]:
    if not CLIENTS_DIR.exists():
        return []
    return sorted(p.parent.name for p in CLIENTS_DIR.glob("*/profile.json"))
