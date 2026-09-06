"""The sync: read the folder, match, classify, suggest, mark what has been read.

Nothing here writes to the record. Every message that the rules recognise becomes a
**suggestion** in Postulo's review queue, matched to an application when the evidence
allows and left unmatched when it does not; the person accepts or declines, and neither
answer is asked for twice because a suggestion is keyed on the message's own identifier.

What is written *to the mailbox* is one keyword, ``$Postulo``, on messages that have been
read — invisible in most clients, and the reason a second pass is cheap. An operator who
would rather have them filed away can have them moved to a subfolder instead, and one who
wants Postulo to touch nothing can turn both off.
"""

from __future__ import annotations

import logging

from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from postulo.applications.models import Application
from postulo.applications.suggestions import suggest
from postulo.plugins.base import FieldSpec, SyncReport, TestResult

from . import __version__, classifying, mailbox
from .mailbox import KEYWORD, Mailbox, MailboxError
from .matching import match

#: How the person's mail is left afterwards.
MARK_KEYWORD = "keyword"
MARK_MOVE = "move"
MARK_NOTHING = "nothing"

logger = logging.getLogger(__name__)


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
            FieldSpec("port", str(_("Port")), type="integer", required=False, default=993),
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
                        "recruiter mail here; Postulo reads nothing else."
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
        if str(config.get("mark")) == MARK_MOVE and not str(config.get("move_to", "")).strip():
            problems["move_to"] = [str(_("Name the subfolder to move messages to."))]
        if not str(config.get("folder", "")).strip():
            problems["folder"] = [str(_("Name the folder to read. Postulo never reads the inbox."))]
        return problems

    # ---------------------------------------------------------------- test

    def test(self, config: dict) -> TestResult:
        try:
            client = mailbox.connect(config)
        except MailboxError as error:
            return TestResult(False, str(error))
        try:
            folder = str(config.get("folder") or "Jobs")
            if not client.folder_exists(folder):
                return TestResult(
                    False,
                    str(_("Signed in, but there is no folder called “%(folder)s”."))
                    % {"folder": folder},
                )
            box = Mailbox(client, folder)
            box.select(readonly=True)
            waiting = box.unread_uids(
                since_days=int(config.get("days") or mailbox.DEFAULT_SINCE_DAYS),
                use_keyword=str(config.get("mark")) == MARK_KEYWORD,
            )
        except MailboxError as error:
            return TestResult(False, str(error))
        finally:
            _close(client)
        return TestResult(
            True,
            str(_("Signed in. %(count)d messages in “%(folder)s” are waiting to be read."))
            % {"count": len(waiting), "folder": folder},
        )

    # ---------------------------------------------------------------- sync

    def sync(self, connection, config: dict) -> SyncReport:
        report = SyncReport()
        how = str(config.get("mark") or MARK_KEYWORD)
        client = mailbox.connect(config)
        try:
            box = Mailbox(client, str(config.get("folder") or "Jobs"))
            box.select(readonly=how == MARK_NOTHING)
            uids = box.unread_uids(
                since_days=int(config.get("days") or mailbox.DEFAULT_SINCE_DAYS),
                use_keyword=how == MARK_KEYWORD,
            )
            mails = box.fetch(uids)
            applications = list(
                Application.objects.for_user(connection.owner)
                .open()
                .select_related("posting__company")
            )
            unmatched = 0
            for mail in mails:
                verdict = classifying.classify(mail.haystack)
                if verdict is None:
                    report.skipped += 1
                    continue
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
                        str(_("Matched by")): found.how if found else str(_("nothing yet")),
                        str(
                            _("Recognised")
                        ): f"{verdict.kind} ({verdict.language}): {verdict.matched}",
                    },
                )
                if created:
                    report.pulled += 1
                    if found is None:
                        unmatched += 1
                else:
                    report.skipped += 1

            if how == MARK_KEYWORD:
                box.mark(uids)
            elif how == MARK_MOVE and uids:
                box.move(uids, str(config.get("move_to") or "Jobs/Read"))

            if unmatched:
                report.notes.append(
                    str(_("%(count)d could not be matched to an application; attach them by hand."))
                    % {"count": unmatched}
                )
        finally:
            _close(client)
        return report


def _close(client) -> None:
    """Log out, and never let a failure to hang up hide what the run actually did."""
    try:
        client.logout()
    except Exception:
        logger.debug("The mailbox did not acknowledge the logout", exc_info=True)
