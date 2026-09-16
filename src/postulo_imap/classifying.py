"""Deciding what a message is, from lists of phrases anyone can read and translate.

Rules, not a model, on purpose: they are inspectable, translatable, and wrong in ways a
person can see. Each language is a JSON file in ``rules/`` — the phrases that mark a
rejection, an invitation, an assessment, an offer, an acknowledgement, and the shapes a
date takes in that language. Adding a language is adding a file, not writing code.

The kinds are tried in the order the file lists them, most consequential first, and the
first that matches wins: a message that says both "we would like to invite you" and
"thank you for your application" is an invitation.

**What happens in a language with no file is worth saying plainly.** The files ship for
German, English, Spanish, French, Italian, Dutch, Polish and Portuguese. A message in any
other language matches nothing, is skipped, and no suggestion is filed — it is never
guessed at, and the mailbox is left exactly as it was. That is the right failure and the
wrong silence: somebody whose rejections are in Swedish sees a plugin that appears to work
and files nothing. So the set is named in `tests/test_imap.py`, adding a language is adding
a file and a line there, and the plugin's catalogues being translated into a language says
nothing about whether its mail can be read (postulo-imap#1).

The files are tried in turn, so a pattern that also reads as another language answers for
that language too — the first file to match decides, and `de.json` is consulted before
`en.json`. Each list therefore keeps to words that belong to its own language: "Online-Test"
rather than a bare "online test" that is equally English.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

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


def dates_in(text: str, language: str = "") -> tuple[str, ...]:
    """Dates the text seems to propose, as they were written.

    They are offered to the person as text rather than parsed into a moment: reading
    "Tuesday the 12th" wrongly and putting the wrong day in a diary is worse than showing
    what the message said and letting them decide.
    """
    found: list[str] = []
    for data in _loaded():
        if language and data["language"] != language:
            continue
        for pattern in data["date_patterns"]:
            for match in pattern.finditer(text):
                whole = " ".join(part for part in match.groups() if part).strip()
                if whole and whole not in found:
                    found.append(whole)
    return tuple(found[:6])


def classify(text: str, *, only: tuple[str, ...] = ()) -> Verdict | None:
    """What kind of message this is, or ``None`` when no rule recognises it.

    ``only`` limits the languages consulted; empty means all of them, which is what a
    mailbox that receives mail in several languages needs.
    """
    lowered = text.lower()
    for data in _loaded():
        if only and data["language"] not in only:
            continue
        for kind, patterns in data["compiled"]:
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
