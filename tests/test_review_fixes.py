# ruff: noqa: F811, E501
"""What the reviews of 2026-09-29 and 2026-10-16 found, as the sentence or input it came with.

The fixtures and the in-memory mailbox are test_imap.py's; these are the cases that file did
not have: messages that are malformed, enormous or worded the way real mail is, and a server
that refuses what a pass asks of it.
"""

from __future__ import annotations

import base64
import logging
import time

import pytest
from django.db import OperationalError
from django.db import connection as db_connection
from django.test.utils import CaptureQueriesContext
from django.utils import translation
from postulo.applications.models import Status, Suggestion
from postulo.jobs.models import Company, JobPosting

from postulo_imap import classifying, mailbox
from postulo_imap import sync as sync_module
from postulo_imap.mailbox import KEYWORD, Mail, parse
from postulo_imap.matching import HOW_LABELS, _mentions, domain_of, match, registrable
from postulo_imap.sync import ImapSync
from tests.test_imap import (  # noqa: F401
    CONFIG,
    FakeIMAP,
    a_mail,
    a_message,
    application,
    connection,
    imap,
    user,
)

pytestmark = pytest.mark.django_db

MEGABYTE = 1_000_000


def raw_message(headers: str, body: str = "", *, encoding="utf-8", eol="\r\n") -> bytes:
    """A message exactly as the bytes arrive, which `EmailMessage` would tidy."""
    lines = headers.strip().splitlines()
    return (eol.join(lines) + eol + eol + body).encode(encoding)


# ----------------------------------------------------- #8 a message that cannot be read


def test_a_date_with_an_8_bit_byte_is_read_without_a_date():
    raw = raw_message(
        "From: Ana <ana@x.test>\nSubject: Hi\nMessage-ID: <d1@x.test>\n"
        "Date: Mon, 01 Jan 2026 10:00:00 +0000 \xe9",
        "body",
        encoding="latin-1",
    )
    mail = parse(1, raw)
    assert isinstance(mail, Mail) and mail.subject == "Hi"
    assert mail.date is None or mail.date.year == 2026


@pytest.mark.parametrize("charset", ["idna", "undefined", "no-such-charset"])
def test_a_part_whose_charset_is_not_one_is_read_as_utf8(charset):
    raw = raw_message(
        f"From: a@x.test\nSubject: s\nContent-Type: text/plain; charset={charset}\n"
        "Content-Transfer-Encoding: 8bit",
        "Thank you for your application.",
    )
    assert "Thank you for your application" in parse(1, raw).text


def test_no_string_of_a_mail_carries_a_nul_character():
    def word(data: bytes) -> str:
        return "=?utf-8?b?" + base64.b64encode(data).decode() + "?="

    body = base64.b64encode(b"bo\x00dy thank you for your application").decode()
    raw = raw_message(
        f"From: {word(b'An\x00a')} <ana@x.test>\nSubject: {word(b'x\x00y')}\n"
        f"Message-ID: <n\x00@x.test>\nContent-Type: text/plain; charset=utf-8\n"
        "Content-Transfer-Encoding: base64",
        body,
    )
    mail = parse(1, raw)
    assert (mail.subject, mail.sender_name) == ("xy", "Ana")
    for value in (mail.subject, mail.sender_name, mail.text, mail.sender, mail.message_id):
        assert "\x00" not in value
    assert "body thank you" in mail.text


def test_a_message_nothing_can_read_does_not_fail_the_pass(
    imap, connection, application, monkeypatch
):
    imap.add(a_message())
    bad = imap.add(a_message(message_id="<bad@blackmesa.test>"))
    real = mailbox.parse

    def parse_or_fail(uid, raw):
        if uid == bad:
            raise AttributeError("'Header' object has no attribute 'split'")
        return real(uid, raw)

    monkeypatch.setattr(mailbox, "parse", parse_or_fail)

    report = ImapSync().sync(connection, connection.full_config)

    assert report.pulled == 1 and not report.error
    assert Suggestion.objects.filter(external_id="<m1@blackmesa.test>").exists()
    assert all(KEYWORD in message["flags"] for message in imap.folders["Jobs"].values())
    assert any("1 message could not be read" in note for note in report.notes)


def test_a_message_whose_matching_raises_is_skipped_and_flagged(
    imap, connection, application, monkeypatch
):
    for name in "abc":
        imap.add(a_message(message_id=f"<{name}@blackmesa.test>"))
    real = sync_module.match

    def match_or_fail(owner, mail, **kwargs):
        if mail.message_id == "<b@blackmesa.test>":
            raise OperationalError("too many SQL variables")
        return real(owner, mail, **kwargs)

    monkeypatch.setattr(sync_module, "match", match_or_fail)

    report = ImapSync().sync(connection, connection.full_config)

    assert report.pulled == 2 and report.skipped == 1 and not report.error
    assert sorted(Suggestion.objects.values_list("external_id", flat=True)) == [
        "<a@blackmesa.test>",
        "<c@blackmesa.test>",
    ]
    assert all(KEYWORD in message["flags"] for message in imap.folders["Jobs"].values())
    assert any("1 message could not be read" in note for note in report.notes)


# --------------------------------------------- #11 mail with no Message-ID is keyed on itself


def test_mail_without_a_message_id_is_known_by_what_it_says_not_by_its_uid():
    raw = a_message(message_id=None)
    assert parse(1, raw).message_id == parse(7, raw).message_id
    assert parse(1, raw).message_id.startswith("sha256-")
    assert parse(1, raw).message_id != parse(1, a_message(message_id=None, body="Other")).message_id


def test_two_messages_with_no_id_and_the_same_uid_in_two_folders_are_both_filed(
    imap, connection, application
):
    imap.folders["Other"] = {}
    imap.add(a_message(message_id=None), folder="Jobs")
    imap.next_uid = 1
    imap.add(
        a_message(message_id=None, subject="Sorry", body="We regret to inform you."),
        folder="Other",
    )

    first = ImapSync().sync(connection, connection.full_config)
    second = ImapSync().sync(connection, {**connection.full_config, "folder": "Other"})

    assert (first.pulled, second.pulled) == (1, 1)
    assert Suggestion.objects.count() == 2


# ------------------------------------------------ #21 headers sent as raw bytes


@pytest.mark.parametrize("encoding", ["utf-8", "latin-1"])
def test_a_from_and_subject_sent_as_raw_bytes_are_read(encoding):
    raw = raw_message(
        "From: Müller GmbH <jobs@mueller.test>\nSubject: Ihre Bewerbung bei Müller\n"
        "Message-ID: <u@mueller.test>",
        "Danke.",
        encoding=encoding,
    )
    mail = parse(1, raw)
    assert mail.sender == "jobs@mueller.test" and mail.sender_name == "Müller GmbH"
    assert mail.subject == "Ihre Bewerbung bei Müller"
    for value in (mail.sender, mail.sender_name, mail.subject, mail.text, mail.message_id):
        assert "�" not in value and not any("\ud800" <= char <= "\udfff" for char in value)


# ------------------------------------------------ #15 time and the number of identifiers


@pytest.mark.parametrize(
    "html",
    ["<script>" * (MEGABYTE // 8), "<" * MEGABYTE, " " * MEGABYTE, "<p>" * (MEGABYTE // 3)],
    ids=["script", "lt", "whitespace", "paragraphs"],
)
def test_html_is_stripped_in_linear_time(html):
    started = time.perf_counter()
    mailbox._strip_html(html)
    assert time.perf_counter() - started < 1


def test_a_references_header_of_a_hundred_kilobytes_is_parsed_in_a_second():
    started = time.perf_counter()
    mail = parse(1, raw_message("From: a@x.test\nReferences: " + "<" * 100_000, "hi"))
    assert time.perf_counter() - started < 1 and mail.references == []


def test_thousands_of_references_are_cut_to_the_parent_and_the_last_few(
    imap, connection, application
):
    ids = " ".join(f"<r{n}@x.test>" for n in range(5000))
    raw = raw_message(
        f"From: careers@blackmesa.test\nSubject: Re: hello\nMessage-ID: <deep@x.test>\n"
        f"In-Reply-To: <r4999@x.test>\nReferences: {ids}",
        "Thank you for your application. We have received it.",
    )
    mail = parse(1, raw)
    assert len(mail.references) <= mailbox.MAX_REFERENCES + 1
    assert mail.references[-1] == "<r4999@x.test>" and "<r0@x.test>" not in mail.references

    imap.add(raw)
    imap.add(a_message())
    with CaptureQueriesContext(db_connection) as queries:
        report = ImapSync().sync(connection, connection.full_config)
    assert report.pulled == 2 and not report.error
    thread = [q["sql"] for q in queries if "external_id" in q["sql"] and "@x.test" in q["sql"]]
    assert thread and all(sql.count("@x.test") <= 9 for sql in thread)
    assert all(KEYWORD in message["flags"] for message in imap.folders["Jobs"].values())


# ------------------------------------------- #20 what a pass transfers


def test_a_pass_never_asks_for_more_than_a_batch_at_once(imap, connection):
    for n in range(120):
        imap.add(a_message(message_id=f"<n{n}@x.test>", sender="hr@unknown.test"))

    report = ImapSync().sync(connection, connection.full_config)

    assert report.pulled == 120
    assert max(len(uids) for uids, _parts in imap.fetches) <= mailbox.FETCH_BATCH


def test_a_big_message_is_fetched_as_its_headers_and_the_start_of_its_text(
    imap, connection, application
):
    uid = imap.add(a_message(body="Thank you for your application. " + "x " * 200_000))
    imap.sizes[uid] = 5_000_000

    report = ImapSync().sync(connection, connection.full_config)

    assert report.pulled == 1
    assert not any("BODY.PEEK[]" in parts for _uids, parts in imap.fetches)
    asked = [parts for uids, parts in imap.fetches if uid in uids and "BODY.PEEK[HEADER]" in parts]
    assert asked and f"BODY.PEEK[TEXT]<0.{mailbox.PARTIAL_TEXT}>" in asked[0]
    assert "Thank you for your application" in Suggestion.objects.get().body


def test_a_pass_in_untouched_mode_downloads_no_message_it_already_filed(
    imap, connection, application
):
    imap.add(a_message())
    config = {**connection.full_config, "mark": "nothing"}
    assert ImapSync().sync(connection, config).pulled == 1
    imap.fetches.clear()

    report = ImapSync().sync(connection, config)

    assert report.pulled == 0 and report.skipped == 1
    assert imap.fetches, "the sizes and identifiers are still asked for"
    assert not any("BODY.PEEK[]" in parts for _uids, parts in imap.fetches)


# ---------------------------------------- #22 what a refusal does to the pass


def test_a_refused_move_keeps_the_report_and_keeps_the_servers_words_out_of_it(
    imap, connection, application, caplog
):
    imap.add(a_message())
    imap.fail["move"] = ValueError("[CANNOT] the server's own words")
    caplog.set_level(logging.INFO, logger="postulo_imap.mailbox")

    report = ImapSync().sync(connection, {**connection.full_config, "mark": "move"})

    assert report.pulled == 1 and not report.error
    assert any("could not be moved" in note for note in report.notes)
    assert "the server's own words" not in " ".join(report.notes)
    assert "the server's own words" in caplog.text


def test_a_refused_flag_keeps_the_report_and_the_note_about_unmatched_mail(
    imap, connection, caplog
):
    imap.add(a_message(sender="hr@unknown-company.test"))
    imap.fail["add_flags"] = ValueError("[READ-ONLY] the server's own words")
    caplog.set_level(logging.INFO, logger="postulo_imap.mailbox")

    report = ImapSync().sync(connection, connection.full_config)

    assert report.pulled == 1 and not report.error
    assert any("could not be flagged" in note for note in report.notes)
    assert any("could not be matched" in note for note in report.notes)
    assert "the server's own words" not in " ".join(report.notes)
    assert "the server's own words" in caplog.text


def test_a_server_without_move_copies_and_expunges_those_messages(imap, connection, application):
    imap.capabilities = {"UIDPLUS"}
    uid = imap.add(a_message())

    report = ImapSync().sync(connection, {**connection.full_config, "mark": "move"})

    assert report.pulled == 1 and not report.notes
    assert imap.copied == [([uid], "Jobs/Read")]
    assert imap.folders["Jobs"] == {} and len(imap.folders["Jobs/Read"]) == 1


def test_a_server_that_cannot_move_at_all_says_so_in_test_and_in_the_pass(imap, connection):
    imap.capabilities = set()
    imap.add(a_message())
    config = {**connection.full_config, "mark": "move"}

    result = ImapSync().test(config)
    assert not result.ok and "cannot move messages" in result.message

    report = ImapSync().sync(connection, config)
    assert report.pulled == 1 and any("cannot move them" in note for note in report.notes)


def test_a_folder_that_keeps_no_keywords_is_read_as_untouched_and_says_so(
    imap, connection, application
):
    imap.permanent_flags = (b"\\Seen", b"\\Deleted")
    imap.add(a_message())

    first = ImapSync().sync(connection, connection.full_config)
    second = ImapSync().sync(connection, connection.full_config)

    assert first.pulled == 1 and any("does not keep flags" in note for note in first.notes)
    assert all(not message["flags"] for message in imap.folders["Jobs"].values())
    assert second.pulled == 0 and second.skipped == 1


def test_test_survives_a_folder_check_the_server_refuses(imap):
    imap.fail["folder_exists"] = RuntimeError("secret server text")

    result = ImapSync().test(CONFIG)

    assert not result.ok and "secret server text" not in result.message


def test_a_folder_that_will_not_open_is_reported_as_a_sentence_and_hangs_up(imap, connection):
    report = ImapSync().sync(connection, {**connection.full_config, "folder": "Nope"})
    assert report.error == "The folder “Nope” could not be opened."
    assert imap.logged_in is False


# ------------------------------------------------ #12 #13 the form


@pytest.mark.parametrize("folder", ["INBOX", "inbox", " Inbox "])
def test_the_inbox_is_refused_in_the_form_the_test_and_the_pass(imap, connection, folder):
    plugin = ImapSync()
    assert "folder" in plugin.validate({**CONFIG, "folder": folder})

    result = plugin.test({**CONFIG, "folder": folder})
    assert not result.ok and "never reads the inbox" in result.message
    report = plugin.sync(connection, {**connection.full_config, "folder": folder})
    assert "never reads the inbox" in report.error
    assert FakeIMAP.instances == [], "refused before anything was dialled"


def test_moving_into_the_folder_being_read_is_refused():
    plugin = ImapSync()
    assert "move_to" in plugin.validate({**CONFIG, "mark": "move", "move_to": "Jobs"})
    assert "move_to" in plugin.validate({**CONFIG, "mark": "move", "move_to": "jobs"})
    assert plugin.validate({**CONFIG, "mark": "keyword", "move_to": "Jobs"}) == {}


@pytest.mark.parametrize("port", ["", None])
def test_starttls_with_the_port_left_empty_dials_143(imap, connection, port):
    ImapSync().sync(connection, {**connection.full_config, "security": "starttls", "port": port})
    assert imap.port == 143 and imap.started_tls


def test_tls_with_the_port_left_empty_dials_993(imap, connection):
    ImapSync().sync(connection, {**connection.full_config, "port": None})
    assert imap.port == 993 and imap.ssl is True


def test_the_port_and_the_look_back_are_bounded():
    plugin = ImapSync()
    for days in (-30, 0, 10**12, "many"):
        assert "days" in plugin.validate({**CONFIG, "days": days}), days
    for port in (0, 70000, -1, "http"):
        assert "port" in plugin.validate({**CONFIG, "port": port}), port
    assert plugin.validate({**CONFIG, "port": "", "days": None}) == {}
    assert plugin.validate({**CONFIG, "port": 143, "days": 3650}) == {}


def test_a_saved_look_back_that_is_out_of_range_does_not_break_the_pass(imap, connection):
    for days in (-30, 10**12, 800000):
        report = ImapSync().sync(connection, {**connection.full_config, "days": days})
        assert not report.error


# ------------------------------------------------ #10 plural forms


def test_one_message_that_could_not_be_matched_is_said_in_the_singular(imap, connection):
    imap.add(a_message(sender="hr@unknown-company.test"))
    report = ImapSync().sync(connection, connection.full_config)
    assert report.notes == ["1 could not be matched to an application; attach it by hand."]

    imap.add(a_message(message_id="<m2@x.test>", sender="hr@unknown-company.test"))
    imap.add(a_message(message_id="<m3@x.test>", sender="hr@unknown-company.test"))
    report = ImapSync().sync(connection, connection.full_config)
    assert report.notes == ["2 could not be matched to an application; attach them by hand."]


# ------------------------------------------------ #9 labels


def test_the_stored_labels_are_words_and_the_identifiers_stay_identifiers(
    imap, connection, application
):
    imap.add(a_message(sender="careers@blackmesa.test"))
    imap.add(
        a_message(
            message_id="<m2@x.test>",
            sender="hr@unknown-company.test",
            subject="Sorry",
            body="We regret to inform you of our decision.",
        )
    )

    with translation.override("pt-PT"):
        ImapSync().sync(connection, connection.full_config)
        assert match(connection.owner, a_mail()).how == "domain"
        assert classifying.classify("we regret to inform you").kind == "rejected"

    matched = Suggestion.objects.get(external_id="<m1@blackmesa.test>")
    unmatched = Suggestion.objects.get(external_id="<m2@x.test>")
    with translation.override("pt-PT"):
        domain = str(HOW_LABELS["domain"])
        rejection = str(classifying.KIND_LABELS["rejected"])
    values = [*matched.context.values(), *unmatched.context.values()]
    assert domain in values and f"{rejection} (en): we regret to inform" in values
    assert not {"domain", "thread", "contact", "role", "company name"} & set(values)
    assert not any(value.startswith(("acknowledged", "rejected")) for value in values)


# ------------------------------------------------ #16 #18 the rules


NOT_AN_OFFER = [
    ("Gracias por tu candidatura a la oferta de empleo Analista de datos. La hemos recibido.", "acknowledged", "es"),
    ("Lamentamos comunicarte que no continuaremos con tu candidatura a la oferta de empleo X.", "rejected", "es"),
    ("Grazie per la tua candidatura all'offerta di lavoro Analista.", "acknowledged", "it"),
    ("Ci dispiace comunicarti che non proseguiremo per l'offerta di lavoro Analista.", "rejected", "it"),
    ("Dziękujemy za aplikację. Oferta pracy: Analityk danych.", "acknowledged", "pl"),
    ("Recebemos a sua candidatura à oferta de emprego Analista de Dados.", "acknowledged", "pt"),
    ("Lamentamos informar que a sua candidatura à oferta de emprego Analista não foi selecionada.", "rejected", "pt"),
    ("We regret to inform you that you were not selected for the job offer Data Analyst.", "rejected", "en"),
    ("Nous avons le plaisir de vous proposer un entretien le 3 octobre", "interview", "fr"),
    ("Temos o prazer de lhe propor uma entrevista no dia 3 de outubro.", "interview", "pt"),
    ("Wir freuen uns, Ihnen einen Gesprächstermin anbieten zu können.", "interview", "de"),
    ("We are pleased to offer you an interview on Tuesday.", "interview", "en"),
    ("Nos complace ofrecerte una entrevista el martes.", "interview", "es"),
    ("Siamo lieti di offrirti un colloquio martedì.", "interview", "it"),
    ("Mamy przyjemność zaproponować Panu rozmowę we wtorek.", "interview", "pl"),
]  # fmt: skip


@pytest.mark.parametrize(("text", "kind", "language"), NOT_AN_OFFER)
def test_a_vacancy_or_a_pleased_to_offer_is_not_an_offer(text, kind, language):
    verdict = classifying.classify(text)
    assert verdict is not None and (verdict.kind, verdict.language) == (kind, language), text


def test_a_recruiters_first_approach_is_not_read_as_an_offer():
    verdict = classifying.classify("Propozycja współpracy")
    assert verdict is None or verdict.kind != "offer"


REJECTIONS = [
    "Lamentamos informar que a sua candidatura não foi selecionada.",
    "We regret to inform you that we will not be proceeding.",
    "Lamentamos comunicarte que no continuaremos con tu candidatura.",
    "Votre candidature n'a pas été retenue.",
    "Leider müssen wir Ihnen mitteilen, dass es nicht geklappt hat.",
    "Ci dispiace comunicarti che non proseguiremo.",
    "Helaas gaan we niet verder met je sollicitatie.",
    "Z przykrością informujemy, że nie została zakwalifikowana.",
]
OFFERS = [
    "Temos o prazer de lhe oferecer a posição.",
    "We are pleased to offer you the position.",
    "Nos complace ofrecerte el puesto.",
    "Nous avons le plaisir de vous proposer le poste.",
    "Wir freuen uns, Ihnen die Stelle anbieten zu können.",
    "Siamo lieti di offrirti la posizione.",
    "We willen je de functie aanbieden.",
    "Mamy przyjemność zaproponować Pani stanowisko.",
]
OTHER_LANGUAGES_TERMS = [
    "Após a entrevista presencial,",
    "Depois da entrevista telefónica,",
    "Após a entrevista por vídeo,",
    "Following the online-test",
    "After the assessment center,",
    "Nach dem Vorstellungsgespräch,",
    "Dopo il colloquio telefonico,",
]


@pytest.mark.parametrize("term", OTHER_LANGUAGES_TERMS)
@pytest.mark.parametrize("sentence", REJECTIONS)
def test_a_rejection_that_mentions_an_earlier_interview_stays_a_rejection(term, sentence):
    assert classifying.classify(f"{term} {sentence}").kind == "rejected"


@pytest.mark.parametrize("term", OTHER_LANGUAGES_TERMS)
@pytest.mark.parametrize("sentence", OFFERS)
def test_an_offer_that_mentions_an_earlier_interview_stays_an_offer(term, sentence):
    assert classifying.classify(f"{term} {sentence}").kind == "offer"


@pytest.mark.parametrize(
    ("text", "language"),
    [
        ("Após a entrevista presencial, lamentamos informar que a sua candidatura não foi selecionada.", "pt"),
        ("Unfortunately, after the assessment center, we will not be proceeding with your application.", "en"),
    ],
)  # fmt: skip
def test_the_language_named_is_the_one_that_matched_the_kind_that_won(text, language):
    verdict = classifying.classify(text)
    assert (verdict.kind, verdict.language) == ("rejected", language)


# ------------------------------------------------ #17 how a phrase is hidden


def html_message(body: str, *, head: str = "") -> bytes:
    return raw_message(
        "From: careers@blackmesa.test\nSubject: Application\nMessage-ID: <h@x.test>\n"
        "Content-Type: text/html; charset=utf-8",
        f"<html><head>{head}</head><body>{body}</body></html>",
    )


HIDDEN = [
    ("wrapped LF", raw_message("Subject: x\nContent-Type: text/plain", "We regret to\ninform you that we will not proceed.", eol="\n"), "rejected"),
    ("wrapped CRLF", raw_message("Subject: x\nContent-Type: text/plain", "We regret to\r\ninform you that we will not proceed."), "rejected"),
    ("inline bold", html_message("<p>We <b>regret</b> to inform you</p>"), "rejected"),
    ("&nbsp;", html_message("We&nbsp;regret&nbsp;to&nbsp;inform you"), "rejected"),
    ("U+00A0", raw_message("Subject: x\nContent-Type: text/plain; charset=utf-8", "We regret to inform you"), "rejected"),
    ("entities", html_message("Votre candidature n&#39;a pas &eacute;t&eacute; retenue."), "rejected"),
    ("U+2019", raw_message("Subject: x\nContent-Type: text/plain; charset=utf-8", "Votre candidature n’a pas été retenue."), "rejected"),
    ("two divs", html_message("<div>Thank you for</div><div>your application</div>"), "acknowledged"),
    ("a title", html_message("<p>Thank you for your application.</p>", head="<title>Offer of employment newsletter</title>"), "acknowledged"),
]  # fmt: skip


@pytest.mark.parametrize(("name", "raw", "kind"), HIDDEN, ids=[case[0] for case in HIDDEN])
def test_a_phrase_is_found_however_the_message_hid_it(name, raw, kind):
    verdict = classifying.classify(parse(1, raw).haystack)
    assert verdict is not None and verdict.kind == kind, (name, parse(1, raw).haystack)


def test_a_company_name_split_by_a_line_break_is_found(user, application):
    mail = a_mail(
        sender="no-reply@recruiting-suite.test", subject="Hello", body="Hi from Black\nMesa  HR"
    )
    found = match(user, mail)
    assert found is not None and found.how == "company name"
    assert _mentions("we at black\nmesa are", "Black Mesa")
    assert _mentions("we at black  mesa are", "Black Mesa")


def test_the_excerpt_keeps_its_line_breaks():
    mail = parse(
        1,
        raw_message("Subject: x\nContent-Type: text/plain", "First line\r\nSecond line", eol="\n"),
    )
    assert mail.text == "First line\nSecond line"


# ------------------------------------------------ #19 domains


def test_a_domain_is_reduced_to_the_organisation_under_a_public_suffix():
    assert registrable("careers.acme.co.uk") == "acme.co.uk"
    assert registrable("vagas.acme.com.br") == "acme.com.br"
    assert registrable("www.acme.com.pl") == "acme.com.pl"
    assert registrable("jobs.acme.com") == "acme.com"
    assert registrable("co.uk") == "" and registrable("acme.github.io") == "acme.github.io"
    assert registrable("github.io") == ""
    assert domain_of("https://www.acme.co.uk/careers") == "acme.co.uk"
    assert domain_of("https://münchen.example") == domain_of("https://xn--mnchen-3ya.example")


@pytest.fixture
def uk_application(user):
    company = Company.objects.create(owner=user, name="Acme", website="https://www.acme.co.uk")
    posting = JobPosting.objects.create(owner=user, company=company, title="Analyst")
    from django.utils import timezone
    from postulo.applications.models import Application
    from postulo.applications.services import change_status

    application = Application.objects.create(owner=user, posting=posting, status=Status.DRAFT)
    change_status(application, Status.APPLIED, occurred_at=timezone.now())
    application.refresh_from_db()
    return application


def test_another_organisation_under_the_same_suffix_is_not_a_domain_match(user, uk_application):
    assert match(user, a_mail(sender="recruiter@otherfirm.co.uk", subject="Hello")) is None
    assert match(user, a_mail(sender="jane.doe@yahoo.co.uk", subject="Hello")) is None
    found = match(user, a_mail(sender="hr@careers.acme.co.uk"))
    assert found is not None and found.application == uk_application and found.how == "domain"


def test_with_two_applications_at_different_co_uk_companies_the_senders_wins(user, uk_application):
    other = Company.objects.create(owner=user, name="Zed", website="https://www.zed.co.uk")
    posting = JobPosting.objects.create(owner=user, company=other, title="Clerk")
    from postulo.applications.models import Application

    Application.objects.create(owner=user, posting=posting, status=Status.APPLIED)

    found = match(user, a_mail(sender="hr@acme.co.uk", subject="Hello"))

    assert found is not None and found.application == uk_application and found.how == "domain"
