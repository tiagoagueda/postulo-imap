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

from postulo.applications.models import Application, Suggestion
from postulo.jobs.models import Contact

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


@dataclass(frozen=True)
class Match:
    application: Application
    how: str


def registrable(host: str) -> str:
    """The last two labels of a hostname: enough to tell one organisation from another."""
    host = (host or "").lower().strip().removeprefix("www.")
    parts = [part for part in host.split(".") if part]
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def domain_of(url: str) -> str:
    if not url:
        return ""
    if "//" not in url:
        url = f"//{url}"
    return registrable(urlsplit(url).hostname or "")


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
    if domain and domain not in PUBLIC_DOMAINS:
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
    needle = (needle or "").strip().lower()
    if len(needle) < 3:
        return False
    return re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", haystack) is not None
