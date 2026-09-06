"""The plugin, against a mailbox that lives in memory, and inside a real Postulo."""

from __future__ import annotations

import datetime as dt
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
from postulo.plugins.base import SyncPlugin
from postulo.plugins.models import Connection

from postulo_imap import classifying, mailbox
from postulo_imap import sync as sync_module
from postulo_imap.mailbox import KEYWORD, Mail, parse, plain_text
from postulo_imap.matching import domain_of, match, registrable
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

    def __init__(self, host, port=993, ssl=True, timeout=None):
        self.host, self.port, self.ssl = host, port, ssl
        self.folders: dict[str, dict[int, dict]] = {"Jobs": {}, "INBOX": {}}
        self.selected = ""
        self.readonly = False
        self.logged_in = False
        self.started_tls = False
        self.credentials: tuple[str, str] | None = None
        self.next_uid = 1
        FakeIMAP.instances.append(self)

    # -- connection
    def starttls(self):
        self.started_tls = True

    def login(self, username, password):
        if (username, password) != ("ana", "app-pw"):
            raise ValueError("[AUTHENTICATIONFAILED] Invalid credentials")
        self.logged_in = True

    def logout(self):
        self.logged_in = False

    # -- folders
    def folder_exists(self, name):
        return name in self.folders

    def create_folder(self, name):
        self.folders.setdefault(name, {})

    def select_folder(self, name, readonly=False):
        if name not in self.folders:
            raise ValueError(f"[NONEXISTENT] Unknown Mailbox: {name}")
        self.selected, self.readonly = name, readonly

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
        return {uid: {b"BODY[]": self.folders[self.selected][uid]["raw"]} for uid in uids}

    def add_flags(self, uids, flags):
        if self.readonly:
            raise ValueError("[READ-ONLY] Mailbox is read-only")
        for uid in uids:
            self.folders[self.selected][uid]["flags"].update(flags)

    def move(self, uids, destination):
        if self.readonly:
            raise ValueError("[READ-ONLY] Mailbox is read-only")
        self.create_folder(destination)
        for uid in uids:
            self.folders[destination][uid] = self.folders[self.selected].pop(uid)


@pytest.fixture
def imap(monkeypatch):
    FakeIMAP.instances = []
    monkeypatch.setattr(mailbox, "IMAPClient", FakeIMAP)
    box = FakeIMAP("imap.example.org")
    FakeIMAP.instances = []

    def factory(host, port=993, ssl=True, timeout=None):
        box.host, box.port, box.ssl = host, port, ssl
        FakeIMAP.instances.append(box)
        return box

    monkeypatch.setattr(mailbox, "IMAPClient", factory)
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
    ],
)
def test_the_rules_recognise_each_kind_in_each_language(text, kind, language):
    verdict = classifying.classify(text.lower())
    assert verdict is not None, text
    assert verdict.kind == kind and verdict.language == language


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
    assert {code for code, _name in classifying.languages()} == {"en", "fr", "pt"}


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
    assert result.ok and "1 messages" in result.message and "Jobs" in result.message

    result = ImapSync().test({**CONFIG, "password": "wrong"})
    assert not result.ok and "AUTHENTICATIONFAILED" in result.message

    result = ImapSync().test({**CONFIG, "folder": "Recruiters"})
    assert not result.ok and "no folder called" in result.message


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
    assert "domain" in " ".join(acknowledged.context.values())

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


def test_starttls_is_asked_for_when_chosen(imap, connection, application):
    ImapSync().sync(connection, {**connection.full_config, "security": "starttls", "port": 143})
    assert imap.started_tls and imap.ssl is False and imap.port == 143


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
    assert "could not be opened" in connection.last_error


def test_the_plugin_speaks_french_and_portuguese_by_itself():
    from django.utils import translation

    registry.plugins("sync", refresh=True)
    plugin = registry.find_plugin("sync", "imap")
    with translation.override("fr-fr"):
        assert str(plugin.label) == "Boîte aux lettres (IMAP)"
        assert plugin.config_fields()[5].label == "Dossier à lire"
    with translation.override("pt-pt"):
        assert str(plugin.label) == "Caixa de correio (IMAP)"
    with translation.override("en-gb"):
        assert str(plugin.label) == "Mailbox (IMAP)"


def test_the_sync_module_closes_the_connection_even_when_it_fails(imap, connection):
    connection.config["folder"] = "Nope"
    with pytest.raises(Exception, match="could not be opened"):
        ImapSync().sync(connection, {**connection.full_config, "folder": "Nope"})
    assert imap.logged_in is False
    assert sync_module.MARK_KEYWORD == "keyword"
