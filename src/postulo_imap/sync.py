"""The sync: read the folder, match, classify, suggest, mark what has been read.

Nothing here writes to the record. Every message that the rules recognise becomes a
**suggestion** in Postulo's review queue, matched to an application when the evidence
allows and left unmatched when it does not; the person accepts or declines, and neither
answer is asked for twice because a suggestion is keyed on the message's own identifier.

What is written *to the mailbox* is one keyword, ``$Postulo``, on messages that have been
read — invisible in most clients, and the reason a second pass is cheap. An operator who
would rather have them filed away can have them moved to a subfolder instead, and one who
wants Postulo to touch nothing can turn both off. Whichever it is, it applies to every
message the pass read, recognised or not, and never to the inbox, which is not read at all.

A message the plugin cannot read, or cannot file, is that message's problem: it is counted
in a note, flagged with the rest, and the pass goes on (postulo-imap#8). A flag or a move
the server refuses leaves the report as it was, with a note that the next pass will read
those messages again (postulo-imap#22).
"""

from __future__ import annotations

import logging

from django.db import transaction
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy, ngettext
from postulo.plugins.api import (
    Application,
    FieldSpec,
    Suggestion,
    SyncReport,
    TestResult,
    suggest,
)

from . import __version__, classifying, mailbox
from .mailbox import KEYWORD, Mailbox, MailboxError, close
from .matching import match

#: How the person's mail is left afterwards.
MARK_KEYWORD = "keyword"
MARK_MOVE = "move"
MARK_NOTHING = "nothing"

logger = logging.getLogger(__name__)


def _is_inbox(name: str) -> bool:
    """IMAP treats the one name `INBOX` without regard to case, and nothing else so."""
    return name.strip().upper() == "INBOX"


def _folder(config: dict) -> str:
    """The folder to read, which is never the inbox, whatever was saved before the rule."""
    folder = str(config.get("folder") or "Jobs").strip()
    if _is_inbox(folder):
        raise MailboxError(
            str(
                _(
                    "Postulo never reads the inbox. Name the folder your mail rule files "
                    "jobs mail into."
                )
            )
        )
    return folder


class ImapSync:
    name = "imap"
    version = __version__
    kind = "sync"
    label = gettext_lazy("Mailbox (IMAP)")

    def config_fields(self) -> list[FieldSpec]:
        return [
            FieldSpec(
                "host", str(_("Server")), type="text", help=str(_("For example imap.example.org."))
            ),
            FieldSpec(
                "port",
                str(_("Port")),
                type="integer",
                required=False,
                help=str(_("Left empty: 993 with TLS, 143 with STARTTLS.")),
            ),
            FieldSpec(
                "security",
                str(_("Security")),
                type="choice",
                choices=(
                    ("ssl", str(_("TLS (port 993)"))),
                    ("starttls", str(_("STARTTLS (port 143)"))),
                ),
                default="ssl",
            ),
            FieldSpec("username", str(_("Username")), type="text"),
            FieldSpec(
                "password",
                str(_("Password or app password")),
                type="password",
                secret=True,
                help=str(
                    _(
                        "An app password wherever the server offers one; Gmail requires it with "
                        "two-factor authentication on. Stored encrypted."
                    )
                ),
            ),
            FieldSpec(
                "folder",
                str(_("Folder to read")),
                type="text",
                default="Jobs",
                help=str(
                    _(
                        "One folder, never the inbox. Make a rule on your mail server that files "
                        "recruiter mail here; Postulo reads nothing else. Every message it reads "
                        "is flagged or moved afterwards, whether or not it was recognised."
                    )
                ),
            ),
            FieldSpec(
                "mark",
                str(_("Once read")),
                type="choice",
                choices=(
                    (MARK_KEYWORD, str(_("Flag it with %(keyword)s") % {"keyword": KEYWORD})),
                    (MARK_MOVE, str(_("Move it to a subfolder"))),
                    (MARK_NOTHING, str(_("Leave the mailbox untouched"))),
                ),
                default=MARK_KEYWORD,
                help=str(
                    _(
                        "Flagging is invisible in most clients and keeps each pass short. Leaving "
                        "the mailbox untouched means every recent message is read again; nothing "
                        "is suggested twice either way."
                    )
                ),
            ),
            FieldSpec(
                "move_to",
                str(_("Subfolder")),
                type="text",
                required=False,
                default="Jobs/Read",
                help=str(_("Used only when messages are moved. Created if missing.")),
            ),
            FieldSpec(
                "days",
                str(_("Look back")),
                type="integer",
                required=False,
                default=mailbox.DEFAULT_SINCE_DAYS,
                help=str(_("How many days of mail a pass considers.")),
            ),
        ]

    def summary(self, config: dict) -> str:
        who = f"{config.get('username', '')}@{config.get('host', '')}".strip("@")
        return f"{who} · {config.get('folder') or 'Jobs'}"

    def validate(self, config: dict) -> dict[str, list[str]]:
        problems: dict[str, list[str]] = {}
        folder = str(config.get("folder", "")).strip()
        if not folder:
            problems["folder"] = [str(_("Name the folder to read. Postulo never reads the inbox."))]
        elif _is_inbox(folder):
            problems["folder"] = [
                str(
                    _(
                        "Postulo never reads the inbox. Name the folder your mail rule files "
                        "jobs mail into."
                    )
                )
            ]
        if str(config.get("mark")) == MARK_MOVE:
            move_to = str(config.get("move_to", "")).strip()
            if not move_to:
                problems["move_to"] = [str(_("Name the subfolder to move messages to."))]
            elif move_to.casefold() == folder.casefold():
                problems["move_to"] = [str(_("Name a subfolder other than the folder being read."))]
        limits = (
            ("port", 65535, _("Give a port between 1 and 65535, or leave it empty.")),
            (
                "days",
                mailbox.MAX_SINCE_DAYS,
                _("Give a number of days between 1 and %(max)d.") % {"max": mailbox.MAX_SINCE_DAYS},
            ),
        )
        for key, highest, message in limits:
            given = config.get(key)
            if given is None or given == "":
                continue
            try:
                number = int(given)
            except (TypeError, ValueError):
                number = 0
            if not 1 <= number <= highest:
                problems[key] = [str(message)]
        return problems

    # ---------------------------------------------------------------- test

    def test(self, config: dict) -> TestResult:
        try:
            folder = _folder(config)
            client = mailbox.connect(config)
        except MailboxError as error:
            return TestResult(False, str(error))
        try:
            box = Mailbox(client, folder)
            if not box.exists():
                return TestResult(
                    False,
                    str(_("Signed in, but there is no folder called “%(folder)s”."))
                    % {"folder": folder},
                )
            if str(config.get("mark")) == MARK_MOVE and not box.can_move():
                return TestResult(
                    False,
                    str(
                        _(
                            "Signed in, but this server cannot move messages. Choose to flag "
                            "them instead."
                        )
                    ),
                )
            box.select(readonly=True)
            waiting = box.unread_uids(
                since_days=mailbox.days_of(config),
                use_keyword=str(config.get("mark") or MARK_KEYWORD) == MARK_KEYWORD,
            )
        except MailboxError as error:
            return TestResult(False, str(error))
        finally:
            close(client)
        return TestResult(
            True,
            ngettext(
                "Signed in. %(count)d message in “%(folder)s” is waiting to be read.",
                "Signed in. %(count)d messages in “%(folder)s” are waiting to be read.",
                len(waiting),
            )
            % {"count": len(waiting), "folder": folder},
        )

    # ---------------------------------------------------------------- sync

    def sync(self, connection, config: dict) -> SyncReport:
        how = str(config.get("mark") or MARK_KEYWORD)
        try:
            folder = _folder(config)
            client = mailbox.connect(config)
        except MailboxError as error:
            return SyncReport(error=str(error))
        try:
            return self._pass(connection, config, client, folder, how)
        finally:
            close(client)

    def _pass(self, connection, config: dict, client, folder: str, how: str) -> SyncReport:
        report = SyncReport()
        box = Mailbox(client, folder)
        try:
            box.select(readonly=how == MARK_NOTHING)
            use_keyword = how == MARK_KEYWORD
            if use_keyword and not box.keeps_keywords:
                # A folder that will not keep a keyword of ours: flagging would do nothing,
                # and the next pass would read everything again, as the untouched mode does.
                use_keyword = False
                report.notes.append(
                    str(
                        _(
                            "This folder does not keep flags of its own, so every message of the "
                            "look-back is read again on each pass."
                        )
                    )
                )
            uids = box.unread_uids(since_days=mailbox.days_of(config), use_keyword=use_keyword)
        except MailboxError as error:
            return SyncReport(error=str(error))
        applications = list(
            Application.objects.for_user(connection.owner).open().select_related("posting__company")
        )
        unmatched = failed = 0
        done: list[int] = []
        stopped: MailboxError | None = None
        for batch in box.batches(uids):
            try:
                sizes, identifiers = box.survey(batch, with_identifiers=not use_keyword)
                known = self._known(connection.owner, identifiers)
                fresh = [uid for uid in batch if uid not in known]
                mails, unreadable = box.read(fresh, sizes)
            except MailboxError as error:
                stopped = error
                break
            report.skipped += len(batch) - len(fresh) + len(unreadable)
            failed += len(unreadable)
            for mail in mails:
                try:
                    # Its own savepoint: a row the database refuses ends this message, not
                    # the transaction a web request or the scheduler holds.
                    with transaction.atomic():
                        outcome = self._file(connection, mail, applications)
                except Exception as error:
                    logger.warning(
                        "Message %s of %r could not be filed: %s: %s",
                        mail.uid,
                        folder,
                        type(error).__name__,
                        error,
                    )
                    failed += 1
                    report.skipped += 1
                    continue
                if outcome == "skipped":
                    report.skipped += 1
                else:
                    report.pulled += 1
                    unmatched += outcome == "unmatched"
            done.extend(batch)

        if stopped is not None:
            if not done:
                return SyncReport(error=str(stopped))
            report.notes.append(str(stopped))

        # Flagged or moved with the rest even when unreadable, so one message nothing can
        # read is not fetched again by every pass until it leaves the look-back.
        try:
            if use_keyword:
                box.mark(done)
            elif how == MARK_MOVE:
                box.move(done, str(config.get("move_to") or "Jobs/Read"))
        except MailboxError as error:
            report.notes.append(str(error))

        if unmatched:
            report.notes.append(
                ngettext(
                    "%(count)d could not be matched to an application; attach it by hand.",
                    "%(count)d could not be matched to an application; attach them by hand.",
                    unmatched,
                )
                % {"count": unmatched}
            )
        if failed:
            report.notes.append(
                ngettext(
                    "%(count)d message could not be read and was skipped.",
                    "%(count)d messages could not be read and were skipped.",
                    failed,
                )
                % {"count": failed}
            )
        return report

    def _known(self, owner, identifiers: dict[int, str]) -> set[int]:
        """The uids whose message already has a suggestion, so it is not downloaded again."""
        limit = mailbox.MAX_ID_CHARS
        wanted = {identifier[:limit] for identifier in identifiers.values() if identifier}
        if not wanted:
            return set()
        filed = set(
            Suggestion.objects.for_user(owner)
            .filter(source=self.name, external_id__in=wanted)
            .values_list("external_id", flat=True)
        )
        return {
            uid
            for uid, identifier in identifiers.items()
            if identifier and identifier[:limit] in filed
        }

    def _file(self, connection, mail, applications) -> str:
        """Classify, match and suggest one message: "skipped", "matched" or "unmatched"."""
        verdict = classifying.classify(mail.haystack)
        if verdict is None:
            return "skipped"
        found = match(connection.owner, mail, applications=applications)
        _suggestion, created = suggest(
            connection.owner,
            source=self.name,
            external_id=mail.message_id,
            application=found.application if found else None,
            kind=verdict.event_kind,
            summary=mail.subject or str(_("A message with no subject")),
            body=mail.text,
            occurred_at=mail.date,
            suggested_status=verdict.suggested_status,
            proposed_dates=verdict.dates,
            context={
                str(_("From")): f"{mail.sender_name} <{mail.sender}>".strip(),
                str(_("Matched by")): found.label if found else str(_("nothing yet")),
                str(_("Recognised")): f"{verdict.label} ({verdict.language}): {verdict.matched}",
            },
        )
        if not created:
            return "skipped"
        return "matched" if found else "unmatched"
