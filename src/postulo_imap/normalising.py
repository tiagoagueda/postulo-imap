"""The text the rules and the matching read: one spelling of what a person would call the same.

A rejection is the same rejection when the mail client wrapped it at the 72nd column, when the
recruiting system put a tag in the middle of it, when an accent arrived as an HTML entity, when
a space is a non-breaking one and when the apostrophe is the typographic one a word processor
types. The phrase lists are written once, with single spaces and plain apostrophes, so the
text is brought to that shape before they run (postulo-imap#17), and the excerpt that is
stored keeps its own line breaks.
"""

from __future__ import annotations

import html
import re
import unicodedata

#: Every apostrophe and prime a word processor or a keyboard layout may type, as ``'``.
_APOSTROPHES = str.maketrans(dict.fromkeys("’ʼ‘′´`", "'"))
_WHITESPACE = re.compile(r"\s+")


def normalise(text: str) -> str:
    """Entities decoded, compatibility forms folded, apostrophes plain, one space between words.

    Case is left alone: the rules lower-case what they search, and the dates an invitation
    offers are shown as they were written.
    """
    text = unicodedata.normalize("NFKC", html.unescape(text or ""))
    return _WHITESPACE.sub(" ", text.translate(_APOSTROPHES)).strip()
