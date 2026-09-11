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


SCOPES = ("any", "body")


@dataclass(frozen=True)
class Rule:
    """One named matcher. ``group`` prefixes the reason: brand:, topic:, keyword:.

    ``scope`` is ``any`` (title, slug and body) or ``body``. A body-only rule
    never selects on its own, nor together with other body-only rules: it
    exists to be the second keyword that ``body_min_keywords`` asks for, next
    to a regular one. Policy vocabulary is the case - measured over 12,449
    mainstream titles, ten policy words hit 58 headlines and not one of them
    also named the sector, so on a title they buy nothing; and in bodies two
    of them together select law-firm pages, not market news.
    """
    group: str
    name: str
    pattern: re.Pattern[str]
    scope: str = "any"

    @property
    def reason(self) -> str:
        return f"{self.group}:{self.name}"


@dataclass(frozen=True)
class PromptInputs:
    """What the LLM stages are told about a client, in the client's own profile.

    Brand names are not repeated here: prompts take them from the brand rules by
    role, so a competitor added for matching reaches the prompt too.

    ``false_matches`` were measured on titles and serve both gates;
    ``body_false_matches`` adds the noise classes only a body shows. The two
    ``regulatory_`` lists are what the body gate's regulatory prompt is told:
    legal areas rather than business topics, because regulatory relevance is
    topical. A profile without them cannot gate regulatory bodies.
    """
    about: str
    relevant: tuple[str, ...]
    false_matches: tuple[str, ...] = ()
    body_false_matches: tuple[str, ...] = ()
    regulatory_relevant: tuple[str, ...] = ()
    regulatory_false_matches: tuple[str, ...] = ()


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
    # (name, role) for every brand; role is "" where the profile gives none.
    brands: tuple[tuple[str, str], ...] = ()
    prompt: PromptInputs | None = None

    def brands_with_role(self, role: str) -> tuple[str, ...]:
        return tuple(name for name, brand_role in self.brands if brand_role == role)

    def reasons_for(self, text: str, *, body: bool = False) -> tuple[str, ...]:
        """Every rule that matches. Plain OR - no rule needs a second term.

        Rules scoped to the body are skipped unless ``body`` is set.
        """
        if not text:
            return ()
        return tuple(dict.fromkeys(
            rule.reason for rule in self.rules
            if (body or rule.scope == "any") and rule.pattern.search(text)))

    def body_reasons(self, text: str) -> tuple[str, ...]:
        """Matches in a body, after the co-occurrence rule.

        A brand or a narrow topic stands alone wherever it appears - finding
        "Temu" in paragraph twelve is the entire point of reading bodies. Broad
        keywords do not: on their own they select supply-chain thinkpieces,
        telecoms rulings and, thanks to German homonyms, iPad reviews ("13-Zoll-
        Display" is inches, not customs) and telecom bundles ("im Paket").
        """
        found = self.reasons_for(text, body=True)
        if not found:
            return ()
        strong = [r for r in found if not r.startswith("keyword:")]
        keywords = [r for r in found if r.startswith("keyword:")]
        anchors = [r for r in keywords if r not in self._body_only]
        # A body-only keyword completes a pair; it never forms one. Two policy
        # words with no sector word between them describe a law, not the
        # client's market - measured, that pattern selected Wettbewerbszentrale
        # category pages and little else.
        if strong or (anchors and len(keywords) >= self.body_min_keywords):
            return found
        return ()

    @property
    def _body_only(self) -> frozenset[str]:
        return frozenset(rule.reason for rule in self.rules if rule.scope == "body")


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
    scope = entry.get("scope", "any")
    if scope not in SCOPES:
        raise ValueError(f"{group}:{name} has scope {scope!r}; expected one of {SCOPES}")
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
    return Rule(group=group.rstrip("s"), name=name, pattern=pattern, scope=scope)


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
        brands=tuple((entry["name"], entry.get("role", ""))
                     for entry in data.get("brands", ())),
        prompt=_prompt_inputs(path, data.get("prompt")),
    )


def _prompt_inputs(path: Path, section: Any) -> PromptInputs | None:
    if section is None:
        return None

    def lines(key: str, required: bool) -> tuple[str, ...]:
        value = section.get(key, [] if not required else None)
        if (not isinstance(value, list) or (required and not value)
                or not all(isinstance(line, str) and line.strip() for line in value)):
            raise ValueError(f"{path}: prompt.{key} must be a "
                             f"{'non-empty ' if required else ''}list of strings")
        return tuple(line.strip() for line in value)

    about = section.get("about")
    if not isinstance(about, str) or not about.strip():
        raise ValueError(f"{path}: prompt.about must be a non-empty string")
    return PromptInputs(about=about.strip(), relevant=lines("relevant", True),
                        false_matches=lines("false_matches", False),
                        body_false_matches=lines("body_false_matches", False),
                        regulatory_relevant=lines("regulatory_relevant", False),
                        regulatory_false_matches=lines("regulatory_false_matches", False))


def available_profiles() -> list[str]:
    if not CLIENTS_DIR.exists():
        return []
    return sorted(p.parent.name for p in CLIENTS_DIR.glob("*/profile.json"))
