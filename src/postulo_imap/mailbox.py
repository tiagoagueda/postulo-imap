"""Talking to the mailbox, and turning a message into the few fields this plugin uses.

IMAPClient does the protocol; the standard library's ``email`` does the parsing. What is
read is deliberately small: the headers that identify and thread a message, the date, the
sender, the subject, and the first plain-text part, shortened. The body is not stored
anywhere by default — only a short excerpt travels with a suggestion — because a job
tracker holding a copy of somebody's mailbox is a different and much larger promise.

**What is transferred is small too** (postulo-imap#20). A pass asks for its messages a batch
at a time, learns each one's size first, and takes a message above ``FULL_LIMIT`` as its
headers plus the first ``PARTIAL_TEXT`` bytes of its text rather than whole, so the memory a
pass needs is set by a batch and not by what somebody put in the folder. Every header and
every decoded part is cut to a fixed size before anything searches it, and the HTML is read
by a parser that takes linear time, so a message built to be slow to read is read in the
time a message of its size takes.

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
import hashlib
import logging
import re
import ssl
from collections.abc import Iterator
from dataclasses import dataclass, field
from email.header import decode_header, make_header
from email.message import Message
from html.parser import HTMLParser

from django.utils.translation import gettext as _
from imapclient import IMAPClient
from postulo.plugins.api import DestinationRefused, approve_host

from .normalising import normalise

#: What travels with a suggestion, at most. An advert-length excerpt is plenty to
#: recognise a message by; the mailbox keeps the rest.
EXCERPT_CHARS = 2000
#: How far back a first pass looks, in days, when nothing has been marked yet.
DEFAULT_SINCE_DAYS = 30
#: The longest look-back a connection may ask for, in days (ten years).
MAX_SINCE_DAYS = 3650
#: The IMAP keyword put on a message once it has been read.
KEYWORD = "$Postulo"
#: Long enough for a slow server, short enough that a scheduled pass cannot hang on one.
TIMEOUT = 30

#: How many messages one command asks for, and so how many are held at once.
FETCH_BATCH = 25
#: A message above this many bytes is not fetched whole.
FULL_LIMIT = 1_000_000
#: How much of the text of such a message is fetched after its headers.
PARTIAL_TEXT = 65_536
#: The most of one decoded part that is looked at: an excerpt is two thousand characters.
MAX_PART_BYTES = 200_000
#: The most of one header that is looked at.
MAX_HEADER_CHARS = 4096
#: How many identifiers of ``References`` threading uses: the parent is the last one
#: (RFC 5322 §3.6.4), and the ones before it only repeat what an earlier message matched.
MAX_REFERENCES = 8
#: The longest identifier a suggestion can be keyed on.
MAX_ID_CHARS = 250

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
        """What the rules and the matching read: lower case, one space between words."""
        return normalise(f"{self.subject}\n{self.text}").lower()


# ------------------------------------------------------------------ reading text

#: NUL (which PostgreSQL refuses in text), the other control characters a person never typed,
#: and lone surrogates (which cannot be written as UTF-8 at all).
_UNSAFE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ud800-\udfff]")
_SPACES = re.compile(r"\s+")
_IDENTIFIER = re.compile(rf"<[^<>\s]{{1,{MAX_ID_CHARS}}}>")


def _clean(text: str) -> str:
    return _UNSAFE.sub("", text)


def _raw_text(value: str, *, limit: int = MAX_HEADER_CHARS, tail: bool = False) -> str:
    """A header value as text, whatever bytes it carried.

    The parser hands back a header with a byte outside ASCII as ``email.header.Header``,
    which neither ``parseaddr`` nor ``parsedate_to_datetime`` can read (postulo-imap#21), so
    the raw string is taken instead, with each such byte still escaped in it, and put back
    to bytes: UTF-8 when it is, which is what internationalised mail sends, else Latin-1.
    """
    value = value[-limit:] if tail else value[:limit]
    try:
        raw = value.encode("utf-8", "surrogateescape")
    except UnicodeError:
        return _clean(value.encode("utf-8", "replace").decode("utf-8"))
    try:
        return _clean(raw.decode("utf-8"))
    except UnicodeDecodeError:
        return _clean(raw.decode("latin-1"))


def _header(message: Message, name: str, *, tail: bool = False) -> str:
    """The first header called ``name``, as text, cut to ``MAX_HEADER_CHARS``."""
    wanted = name.lower()
    for key, value in message.raw_items():
        if key.lower() == wanted:
            return _raw_text(str(value), tail=tail)
    return ""


def _decoded(value: str | None) -> str:
    """A header as text: RFC 2047 words decoded, whitespace tidied."""
    if not value:
        return ""
    try:
        text = str(make_header(decode_header(value)))
    except Exception:
        text = value
    return _SPACES.sub(" ", _clean(text)).strip()


class _Text(HTMLParser):
    """The text of an HTML part, in time that grows with its size and no faster.

    ``<head>`` goes with ``<script>`` and ``<style>``: the title of a newsletter is not what
    the message says. A tag that ends a line ends one here and an inline tag ends nothing, so
    ``re<b>gret</b>`` is still one word and ``<div>a</div><div>b</div>`` is two lines.
    """

    SKIPPED = frozenset({"script", "style", "head", "title", "noscript", "template"})
    LINES = frozenset(
        {"br", "p", "div", "tr", "li", "ul", "ol", "table", "h1", "h2", "h3", "h4", "h5", "h6"}
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skipping = 0

    def handle_starttag(self, tag, attrs):
        if tag == "body":
            # A document that never closed its <head> would otherwise lose its whole text.
            self.skipping = 0
        elif tag in self.SKIPPED:
            self.skipping += 1
        elif tag in self.LINES:
            self.parts.append("\n")

    def handle_startendtag(self, tag, attrs):
        if tag in self.LINES:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIPPED:
            self.skipping = max(0, self.skipping - 1)
        elif tag in self.LINES:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skipping:
            self.parts.append(data)


def _strip_html(html: str) -> str:
    parser = _Text()
    try:
        parser.feed(html[:MAX_PART_BYTES])
        parser.close()
    except Exception:
        logger.info("An HTML part could not be read to its end", exc_info=True)
    return "".join(parser.parts)


def _payload(part: Message) -> str:
    try:
        raw = part.get_payload(decode=True)
    except Exception:
        return ""
    if not isinstance(raw, bytes):
        return ""
    raw = raw[:MAX_PART_BYTES]
    charset = part.get_content_charset() or "utf-8"
    try:
        text = raw.decode(charset, errors="replace")
    except (LookupError, UnicodeError, ValueError):
        # A name no codec knows, or one (``idna``, ``undefined``) that is a codec but not a
        # character set and refuses ``errors="replace"``.
        text = raw.decode("utf-8", errors="replace")
    return _clean(text)


def plain_text(message: Message) -> str:
    """The first plain-text part, or the HTML as text, shortened. Line breaks are kept."""
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
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(line.rstrip() for line in lines)).strip()
    return text[:EXCERPT_CHARS]


def _threading(message: Message) -> list[str]:
    """The identifiers that tie a message to an earlier one: its parent, and the last few.

    Few on purpose: they go into one database ``IN`` list, which a header of thousands
    would overflow, and the ones before the last only repeat what an earlier message
    already matched (postulo-imap#15).
    """
    parent = _IDENTIFIER.findall(_header(message, "In-Reply-To"))[-1:]
    earlier = _IDENTIFIER.findall(_header(message, "References", tail=True))[-MAX_REFERENCES:]
    return list(dict.fromkeys([*earlier, *parent]))


def parse(uid: int, raw: bytes) -> Mail:
    message = email.message_from_bytes(raw)
    name, address = email.utils.parseaddr(_header(message, "From"))
    date = None
    if value := _header(message, "Date"):
        try:
            date = email.utils.parsedate_to_datetime(value)
        except Exception:
            date = None
    if date is not None and date.tzinfo is None:
        date = date.replace(tzinfo=dt.UTC)
    return Mail(
        uid=uid,
        # A message with no identifier is known by what it says, not by where it sits: a UID
        # repeats across folders, mailboxes and servers, and a repeat is a suggestion that
        # is never filed (postulo-imap#11).
        message_id=_decoded(_header(message, "Message-ID"))
        or f"sha256-{hashlib.sha256(raw).hexdigest()}",
        sender=_clean(address).lower(),
        sender_name=_decoded(name),
        subject=_decoded(_header(message, "Subject")),
        date=date,
        text=plain_text(message),
        references=_threading(message),
    )


def identifier_of(header_block: bytes) -> str:
    """The Message-ID in a block of headers, or "" when it has none."""
    try:
        return _decoded(_header(email.message_from_bytes(header_block), "Message-ID"))
    except Exception:
        return ""


# ------------------------------------------------------------------- the mailbox


def _part(answer: dict, prefix: bytes) -> bytes:
    """The data item of a FETCH answer whose name starts with ``prefix``.

    A partial fetch is answered under a name with its offset in it (``BODY[TEXT]<0>``).
    """
    for key, value in answer.items():
        if isinstance(key, bytes) and key.upper().startswith(prefix) and value:
            return value
    return b""


class Mailbox:
    """One account, one folder, opened for as long as a run takes."""

    def __init__(self, client: IMAPClient, folder: str) -> None:
        self.client = client
        self.folder = folder
        #: Whether the folder keeps keywords of its own, as far as it said. Unknown is yes.
        self.keeps_keywords = True

    def exists(self, folder: str | None = None) -> bool:
        try:
            return bool(self.client.folder_exists(folder or self.folder))
        except Exception as error:
            logger.info("The mailbox refused to look for a folder: %s", error)
            raise MailboxError(str(_("The mailbox refused to look for the folder."))) from error

    def select(self, *, readonly: bool = False) -> None:
        try:
            answer = self.client.select_folder(self.folder, readonly=readonly)
        except Exception as error:
            logger.info("The folder %r could not be opened: %s", self.folder, error)
            raise MailboxError(
                str(_("The folder “%(folder)s” could not be opened.")) % {"folder": self.folder}
            ) from error
        permanent = answer.get(b"PERMANENTFLAGS") if isinstance(answer, dict) else None
        if permanent:
            # RFC 3501 §6.3.1: ``\*`` says a client may create new keywords. A folder without
            # it ignores ours, and a pass that relied on the flag would read everything again.
            self.keeps_keywords = b"\\*" in permanent or KEYWORD.encode() in permanent

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

    @staticmethod
    def batches(uids: list[int]) -> Iterator[list[int]]:
        for start in range(0, len(uids), FETCH_BATCH):
            yield uids[start : start + FETCH_BATCH]

    def _fetch(self, uids: list[int], parts: list[str]) -> dict:
        try:
            return self.client.fetch(uids, parts)
        except Exception as error:
            logger.info("The mailbox refused to hand over messages: %s", error)
            raise MailboxError(str(_("The mailbox refused to hand over the messages."))) from error

    def survey(
        self, uids: list[int], *, with_identifiers: bool = False
    ) -> tuple[dict[int, int], dict[int, str]]:
        """How big each message is, and (when asked) what its Message-ID says.

        The headers alone, so that a message already filed is never downloaded again.
        """
        parts = ["RFC822.SIZE"]
        if with_identifiers:
            parts.append("BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)]")
        answer = self._fetch(uids, parts)
        sizes: dict[int, int] = {}
        identifiers: dict[int, str] = {}
        for uid, data in answer.items():
            sizes[int(uid)] = int(data.get(b"RFC822.SIZE") or FULL_LIMIT + 1)
            if with_identifiers:
                identifiers[int(uid)] = identifier_of(_part(data, b"BODY[HEADER.FIELDS"))
        return sizes, identifiers

    def read(self, uids: list[int], sizes: dict[int, int]) -> tuple[list[Mail], list[int]]:
        """The messages of one batch, and the uids of those that could not be read.

        A message that cannot be parsed is the message's problem and not the pass's
        (postulo-imap#8): it is logged, counted and handed back so that it is flagged with
        the rest and not fetched again on every pass until it ages out of the look-back.
        """
        whole = [uid for uid in uids if sizes.get(uid, FULL_LIMIT + 1) <= FULL_LIMIT]
        raws: dict[int, bytes] = {}
        if whole:
            for uid, data in self._fetch(whole, ["BODY.PEEK[]"]).items():
                raws[int(uid)] = data.get(b"BODY[]") or data.get(b"RFC822") or b""
        for uid in uids:
            if uid in whole:
                continue
            # Too big to hold: its headers and the start of its text say all this plugin
            # reads of it, and the attachments are somebody else's business.
            answer = self._fetch(
                [uid], ["BODY.PEEK[HEADER]", f"BODY.PEEK[TEXT]<0.{PARTIAL_TEXT}>"]
            ).get(uid, {})
            raws[uid] = _part(answer, b"BODY[HEADER]") + _part(answer, b"BODY[TEXT]")
        mails: list[Mail] = []
        unreadable: list[int] = []
        for uid in sorted(uids):
            raw = raws.get(uid, b"")
            try:
                if not raw:
                    raise ValueError("the server sent nothing for this message")
                mails.append(parse(uid, raw))
            except Exception as error:
                logger.warning(
                    "Message %s of %r could not be read: %s: %s",
                    uid,
                    self.folder,
                    type(error).__name__,
                    error,
                )
                unreadable.append(uid)
        return mails, unreadable

    def mark(self, uids: list[int]) -> None:
        if not uids:
            return
        try:
            self.client.add_flags(uids, [KEYWORD])
        except Exception as error:
            logger.info("The mailbox refused to flag messages: %s: %s", type(error).__name__, error)
            raise MailboxError(
                str(
                    _(
                        "The messages were read but could not be flagged; the next pass will "
                        "read them again."
                    )
                )
            ) from error

    def can_move(self) -> bool:
        """Whether the server can move messages: MOVE, or copying and then deleting by UID."""
        try:
            return bool(self.client.has_capability("MOVE") or self.client.has_capability("UIDPLUS"))
        except Exception:
            return False

    def move(self, uids: list[int], destination: str) -> None:
        if not uids:
            return
        try:
            if not self.can_move():
                raise MailboxError(
                    str(
                        _(
                            "The messages were read, but this server cannot move them; the next "
                            "pass will read them again."
                        )
                    )
                )
            if not self.client.folder_exists(destination):
                self.client.create_folder(destination)
            if self.client.has_capability("MOVE"):
                self.client.move(uids, destination)
            else:
                # Copy, mark as deleted, and expunge those uids only: a plain EXPUNGE would
                # also remove whatever else the person had marked for deletion in the folder.
                self.client.copy(uids, destination)
                self.client.delete_messages(uids)
                self.client.expunge(uids)
        except MailboxError:
            raise
        except Exception as error:
            logger.info("The mailbox refused to move messages: %s: %s", type(error).__name__, error)
            raise MailboxError(
                str(
                    _(
                        "The messages were read but could not be moved to “%(folder)s”; the "
                        "next pass will read them again."
                    )
                )
                % {"folder": destination}
            ) from error


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


def port_of(config: dict) -> int:
    """The port to dial: the one typed, else the one the chosen security uses."""
    starttls = str(config.get("security")) == "starttls"
    try:
        port = int(config.get("port") or 0)
    except (TypeError, ValueError):
        port = 0
    return port if 0 < port <= 65535 else (143 if starttls else 993)


def days_of(config: dict) -> int:
    """The look-back in days: the one typed when it is sane, else the default."""
    try:
        days = int(config.get("days") or 0)
    except (TypeError, ValueError):
        days = 0
    return days if 1 <= days <= MAX_SINCE_DAYS else DEFAULT_SINCE_DAYS


def connect(config: dict) -> IMAPClient:
    """Open the account, at an address the instance's policy allows. The caller closes it.

    Two failures, two answers, and neither of them repeats what the server said. *Could not
    reach it* is an address, a port or a security setting that is wrong, or a server that is
    down; *sign-in refused* is a username or a password. Anything more precise — which of
    the two a mailbox admitted to, how long it took, what its banner says — is exactly the
    reconnaissance the destination check exists to prevent.
    """
    host = str(config.get("host", "")).strip()
    port = port_of(config)
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
