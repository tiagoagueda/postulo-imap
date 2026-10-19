"""Which application a message is about, and how sure that is.

Four signals, tried in order of how much they prove:

1. **The thread.** A message answering one already matched belongs to the same
   application. Nothing beats that, and it is free: the identifiers are in the headers.
2. **A contact's address.** The person recorded who they are talking to; mail from that
   address is about the application that contact belongs to.
3. **The sender's domain against the company's website.** ``careers@blackmesa.test`` and
   ``https://blackmesa.test`` are the same organisation often enough to be worth using,
   and are checked against the person's own companies only.
4. **The company name or the role title in the subject or body.** Weakest, and used last.

When two applications answer equally well, none is chosen: an unmatched suggestion a
person attaches by hand is better than a confident wrong one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from django.utils.translation import gettext_lazy as _
from postulo.plugins.api import Application, Contact, Suggestion

from .normalising import normalise

#: Domains that host mailboxes for everyone, so a match on them means nothing.
PUBLIC_DOMAINS = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "outlook.com",
        "hotmail.com",
        "live.com",
        "yahoo.com",
        "yahoo.co.uk",
        "hotmail.co.uk",
        "live.co.uk",
        "yahoo.com.br",
        "hotmail.com.br",
        "outlook.com.br",
        "icloud.com",
        "me.com",
        "proton.me",
        "protonmail.com",
        "gmx.com",
        "gmx.net",
        "web.de",
        "orange.fr",
        "free.fr",
        "sapo.pt",
    }
)


#: How a match is worded to the person, apart from the identifier the code and the tests
#: use (`Match.how`). What a suggestion stores is the translated words.
HOW_LABELS = {
    "thread": _("a reply to a message already matched"),
    "contact": _("the address of a contact"),
    "domain": _("the sender’s web domain"),
    "company name": _("the company’s name in the message"),
    "role": _("the role’s title in the message"),
}


@dataclass(frozen=True)
class Match:
    application: Application
    how: str

    @property
    def label(self) -> str:
        """How it was matched, in the active language, for a person to read."""
        return str(HOW_LABELS[self.how])


#: Under these a country registers names one level down (`acme.co.uk`, `acme.com.br`), so
#: the organisation is three labels and not two. They are only read under a two-letter
#: country code, which is where the convention lives.
_SECOND_LEVELS = frozenset(
    {
        "ac", "co", "com", "edu", "go", "gob", "gouv", "gov", "gv", "ltd", "me", "mil",
        "ne", "net", "nom", "or", "org", "plc", "sch", "asso",
    }
)  # fmt: skip

#: Hosts that give their names away to anybody: everything under one of these is a
#: different organisation, so the suffix is the whole of it.
_SHARED_SUFFIXES = frozenset(
    {
        "github.io", "gitlab.io", "herokuapp.com", "appspot.com", "blogspot.com",
        "wordpress.com", "netlify.app", "vercel.app", "pages.dev", "workers.dev", "web.app",
        "firebaseapp.com", "azurewebsites.net", "cloudfront.net", "amazonaws.com",
        "myshopify.com", "wixsite.com", "weebly.com", "webflow.io", "notion.site", "carrd.co",
    }
)  # fmt: skip


def _ascii_host(host: str) -> str:
    """A hostname in its ASCII form, which is how a sender's domain arrives.

    A company website typed with its own letters (`münchen.example`) and a sender's
    `xn--mnchen-3ya.example` are the same host; compared in one form they are seen to be.
    """
    host = (host or "").lower().strip().rstrip(".")
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        return host


def registrable(host: str) -> str:
    """The organisation behind a hostname: one label more than its public suffix.

    `careers.acme.co.uk` is `acme.co.uk`, not `co.uk`, which every organisation under that
    suffix shares (postulo-imap#19). The suffix is told from a short table of the hosts that
    give names away and the second-level labels country codes register under; a hostname
    that is nothing but a suffix belongs to nobody and reduces to "". The Public Suffix
    List itself is not used because reading it means either a dependency or a download, and
    a plugin makes no request of its own.
    """
    host = _ascii_host(host).removeprefix("www.")
    parts = [part for part in host.split(".") if part]
    if len(parts) < 2:
        return host
    suffix = 1
    if ".".join(parts[-2:]) in _SHARED_SUFFIXES or (
        len(parts[-1]) == 2 and parts[-2] in _SECOND_LEVELS
    ):
        suffix = 2
    if len(parts) <= suffix:
        return ""
    return ".".join(parts[-(suffix + 1) :])


def domain_of(url: str) -> str:
    if not url:
        return ""
    if "//" not in url:
        url = f"//{url}"
    try:
        return registrable(urlsplit(url).hostname or "")
    except ValueError:
        return ""


def _only(candidates: list[Application], how: str) -> Match | None:
    """One candidate is an answer; several are not."""
    unique = {application.pk: application for application in candidates}
    if len(unique) == 1:
        return Match(next(iter(unique.values())), how)
    return None


def match(owner, mail, *, applications=None) -> Match | None:
    """The application this message is about, or ``None``."""
    applications = list(
        applications
        if applications is not None
        else Application.objects.for_user(owner).open().select_related("posting__company")
    )
    if not applications:
        return None

    # 1. The thread.
    if mail.references:
        threaded = (
            Suggestion.objects.for_user(owner)
            .filter(external_id__in=mail.references, application__isnull=False)
            .select_related("application__posting__company")
            .first()
        )
        if threaded is not None:
            return Match(threaded.application, "thread")

    by_company: dict[int, list[Application]] = {}
    for application in applications:
        by_company.setdefault(application.posting.company_id, []).append(application)

    # 2. A contact's address.
    if mail.sender:
        contacts = Contact.objects.for_user(owner).filter(email__iexact=mail.sender)
        candidates = [
            application
            for contact in contacts
            for application in by_company.get(contact.company_id, [])
        ]
        found = _only(candidates, "contact")
        if found is not None:
            return found

    # 3. The sender's domain against the company's website.
    domain = registrable(mail.domain)
    if domain and domain not in PUBLIC_DOMAINS and _ascii_host(mail.domain) not in PUBLIC_DOMAINS:
        candidates = [
            application
            for application in applications
            if domain_of(application.posting.company.website) == domain
        ]
        found = _only(candidates, "domain")
        if found is not None:
            return found

    # 4. The company name, then the role, in what the message says.
    haystack = mail.haystack
    candidates = [
        application
        for application in applications
        if _mentions(haystack, application.posting.company.name)
    ]
    found = _only(candidates, "company name")
    if found is not None:
        return found
    candidates = [
        application
        for application in applications
        if _mentions(haystack, application.posting.title)
    ]
    return _only(candidates, "role")


def _mentions(haystack: str, needle: str) -> bool:
    """Whether the words are there, however the message wrapped, spaced or spelt them."""
    needle = normalise(needle).lower()
    if len(needle) < 3:
        return False
    return re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", normalise(haystack).lower()) is not None
