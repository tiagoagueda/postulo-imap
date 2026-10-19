"""The plugin, against a mailbox that lives in memory, and inside a real Postulo."""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
from email.message import EmailMessage
from typing import ClassVar

import pytest
from django.utils import timezone
from postulo.applications.models import (
    Application,
    ApplicationEvent,
    EventKind,
    Status,
    Suggestion,
    SuggestionStatus,
)
from postulo.applications.services import change_status
from postulo.applications.suggestions import accept
from postulo.jobs.models import Company, Contact, JobPosting
from postulo.plugins import registry, syncing
from postulo.plugins.api import SyncPlugin, is_language_tag, language_tag
from postulo.plugins.api import approve_host as real_approve_host
from postulo.plugins.models import Connection

from postulo_imap import classifying, mailbox
from postulo_imap import sync as sync_module
from postulo_imap.mailbox import KEYWORD, Mail, parse, plain_text
from postulo_imap.matching import HOW_LABELS, domain_of, match, registrable
from postulo_imap.sync import ImapSync

pytestmark = pytest.mark.django_db

CONFIG = {
    "host": "imap.example.org",
    "port": 993,
    "security": "ssl",
    "username": "ana",
    "password": "app-pw",
    "folder": "Jobs",
    "mark": "keyword",
    "move_to": "Jobs/Read",
    "days": 30,
}


def a_message(
    *,
    sender="careers@blackmesa.test",
    name="Black Mesa Careers",
    subject="Your application",
    body="Thank you for your application. We have received it.",
    message_id="<m1@blackmesa.test>",
    when=None,
    in_reply_to="",
    html=False,
) -> bytes:
    message = EmailMessage()
    message["From"] = f"{name} <{sender}>" if name else sender
    message["To"] = "ana@example.org"
    message["Subject"] = subject
    if message_id:
        message["Message-ID"] = message_id
    message["Date"] = (when or timezone.now()).strftime("%a, %d %b %Y %H:%M:%S %z")
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
    if html:
        # HTML only, as plenty of recruiting systems send: there is no plain part to prefer.
        message.set_content(f"<html><body><p>{body}</p></body></html>", subtype="html")
    else:
        message.set_content(body)
    return message.as_bytes()


class FakeIMAP:
    """Enough of IMAPClient: folders, a search by keyword and date, fetch, flags, move."""

    instances: ClassVar[list[FakeIMAP]] = []

    def __init__(self, host, port=993, ssl=True, ssl_context=None, timeout=None):
        self.host, self.port, self.ssl, self.ssl_context = host, port, ssl, ssl_context
        self.folders: dict[str, dict[int, dict]] = {"Jobs": {}, "INBOX": {}}
        self.selected = ""
        self.readonly = False
        self.logged_in = False
        self.started_tls = False
        self.credentials: tuple[str, str] | None = None
        self.next_uid = 1
        #: Every FETCH, as (uids, parts): what a pass asked the server for.
        self.fetches: list[tuple[list[int], list[str]]] = []
        #: A size to report for a uid in place of the real one, to pretend a message is big.
        self.sizes: dict[int, int] = {}
        #: An exception to raise from a method, by name.
        self.fail: dict[str, Exception] = {}
        #: What the server says it can do, and what it says about the folder it selected.
        self.capabilities = {"MOVE", "UIDPLUS"}
        self.permanent_flags: tuple[bytes, ...] | None = None
        self.copied: list[tuple[list[int], str]] = []
        FakeIMAP.instances.append(self)

    def _maybe_fail(self, name):
        if name in self.fail:
            raise self.fail[name]

    def has_capability(self, name):
        return name in self.capabilities

    # -- connection
    def starttls(self, ssl_context=None):
        self.started_tls = True
        self.ssl_context = ssl_context

    def login(self, username, password):
        if (username, password) != ("ana", "app-pw"):
            raise ValueError("[AUTHENTICATIONFAILED] Invalid credentials")
        self.logged_in = True

    def logout(self):
        self.logged_in = False

    # -- folders
    def folder_exists(self, name):
        self._maybe_fail("folder_exists")
        return name in self.folders

    def create_folder(self, name):
        self.folders.setdefault(name, {})

    def select_folder(self, name, readonly=False):
        if name not in self.folders:
            raise ValueError(f"[NONEXISTENT] Unknown Mailbox: {name}")
        self.selected, self.readonly = name, readonly
        answer = {b"EXISTS": len(self.folders[name])}
        if self.permanent_flags is not None:
            answer[b"PERMANENTFLAGS"] = self.permanent_flags
        return answer

    # -- messages
    def add(self, raw: bytes, *, folder="Jobs", flags=(), date=None) -> int:
        uid = self.next_uid
        self.next_uid += 1
        self.folders[folder][uid] = {
            "raw": raw,
            "flags": set(flags),
            "date": date or dt.datetime.now(dt.UTC).date(),
        }
        return uid

    def search(self, criteria):
        messages = self.folders[self.selected]
        exclude_keyword = None
        since = None
        rest = list(criteria)
        if rest[:2] == ["NOT", "KEYWORD"]:
            exclude_keyword = rest[2]
            rest = rest[3:]
        if rest[:1] == ["SINCE"]:
            since = rest[1]
        found = []
        for uid, message in messages.items():
            if exclude_keyword and exclude_keyword in message["flags"]:
                continue
            if since and message["date"] < since:
                continue
            found.append(uid)
        return found

    def fetch(self, uids, parts):
        self._maybe_fail("fetch")
        self.fetches.append((list(uids), list(parts)))
        answer = {}
        for uid in uids:
            raw = self.folders[self.selected][uid]["raw"]
            pieces = [*re.split(rb"(\r?\n\r?\n)", raw, maxsplit=1), b"", b""]
            head, separator, body = pieces[:3]
            item = {}
            for part in parts:
                if part == "RFC822.SIZE":
                    item[b"RFC822.SIZE"] = self.sizes.get(uid, len(raw))
                elif part == "BODY.PEEK[]":
                    item[b"BODY[]"] = raw
                elif part == "BODY.PEEK[HEADER]":
                    item[b"BODY[HEADER]"] = head + separator
                elif part.startswith("BODY.PEEK[HEADER.FIELDS"):
                    wanted = [
                        line
                        for line in head.splitlines()
                        if line.lower().startswith(b"message-id:")
                    ]
                    item[b"BODY[HEADER.FIELDS (MESSAGE-ID)]"] = b"\r\n".join(wanted) + b"\r\n\r\n"
                elif part.startswith("BODY.PEEK[TEXT]<0."):
                    size = int(part.split(".")[-1].rstrip(">"))
                    item[b"BODY[TEXT]<0>"] = body[:size]
            answer[uid] = item
        return answer

    def add_flags(self, uids, flags):
        self._maybe_fail("add_flags")
        if self.readonly:
            raise ValueError("[READ-ONLY] Mailbox is read-only")
        for uid in uids:
            self.folders[self.selected][uid]["flags"].update(flags)

    def move(self, uids, destination):
        self._maybe_fail("move")
        if self.readonly:
            raise ValueError("[READ-ONLY] Mailbox is read-only")
        self.create_folder(destination)
        for uid in uids:
            self.folders[destination][uid] = self.folders[self.selected].pop(uid)

    def copy(self, uids, destination):
        self.copied.append((list(uids), destination))
        self.create_folder(destination)
        for uid in uids:
            self.folders[destination][uid] = dict(self.folders[self.selected][uid])

    def delete_messages(self, uids):
        for uid in uids:
            self.folders[self.selected][uid]["flags"].add("\\Deleted")

    def expunge(self, uids=None):
        for uid in list(self.folders[self.selected]):
            if (uids is None or uid in uids) and "\\Deleted" in self.folders[self.selected][uid][
                "flags"
            ]:
                del self.folders[self.selected][uid]


#: The order every rule list names its kinds in, most consequential first: a message that
#: both thanks somebody and invites them is an invitation.
MOST_CONSEQUENTIAL_FIRST = list(classifying.MOST_CONSEQUENTIAL_FIRST)
assert MOST_CONSEQUENTIAL_FIRST == ["offer", "rejected", "interview", "assessment", "acknowledged"]

#: What the fake resolver says `imap.example.org` answers with. A test never asks the real
#: one: a name that has to resolve is a test that fails on a train.
APPROVED = "198.51.100.25"


@pytest.fixture
def imap(monkeypatch):
    FakeIMAP.instances = []
    monkeypatch.setattr(mailbox, "IMAPClient", FakeIMAP)
    box = FakeIMAP("imap.example.org")
    FakeIMAP.instances = []

    def factory(host, port=993, ssl=True, ssl_context=None, timeout=None):
        box.host, box.port, box.ssl, box.ssl_context = host, port, ssl, ssl_context
        FakeIMAP.instances.append(box)
        return box

    monkeypatch.setattr(mailbox, "IMAPClient", factory)
    monkeypatch.setattr(mailbox, "approve_host", lambda host: APPROVED)
    return box


@pytest.fixture
def user(db):
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create_user(email="ana@example.org", password="pw" * 8)  # noqa: S106


@pytest.fixture
def connection(user):
    connection = Connection(
        owner=user,
        kind="sync",
        plugin="imap",
        label="Mailbox",
        config={key: value for key, value in CONFIG.items() if key != "password"},
    )
    connection.secrets = {"password": "app-pw"}
    connection.save()
    return connection


@pytest.fixture
def application(user):
    company = Company.objects.create(
        owner=user, name="Black Mesa", website="https://www.blackmesa.test/jobs"
    )
    posting = JobPosting.objects.create(owner=user, company=company, title="Research Engineer")
    application = Application.objects.create(owner=user, posting=posting, status=Status.DRAFT)
    change_status(application, Status.APPLIED, occurred_at=timezone.now() - dt.timedelta(days=9))
    application.refresh_from_db()
    return application


def a_mail(**overrides) -> Mail:
    return parse(1, a_message(**overrides))


# ------------------------------------------------------------------ reading


def test_a_message_is_read_down_to_the_fields_that_matter():
    mail = a_mail(subject="Re: Research Engineer", in_reply_to="<earlier@blackmesa.test>")
    assert mail.message_id == "<m1@blackmesa.test>"
    assert mail.sender == "careers@blackmesa.test" and mail.sender_name == "Black Mesa Careers"
    assert mail.subject == "Re: Research Engineer" and mail.domain == "blackmesa.test"
    assert "Thank you for your application" in mail.text
    assert mail.references == ["<earlier@blackmesa.test>"]
    assert mail.date is not None and mail.date.tzinfo is not None


def test_an_encoded_subject_and_html_body_are_understood():
    mail = parse(2, a_message(subject="=?utf-8?B?Q2FuZGlkYXR1cmUgcmXDp3Vl?=", html=True))
    assert mail.subject == "Candidature reçue"
    assert "Thank you for your application" in mail.text and "<p>" not in mail.text


def test_a_very_long_body_is_cut():
    message = EmailMessage()
    message["From"] = "a@b.test"
    message["Subject"] = "x"
    message["Message-ID"] = "<long@b.test>"
    message.set_content("word " * 5000)
    assert len(plain_text(message)) <= mailbox.EXCERPT_CHARS


# ----------------------------------------------------------------- the rules


@pytest.mark.parametrize(
    "text,kind,language",
    [
        ("Thank you for your application, we have received it.", "acknowledged", "en"),
        ("We regret to inform you that we will not be proceeding.", "rejected", "en"),
        ("We would like to invite you to an interview on 12/09/2026 at 14:00.", "interview", "en"),
        ("Please complete the technical test before Friday.", "assessment", "en"),
        ("We are pleased to offer you the position.", "offer", "en"),
        ("Nous avons bien reçu votre candidature.", "acknowledged", "fr"),
        ("Votre candidature n'a pas été retenue.", "rejected", "fr"),
        ("Nous souhaitons vous rencontrer le 3 octobre à 10h00.", "interview", "fr"),
        ("Recebemos a sua candidatura.", "acknowledged", "pt"),
        ("Lamentamos informar que não foi selecionada.", "rejected", "pt"),
        ("Gostaríamos de agendar uma entrevista para 12/09/2026.", "interview", "pt"),
        ("Vielen Dank für Ihre Bewerbung.", "acknowledged", "de"),
        ("Leider müssen wir Ihnen mitteilen, dass es nicht geklappt hat.", "rejected", "de"),
        (
            "Wir möchten Sie zu einem Vorstellungsgespräch einladen, am 12.09.2026.",
            "interview",
            "de",
        ),
        ("Bitte lösen Sie vorher unsere Programmieraufgabe.", "assessment", "de"),
        ("Wir freuen uns, Ihnen die Stelle anbieten zu können.", "offer", "de"),
        ("Gracias por tu candidatura, la hemos recibido.", "acknowledged", "es"),
        ("Lamentamos comunicarte que no continuaremos con tu candidatura.", "rejected", "es"),
        ("Nos gustaría invitarte a una entrevista el 12/09/2026 a las 14:00.", "interview", "es"),
        ("Antes tendrás que hacer una prueba técnica.", "assessment", "es"),
        ("Nos complace ofrecerte el puesto.", "offer", "es"),
        ("Grazie per la tua candidatura.", "acknowledged", "it"),
        ("Ci dispiace comunicarti che non proseguiremo.", "rejected", "it"),
        ("Vorremmo invitarti a un colloquio il 12/09/2026 alle 14:00.", "interview", "it"),
        ("Ti chiediamo di svolgere un test tecnico.", "assessment", "it"),
        ("Siamo lieti di offrirti la posizione.", "offer", "it"),
        ("Bedankt voor je sollicitatie.", "acknowledged", "nl"),
        ("Helaas gaan we niet verder met je sollicitatie.", "rejected", "nl"),
        (
            "We willen je graag uitnodigen voor een gesprek op 12-09-2026 om 14:00.",
            "interview",
            "nl",
        ),
        ("Je krijgt eerst een technische test.", "assessment", "nl"),
        ("We willen je de functie aanbieden.", "offer", "nl"),
        ("Dziękujemy za przesłanie aplikacji.", "acknowledged", "pl"),
        ("Z przykrością informujemy, że nie została zakwalifikowana.", "rejected", "pl"),
        ("Zapraszamy na rozmowę kwalifikacyjną 12.09.2026 o 14:00.", "interview", "pl"),
        ("Prosimy o rozwiązanie zadania rekrutacyjnego.", "assessment", "pl"),
        ("Mamy przyjemność zaproponować Pani stanowisko.", "offer", "pl"),
    ],
)
def test_the_rules_recognise_each_kind_in_each_language(text, kind, language):
    verdict = classifying.classify(text.lower())
    assert verdict is not None, text
    assert verdict.kind == kind, text
    assert verdict.language == language, (
        f"{text!r} was read by the {verdict.language} rules. Every file is tried in turn and "
        f"the first match wins, so a pattern that also reads as another language answers for "
        f"that language too — keep the words of one."
    )


def test_a_message_the_rules_do_not_know_is_left_alone():
    assert classifying.classify("the christmas party is on friday") is None


def test_the_most_consequential_reading_wins():
    verdict = classifying.classify(
        "thank you for your application. we would like to invite you to an interview."
    )
    assert verdict.kind == "interview"


def test_an_invitation_offers_the_dates_it_names_as_they_were_written():
    verdict = classifying.classify(
        "We would like to invite you to an interview on 12/09/2026 at 14:00, "
        "or 15 September 2026 at 09:30."
    )
    assert verdict.kind == "interview"
    assert "12/09/2026 14:00" in verdict.dates
    assert any("15 September 2026" in date for date in verdict.dates)


def test_each_kind_maps_onto_postulos_own_vocabulary():
    verdict = classifying.classify("we regret to inform you")
    assert verdict.event_kind == EventKind.REJECTION
    assert verdict.suggested_status == Status.REJECTED


def test_the_rules_cover_the_languages_this_plugin_says_they_do():
    """The fallback is documented rather than silent, and this is where it is written down.

    A language with no file here is not read wrongly — it is not read at all, and every
    message in it is skipped. That is the right failure and the wrong silence, so the set is
    named: adding a file means adding it here, and losing one fails (postulo-imap#1).
    """
    assert {code for code, _name in classifying.languages()} == {
        "de",
        "en",
        "es",
        "fr",
        "it",
        "nl",
        "pl",
        "pt",
    }


@pytest.mark.parametrize("code", sorted(code for code, _name in classifying.languages()))
def test_every_rule_list_is_whole(code):
    """A rule list is data anyone may edit, so what a usable one looks like is a test."""
    data = json.loads((classifying.RULES_DIR / f"{code}.json").read_text(encoding="utf-8"))

    assert data["language"] == code and data["name"].strip()
    assert data["dates"], "a language with no date shapes offers no dates for an invitation"
    kinds = [entry["kind"] for entry in data["kinds"]]
    assert set(kinds) == set(classifying.OUTCOMES), f"{code}: {kinds}"
    assert kinds == MOST_CONSEQUENTIAL_FIRST, (
        f"{code}: the first match wins, so the order a file lists its kinds in decides what "
        f"a message saying two things is read as. It is the same order in every language."
    )
    for pattern in data["dates"] + [p for entry in data["kinds"] for p in entry["patterns"]]:
        re.compile(pattern)


def test_a_language_with_no_rules_is_skipped_rather_than_guessed_at():
    """The documented fallback, in the one language nobody has written rules for."""
    assert classifying.classify("Köszönjük a jelentkezését.".lower()) is None


def test_a_rule_list_is_named_after_a_language_tag():
    """`pt`, or `pt-BR` if a variety needs phrases of its own: a tag, as Postulo writes one.

    The name is what a code is matched against, and what a suggestion shows, so a file
    called `PT.json` or `pt_br.json` is a second spelling of something that has one
    (postulo-imap#3).
    """
    for path in sorted(classifying.RULES_DIR.glob("*.json")):
        code = path.stem
        assert is_language_tag(code), f"{path.name} is not named after a language"
        assert language_tag(code) == code, f"{path.name}: write it {language_tag(code)}.json"
        assert json.loads(path.read_text(encoding="utf-8"))["language"] == code, path.name


@pytest.mark.parametrize("code", ["pt-BR", "PT", "pt-PT", "pt"])
def test_a_code_finds_its_rules_however_it_is_spelt(code):
    """Tags compare without regard to case, and a variety is read by its language's rules.

    Compared by exact string, a Brazilian reader's `pt-BR` matched no file: nothing was
    consulted, nothing was filed and nothing said so.
    """
    assert classifying.rules_for(code) == "pt"

    verdict = classifying.classify("Lamentamos informar que não foi selecionada.", only=(code,))
    assert verdict is not None and (verdict.kind, verdict.language) == ("rejected", "pt")
    assert classifying.dates_in("Entrevista a 12/09/2026 às 14:00.", code) == ("12/09/2026 14:00",)


def test_asking_for_one_language_leaves_the_others_out():
    assert classifying.classify("we regret to inform you", only=("pt-BR",)) is None
    assert classifying.classify("we regret to inform you", only=("pt-BR", "en-GB")).language == "en"


def test_a_language_asked_for_that_has_no_rules_reads_nothing():
    """Asking for Swedish is not asking for everything: no list, so no reading at all."""
    assert classifying.rules_for("sv") == ""
    assert classifying.classify("we regret to inform you", only=("sv",)) is None
    assert classifying.dates_in("on 12/09/2026 at 14:00", "sv") == ()


# --------------------------------------------------------------- the matching


def test_a_domain_is_reduced_to_the_organisation():
    assert registrable("careers.blackmesa.test") == "blackmesa.test"
    assert domain_of("https://www.blackmesa.test/jobs") == "blackmesa.test"
    assert domain_of("") == ""


def test_a_message_is_matched_by_its_domain(user, application):
    found = match(user, a_mail())
    assert found is not None and found.application == application and found.how == "domain"


def test_a_contact_beats_a_domain_and_a_public_domain_proves_nothing(user, application):
    Contact.objects.create(
        owner=user,
        company=application.posting.company,
        name="Eli Vance",
        email="eli@gmail.com",
    )
    found = match(user, a_mail(sender="eli@gmail.com"))
    assert found is not None and found.how == "contact"
    assert match(user, a_mail(sender="someone@gmail.com")) is None


def test_the_company_name_or_role_in_the_words_is_the_last_resort(user, application):
    found = match(user, a_mail(sender="no-reply@recruiting-suite.test", subject="Black Mesa"))
    assert found is not None and found.how == "company name"
    found = match(
        user, a_mail(sender="no-reply@recruiting-suite.test", subject="Research Engineer")
    )
    assert found is not None and found.how == "role"
    assert match(user, a_mail(sender="no-reply@recruiting-suite.test", subject="Hello")) is None


def test_two_equally_good_answers_are_no_answer(user, application):
    second = JobPosting.objects.create(
        owner=user, company=application.posting.company, title="Physicist"
    )
    Application.objects.create(owner=user, posting=second, status=Status.APPLIED)
    assert match(user, a_mail()) is None, "two applications at one company: let a person say"


def test_a_reply_to_a_matched_message_belongs_to_the_same_application(user, application):
    from postulo.applications.suggestions import suggest

    suggest(
        user,
        source="imap",
        external_id="<first@blackmesa.test>",
        application=application,
        summary="Your application",
    )
    mail = a_mail(sender="someone@unrelated.test", in_reply_to="<first@blackmesa.test>")
    found = match(user, mail)
    assert found is not None and found.application == application and found.how == "thread"


# ------------------------------------------------------------------- the sync


def test_it_is_a_sync_and_says_where_it_reads():
    plugin = ImapSync()
    assert isinstance(plugin, SyncPlugin)
    assert plugin.summary(CONFIG) == "ana@imap.example.org · Jobs"


def test_the_form_insists_on_a_folder_and_a_destination():
    plugin = ImapSync()
    assert plugin.validate(CONFIG) == {}
    assert "folder" in plugin.validate({**CONFIG, "folder": " "})
    assert "move_to" in plugin.validate({**CONFIG, "mark": "move", "move_to": ""})


def test_the_test_button_signs_in_and_counts_what_is_waiting(imap):
    imap.add(a_message())
    result = ImapSync().test(CONFIG)
    assert result.ok and "1 message " in result.message and "Jobs" in result.message
    assert "1 messages" not in result.message
    imap.add(a_message(message_id="<m2@blackmesa.test>"))
    assert "2 messages " in ImapSync().test(CONFIG).message

    result = ImapSync().test({**CONFIG, "password": "wrong"})
    assert not result.ok and "Sign-in refused" in result.message
    assert "AUTHENTICATIONFAILED" not in result.message, "what the server said is not for a form"

    result = ImapSync().test({**CONFIG, "folder": "Recruiters"})
    assert not result.ok and "no folder called" in result.message


# ------------------------------------------------------ where it is allowed to dial


def test_it_connects_to_the_address_that_was_approved_and_proves_the_name(imap):
    """The two halves of the guard, and neither is any use without the other."""
    imap.add(a_message())
    assert ImapSync().test(CONFIG).ok

    assert imap.host == APPROVED, "the name was approved once, and that answer is what is dialled"
    assert imap.ssl_context.server_name == "imap.example.org", (
        "a certificate proved against a number never matches: TLS keeps the typed name"
    )


def test_starttls_gets_the_same_context_and_so_the_same_name(imap, connection, application):
    ImapSync().sync(connection, {**connection.full_config, "security": "starttls", "port": 143})
    assert imap.started_tls and imap.ssl is False and imap.port == 143
    assert imap.host == APPROVED
    assert imap.ssl_context.server_name == "imap.example.org"


def test_a_private_address_is_refused_before_anything_is_dialled(imap, monkeypatch, settings):
    """The whole point: no socket, so nothing is learned about what is listening."""
    monkeypatch.setattr(mailbox, "approve_host", real_approve_host)
    settings.POSTULO_CONNECTIONS_ALLOW_PRIVATE = False
    FakeIMAP.instances = []

    result = ImapSync().test({**CONFIG, "host": "127.0.0.1", "port": 6379})

    assert not result.ok and "POSTULO_CONNECTIONS_ALLOW_PRIVATE" in result.message
    assert FakeIMAP.instances == [], "a refused destination is never connected to"


def test_the_operator_can_allow_a_mail_server_on_their_own_network(imap, monkeypatch, settings):
    """Which is where a self-hosted mailbox lives, and is the operator's decision to make."""
    monkeypatch.setattr(mailbox, "approve_host", real_approve_host)
    settings.POSTULO_CONNECTIONS_ALLOW_PRIVATE = True
    imap.add(a_message())

    result = ImapSync().test({**CONFIG, "host": "127.0.0.1", "port": 143})

    assert result.ok and imap.host == "127.0.0.1"


def test_what_the_server_said_goes_to_the_log_and_not_to_the_person(imap, monkeypatch, caplog):
    def refuse(*args, **kwargs):
        raise OSError("[Errno 111] Connection refused by 10.0.0.5:6379")

    monkeypatch.setattr(mailbox, "IMAPClient", refuse)
    caplog.set_level(logging.INFO, logger="postulo_imap.mailbox")

    result = ImapSync().test(CONFIG)

    assert not result.ok and "Could not reach the server" in result.message
    assert "Errno 111" not in result.message and "OSError" not in result.message
    assert "Errno 111" in caplog.text, "the operator still gets the whole of it"


def test_a_pass_suggests_what_it_recognises_and_marks_what_it_read(imap, connection, application):
    imap.add(a_message())
    imap.add(
        a_message(
            message_id="<m2@blackmesa.test>",
            subject="Interview",
            body="We would like to invite you to an interview on 12/09/2026 at 14:00.",
        )
    )
    imap.add(a_message(message_id="<m3@x.test>", sender="friend@elsewhere.test", body="Lunch?"))

    report = ImapSync().sync(connection, connection.full_config)
    assert report.pulled == 2 and report.skipped == 1

    acknowledged = Suggestion.objects.get(external_id="<m1@blackmesa.test>")
    assert acknowledged.owner == connection.owner and acknowledged.source == "imap"
    assert acknowledged.application == application
    assert acknowledged.kind == EventKind.EMAIL_RECEIVED
    assert acknowledged.suggested_status == Status.ACKNOWLEDGED
    assert acknowledged.status == SuggestionStatus.PENDING
    assert "Black Mesa Careers" in " ".join(acknowledged.context.values())
    assert str(HOW_LABELS["domain"]) in acknowledged.context.values()

    invitation = Suggestion.objects.get(external_id="<m2@blackmesa.test>")
    assert invitation.suggested_status == Status.INTERVIEWING
    assert "12/09/2026 14:00" in invitation.proposed_dates

    assert all(KEYWORD in message["flags"] for message in imap.folders["Jobs"].values())
    assert not ApplicationEvent.objects.filter(actor="imap").exists(), "nothing is written yet"

    # A second pass has nothing to do, and a third after unflagging suggests nothing new.
    assert ImapSync().sync(connection, connection.full_config).summary() == "nothing to do"
    for message in imap.folders["Jobs"].values():
        message["flags"].clear()
    report = ImapSync().sync(connection, connection.full_config)
    assert report.pulled == 0 and Suggestion.objects.count() == 2


def test_an_unmatched_message_is_filed_as_such_and_says_so(imap, connection):
    imap.add(a_message(sender="hr@unknown-company.test"))
    report = ImapSync().sync(connection, connection.full_config)
    assert report.pulled == 1 and "could not be matched" in report.summary()
    suggestion = Suggestion.objects.get()
    assert suggestion.application is None


def test_accepting_a_suggestion_writes_the_timeline_and_moves_the_application(
    imap, connection, application
):
    imap.add(a_message(subject="Sorry", body="We regret to inform you of our decision."))
    ImapSync().sync(connection, connection.full_config)
    suggestion = Suggestion.objects.get()
    assert suggestion.suggested_status == Status.REJECTED

    accept(suggestion)
    application.refresh_from_db()
    assert application.status == Status.REJECTED
    assert ApplicationEvent.objects.filter(actor="imap", to_status=Status.REJECTED).exists()


def test_moving_messages_away_instead_of_flagging_them(imap, connection, application):
    imap.add(a_message())
    ImapSync().sync(connection, {**connection.full_config, "mark": "move"})
    assert imap.folders["Jobs"] == {} and len(imap.folders["Jobs/Read"]) == 1


def test_leaving_the_mailbox_untouched(imap, connection, application):
    imap.add(a_message())
    report = ImapSync().sync(connection, {**connection.full_config, "mark": "nothing"})
    assert report.pulled == 1
    assert imap.readonly is True
    assert all(not message["flags"] for message in imap.folders["Jobs"].values())
    report = ImapSync().sync(connection, {**connection.full_config, "mark": "nothing"})
    assert report.pulled == 0 and report.skipped == 1, "read again, suggested once"


def test_old_mail_is_left_out(imap, connection, application):
    imap.add(a_message(), date=dt.date(2020, 1, 1))
    assert ImapSync().sync(connection, connection.full_config).pulled == 0


# --------------------------------------------------------------- in Postulo


def test_postulo_runs_it_from_the_scheduler(imap, connection, application):
    imap.add(a_message())
    registry.plugins("sync", refresh=True)
    assert registry.find_plugin("sync", "imap") is not None
    assert syncing.run_syncs() == (1, 0)
    connection.refresh_from_db()
    assert connection.last_summary == "1 pulled" and connection.last_error == ""


def test_a_mailbox_that_will_not_open_is_reported_on_the_connection(imap, connection):
    registry.plugins("sync", refresh=True)
    connection.config["folder"] = "Nope"
    connection.save()
    assert syncing.run_syncs() == (1, 1)
    connection.refresh_from_db()
    assert connection.last_error == "The folder “Nope” could not be opened."


def test_the_plugin_speaks_french_and_portuguese_by_itself():
    from django.utils import translation

    registry.plugins("sync", refresh=True)
    plugin = registry.find_plugin("sync", "imap")
    with translation.override("fr-FR"):
        assert str(plugin.label) == "Boîte aux lettres (IMAP)"
        assert plugin.config_fields()[5].label == "Dossier à lire"
    with translation.override("pt-PT"):
        assert str(plugin.label) == "Caixa de correio (IMAP)"
    with translation.override("en-GB"):
        assert str(plugin.label) == "Mailbox (IMAP)"


def test_the_sync_module_closes_the_connection_even_when_it_fails(imap, connection):
    connection.config["folder"] = "Nope"
    report = ImapSync().sync(connection, {**connection.full_config, "folder": "Nope"})
    assert report.error == "The folder “Nope” could not be opened."
    assert imap.logged_in is False
    assert sync_module.MARK_KEYWORD == "keyword"
