"""Deciding what a message is, from lists of phrases anyone can read and translate.

Rules, not a model, on purpose: they are inspectable, translatable, and wrong in ways a
person can see. Each language is a JSON file in ``rules/`` — the phrases that mark a
rejection, an invitation, an assessment, an offer, an acknowledgement, and the shapes a
date takes in that language. Adding a language is adding a file, not writing code. The
file is named after the language's BCP 47 tag as Postulo writes one: ``pt``, or ``pt-BR``
should a variety need phrases of its own.

The kinds are tried in the order ``MOST_CONSEQUENTIAL_FIRST`` names, and for each kind in
turn *every* language is tried, so the first kind with a match wins whichever language
matched it: a message that says both "we would like to invite you" and "thank you for your
application" is an invitation, and a Portuguese rejection that mentions an earlier
"entrevista presencial" is a rejection, not the interview the Spanish rules would have read
it as had they been asked first (postulo-imap#18).

**What happens in a language with no file is worth saying plainly.** The files ship for
German, English, Spanish, French, Italian, Dutch, Polish and Portuguese. A message in any
other language matches nothing, is skipped, and no suggestion is filed — it is never
guessed at. That is the right failure and the wrong silence: somebody whose rejections are
in Swedish sees a plugin that appears to work and files nothing. So the set is named in
`tests/test_imap.py`, adding a language is adding a file and a line there, and the plugin's
catalogues being translated into a language says nothing about whether its mail can be read
(postulo-imap#1). A skipped message is still flagged or moved like every other one the pass
read.

Within one kind the files are tried in turn, so a pattern that also reads as another
language answers for that language too — the first file to match names the language, and
`de.json` is consulted before `en.json`. Each list therefore keeps to words that belong to
its own language: "Online-Test" rather than a bare "online test" that is equally English.
The text is brought to one spelling first (`normalising.normalise`): a line wrap, an
entity, a non-breaking space and a typographic apostrophe do not hide a phrase.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from django.utils.translation import gettext_lazy as _
from postulo.plugins.api import language_match

from .normalising import normalise

RULES_DIR = Path(__file__).parent / "rules"

#: What each kind means for the record: the timeline entry, and the status it suggests.
#: Both are Postulo's own vocabulary, so accepting a suggestion writes exactly what a
#: person typing it would have written.
OUTCOMES = {
    "acknowledged": ("email_received", "acknowledged"),
    "rejected": ("rejection", "rejected"),
    "interview": ("email_received", "interviewing"),
    "assessment": ("assessment", "assessment"),
    "offer": ("offer", "offer"),
}


#: The kinds in the order they are tried: what a message that says two things is read as.
#: The first kind with a match, in any language, decides.
MOST_CONSEQUENTIAL_FIRST = ("offer", "rejected", "interview", "assessment", "acknowledged")

#: How a kind is worded to the person, apart from the identifier the code uses. What a
#: suggestion stores is the translated words, and the identifier stays `Verdict.kind`.
KIND_LABELS = {
    "acknowledged": _("an acknowledgement"),
    "rejected": _("a rejection"),
    "interview": _("an invitation"),
    "assessment": _("an assessment"),
    "offer": _("an offer"),
}


@dataclass(frozen=True)
class Verdict:
    """What the rules made of a message."""

    kind: str
    language: str
    matched: str
    dates: tuple[str, ...] = ()

    @property
    def event_kind(self) -> str:
        return OUTCOMES[self.kind][0]

    @property
    def suggested_status(self) -> str:
        return OUTCOMES[self.kind][1]

    @property
    def label(self) -> str:
        """The kind in the active language, for a person to read."""
        return str(KIND_LABELS[self.kind])


@lru_cache(maxsize=1)
def _loaded() -> list[dict]:
    """Every rule file, compiled once."""
    files = []
    for path in sorted(RULES_DIR.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        data["compiled"] = [
            (
                entry["kind"],
                [re.compile(pattern, re.IGNORECASE) for pattern in entry["patterns"]],
            )
            for entry in data.get("kinds", [])
            if entry.get("kind") in OUTCOMES
        ]
        data["date_patterns"] = [
            re.compile(pattern, re.IGNORECASE) for pattern in data.get("dates", [])
        ]
        files.append(data)
    return files


def languages() -> list[tuple[str, str]]:
    """The rule lists that ship, as (code, name), for the connection form."""
    return [(data["language"], data["name"]) for data in _loaded()]


def rules_for(code: str) -> str:
    """The rule list that reads ``code``, as its file names it, or "" when none does.

    A rule file is named after a BCP 47 tag, and a code is compared the way tags are:
    without regard to case, and a variety with no list of its own is read by its
    language's. `pt-BR` and `PT` both find `pt`; compared by exact string, each found
    nothing and said nothing (postulo-imap#3). The comparison is Postulo's own.
    """
    return language_match(code, [data["language"] for data in _loaded()])


def _consulted(only: tuple[str, ...]) -> list[dict]:
    """The rule files to try: every one, or those that read the languages asked for."""
    if not only:
        return _loaded()
    wanted = {rules_for(code) for code in only}
    return [data for data in _loaded() if data["language"] in wanted]


def dates_in(text: str, language: str = "") -> tuple[str, ...]:
    """Dates the text seems to propose, as they were written.

    They are offered to the person as text rather than parsed into a moment: reading
    "Tuesday the 12th" wrongly and putting the wrong day in a diary is worse than showing
    what the message said and letting them decide.
    """
    found: list[str] = []
    for data in _consulted((language,) if language else ()):
        for pattern in data["date_patterns"]:
            for match in pattern.finditer(text):
                whole = " ".join(part for part in match.groups() if part).strip()
                if whole and whole not in found:
                    found.append(whole)
    return tuple(found[:6])


def classify(text: str, *, only: tuple[str, ...] = ()) -> Verdict | None:
    """What kind of message this is, or ``None`` when no rule recognises it.

    Kind by kind, most consequential first, and every language for each kind: see the
    module's docstring. ``only`` limits the languages consulted; empty means all of them,
    which is what a mailbox that receives mail in several languages needs. Each is a
    language code however it is spelt, matched to a rule list by `rules_for`. Nothing in the
    plugin passes it yet: the connection form has no language field.
    """
    text = normalise(text)
    lowered = text.lower()
    files = _consulted(only)
    for kind in MOST_CONSEQUENTIAL_FIRST:
        for data in files:
            for entry_kind, patterns in data["compiled"]:
                if entry_kind != kind:
                    continue
                for pattern in patterns:
                    match = pattern.search(lowered)
                    if match:
                        dates = dates_in(text, data["language"]) if kind == "interview" else ()
                        return Verdict(
                            kind=kind,
                            language=data["language"],
                            matched=match.group(0).strip(),
                            dates=dates,
                        )
    return None
