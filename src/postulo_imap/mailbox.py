"""Talking to the mailbox, and turning a message into the few fields this plugin uses.

IMAPClient does the protocol; the standard library's ``email`` does the parsing. What is
read is deliberately small: the headers that identify and thread a message, the date, the
sender, the subject, and the first plain-text part, shortened. The body is not stored
anywhere by default — only a short excerpt travels with a suggestion — because a job
tracker holding a copy of somebody's mailbox is a different and much larger promise.

**Where this is allowed to dial is not the plugin's decision.** A server and a port are
typed into a form by whoever owns the connection, and a form that opens a socket wherever
it is pointed is a port scanner and a way onto the loopback interface of the machine
Postulo runs on. So the host goes through ``approve_host`` first: it resolves the name,
holds every address it answers with to the instance's policy
(``POSTULO_CONNECTIONS_ALLOW_PRIVATE``, which is how somebody says their mail server is on
their own network), and hands back the address to connect to. Connecting to *that* address
rather than to the name again is the point — a name is free to answer publicly for a check
and with ``127.0.0.1`` a moment later — and the name is kept for TLS, because a
certificate proved against a number never matches.

**What goes wrong is said plainly and no more than that.** Whatever the far side answered
— a refused banner, a protocol error, the exact reason a sign-in failed — goes to the log,
where the operator can read it, and never into the message the person sees: *Test* is
reachable by anyone who can add a connection, and a verbatim error is how a form becomes a
way to read what is listening on a network it should not be able to see at all.
"""

from __future__ import annotations

import datetime as dt
import email
import email.utils
import logging
import re
import ssl
from dataclasses import dataclass, field
from email.header import decode_header, make_header
from email.message import Message

from django.utils.translation import gettext as _
from imapclient import IMAPClient
from postulo.plugins.api import DestinationRefused, approve_host

#: What travels with a suggestion, at most. An advert-length excerpt is plenty to
#: recognise a message by; the mailbox keeps the rest.
EXCERPT_CHARS = 2000
#: How far back a first pass looks, in days, when nothing has been marked yet.
DEFAULT_SINCE_DAYS = 30
#: The IMAP keyword put on a message once it has been read.
KEYWORD = "$Postulo"
#: Long enough for a slow server, short enough that a scheduled pass cannot hang on one.
TIMEOUT = 30

logger = logging.getLogger(__name__)


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
            logger.info("The folder %r could not be opened: %s", self.folder, error)
            raise MailboxError(
                str(_("The folder “%(folder)s” could not be opened.")) % {"folder": self.folder}
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
            logger.info("The mailbox refused the search: %s", error)
            raise MailboxError(str(_("The mailbox refused the search."))) from error

    def fetch(self, uids: list[int]) -> list[Mail]:
        if not uids:
            return []
        try:
            answer = self.client.fetch(uids, ["BODY.PEEK[]"])
        except Exception as error:
            logger.info("The mailbox refused to hand over messages: %s", error)
            raise MailboxError(str(_("The mailbox refused to hand over the messages."))) from error
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


class _ProvesTheName(ssl.SSLContext):
    """Dial the address that was approved; prove the certificate against the name typed.

    IMAPClient takes the TLS server name from whatever it was told to connect to, which is
    the approved address here — and a certificate checked against a number never matches.
    So the name travels with the context and is put back at the moment the socket is
    wrapped, which covers both ways this plugin gets to TLS: implicit on port 993, and
    ``STARTTLS`` on 143. It is the shape ``postulo.core.destinations`` uses for SMTP and
    ``plugins/http.py`` uses with ``sni_hostname``, for the same reason.
    """

    #: The host somebody typed, which is what the certificate has to be good for.
    server_name: str = ""

    def wrap_socket(self, sock, *args, **kwargs):
        return super().wrap_socket(sock, *args, **{**kwargs, "server_hostname": self.server_name})


def tls_context(server_name: str) -> ssl.SSLContext:
    """Python's default client context, told which name it is proving."""
    context = ssl.create_default_context()
    context.__class__ = _ProvesTheName
    context.server_name = server_name
    return context


def close(client) -> None:
    """Log out, and never let a failure to hang up hide what the run actually did."""
    try:
        client.logout()
    except Exception:
        logger.debug("The mailbox did not acknowledge the logout", exc_info=True)


def connect(config: dict) -> IMAPClient:
    """Open the account, at an address the instance's policy allows. The caller closes it.

    Two failures, two answers, and neither of them repeats what the server said. *Could not
    reach it* is an address, a port or a security setting that is wrong, or a server that is
    down; *sign-in refused* is a username or a password. Anything more precise — which of
    the two a mailbox admitted to, how long it took, what its banner says — is exactly the
    reconnaissance the destination check exists to prevent.
    """
    host = str(config.get("host", "")).strip()
    port = int(config.get("port") or 993)
    starttls = str(config.get("security")) == "starttls"
    try:
        address = approve_host(host)
    except DestinationRefused as error:
        raise MailboxError(str(error)) from error
    context = tls_context(host)
    try:
        client = IMAPClient(
            address, port=port, ssl=not starttls, ssl_context=context, timeout=TIMEOUT
        )
        if starttls:
            client.starttls(context)
    except Exception as error:
        logger.info("%s:%s did not answer: %s: %s", host, port, type(error).__name__, error)
        raise MailboxError(
            str(_("Could not reach the server. Check the address, the port and the security."))
        ) from error
    try:
        client.login(str(config.get("username", "")), str(config.get("password", "")))
    except Exception as error:
        logger.info("%s refused the sign-in: %s: %s", host, type(error).__name__, error)
        close(client)
        raise MailboxError(
            str(_("Sign-in refused. Check the username and the password."))
        ) from error
    return client
