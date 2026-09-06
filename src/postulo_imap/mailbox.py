"""Talking to the mailbox, and turning a message into the few fields this plugin uses.

IMAPClient does the protocol; the standard library's ``email`` does the parsing. What is
read is deliberately small: the headers that identify and thread a message, the date, the
sender, the subject, and the first plain-text part, shortened. The body is not stored
anywhere by default — only a short excerpt travels with a suggestion — because a job
tracker holding a copy of somebody's mailbox is a different and much larger promise.
"""

from __future__ import annotations

import datetime as dt
import email
import email.utils
import re
from dataclasses import dataclass, field
from email.header import decode_header, make_header
from email.message import Message

from imapclient import IMAPClient

#: What travels with a suggestion, at most. An advert-length excerpt is plenty to
#: recognise a message by; the mailbox keeps the rest.
EXCERPT_CHARS = 2000
#: How far back a first pass looks, in days, when nothing has been marked yet.
DEFAULT_SINCE_DAYS = 30
#: The IMAP keyword put on a message once it has been read.
KEYWORD = "$Postulo"


class MailboxError(Exception):
    """The mailbox said no, and the message is for the person."""


@dataclass
class Mail:
    """One message, as much of it as this plugin looks at."""

    uid: int
    message_id: str
    sender: str
    sender_name: str
    subject: str
    date: dt.datetime | None
    text: str
    references: list[str] = field(default_factory=list)

    @property
    def domain(self) -> str:
        _, _, domain = self.sender.partition("@")
        return domain.lower()

    @property
    def haystack(self) -> str:
        return f"{self.subject}\n{self.text}".lower()


def _decoded(value: str | None) -> str:
    """A header as text: RFC 2047 words decoded, whitespace tidied."""
    if not value:
        return ""
    try:
        return re.sub(r"\s+", " ", str(make_header(decode_header(value)))).strip()
    except Exception:
        return re.sub(r"\s+", " ", value).strip()


def plain_text(message: Message) -> str:
    """The first plain-text part, or the HTML with its tags removed, shortened."""
    text = ""
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_type() == "text/plain" and not part.get_filename():
                text = _payload(part)
                break
        if not text:
            for part in message.walk():
                if part.get_content_type() == "text/html" and not part.get_filename():
                    text = _strip_html(_payload(part))
                    break
    else:
        text = _payload(message)
        if message.get_content_type() == "text/html":
            text = _strip_html(text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:EXCERPT_CHARS]


def _payload(part: Message) -> str:
    try:
        raw = part.get_payload(decode=True)
    except Exception:
        return ""
    if raw is None:
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return raw.decode(charset, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def _strip_html(html: str) -> str:
    html = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    html = re.sub(r"(?i)<br\s*/?>|</p>", "\n", html)
    return re.sub(r"\s+\n", "\n", re.sub(r"<[^>]+>", " ", html))


def parse(uid: int, raw: bytes) -> Mail:
    message = email.message_from_bytes(raw)
    name, address = email.utils.parseaddr(message.get("From", ""))
    date = None
    if message.get("Date"):
        try:
            date = email.utils.parsedate_to_datetime(message["Date"])
        except (TypeError, ValueError):
            date = None
    if date is not None and date.tzinfo is None:
        date = date.replace(tzinfo=dt.UTC)
    references = re.findall(
        r"<[^>]+>", f"{message.get('In-Reply-To', '')} {message.get('References', '')}"
    )
    return Mail(
        uid=uid,
        message_id=_decoded(message.get("Message-ID")) or f"uid-{uid}",
        sender=address.lower(),
        sender_name=_decoded(name),
        subject=_decoded(message.get("Subject")),
        date=date,
        text=plain_text(message),
        references=[reference.strip() for reference in references],
    )


class Mailbox:
    """One account, one folder, opened for as long as a run takes."""

    def __init__(self, client: IMAPClient, folder: str) -> None:
        self.client = client
        self.folder = folder

    def select(self, *, readonly: bool = False) -> None:
        try:
            self.client.select_folder(self.folder, readonly=readonly)
        except Exception as error:
            raise MailboxError(
                f"the folder {self.folder!r} could not be opened: {error}"
            ) from error

    def unread_uids(
        self, *, since_days: int = DEFAULT_SINCE_DAYS, use_keyword: bool = True
    ) -> list[int]:
        """Messages worth looking at: not yet marked, and recent enough.

        With the keyword in use, "not yet marked" is exactly that. Without it — read-only
        mode, or a server that refuses keywords — every recent message is read again, and
        nothing is filed twice because a suggestion is keyed on the message's identifier.
        """
        since = (dt.datetime.now(dt.UTC) - dt.timedelta(days=since_days)).date()
        criteria: list = ["SINCE", since]
        if use_keyword:
            criteria = ["NOT", "KEYWORD", KEYWORD, *criteria]
        try:
            return sorted(self.client.search(criteria))
        except Exception as error:
            raise MailboxError(f"the mailbox refused the search: {error}") from error

    def fetch(self, uids: list[int]) -> list[Mail]:
        if not uids:
            return []
        try:
            answer = self.client.fetch(uids, ["BODY.PEEK[]"])
        except Exception as error:
            raise MailboxError(f"the mailbox refused to hand over messages: {error}") from error
        mails = []
        for uid, parts in answer.items():
            raw = parts.get(b"BODY[]") or parts.get(b"RFC822") or b""
            if raw:
                mails.append(parse(int(uid), raw))
        return sorted(mails, key=lambda mail: mail.uid)

    def mark(self, uids: list[int]) -> None:
        if uids:
            self.client.add_flags(uids, [KEYWORD])

    def move(self, uids: list[int], destination: str) -> None:
        if not uids:
            return
        if not self.client.folder_exists(destination):
            self.client.create_folder(destination)
        self.client.move(uids, destination)


def connect(config: dict) -> IMAPClient:
    """Open the account. The caller closes it."""
    host = str(config.get("host", "")).strip()
    if not host:
        raise MailboxError("no server to connect to")
    port = int(config.get("port") or 993)
    starttls = str(config.get("security")) == "starttls"
    try:
        client = IMAPClient(host, port=port, ssl=not starttls, timeout=30)
        if starttls:
            client.starttls()
        client.login(str(config.get("username", "")), str(config.get("password", "")))
    except Exception as error:
        raise MailboxError(f"{type(error).__name__}: {error}") from error
    return client
