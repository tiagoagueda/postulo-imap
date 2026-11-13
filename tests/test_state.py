# ruff: noqa: F811
"""A pass that flags nothing remembers where it stopped (postulo-imap#34).

Through `run_sync`, as the scheduler runs a connection, so the state is kept or refused by
Postulo's rules and read back with `kept_sync_state`.
"""

from __future__ import annotations

import pytest
from postulo.plugins.testing import kept_sync_state, run_sync

from postulo_imap import mailbox
from postulo_imap.sync import ImapSync
from tests.test_imap import (  # noqa: F401
    FakeIMAP,
    a_message,
    application,
    connection,
    imap,
    user,
)

pytestmark = pytest.mark.django_db


def untouched(connection):
    connection.config = {**connection.config, "mark": "nothing"}
    connection.save()
    return connection


def fetched(imap) -> set[int]:
    return {uid for uids, _parts in imap.fetches for uid in uids}


def test_the_folder_and_the_last_uid_are_remembered(imap, connection, application):
    connection = untouched(connection)
    imap.add(a_message(message_id="<a@blackmesa.test>"))
    imap.add(a_message(message_id="<b@blackmesa.test>"))

    report = run_sync(connection)

    assert report.pulled == 2
    assert kept_sync_state(connection) == {
        "folder": "Jobs",
        "uidvalidity": imap.uidvalidity,
        "last_uid": 2,
    }


def test_the_next_pass_asks_only_for_what_came_after_and_fetches_only_that(
    imap, connection, application
):
    connection = untouched(connection)
    imap.add(a_message(message_id="<a@blackmesa.test>"))
    run_sync(connection)
    imap.fetches.clear()
    imap.searches.clear()
    new = imap.add(a_message(message_id="<b@blackmesa.test>"))

    report = run_sync(connection)

    assert report.pulled == 1
    assert imap.searches[0][:2] == ["UID", "2:*"]
    assert fetched(imap) == {new}, "the message passed over is not even surveyed again"
    assert kept_sync_state(connection)["last_uid"] == new


def test_the_highest_uid_that_n_star_always_returns_is_dropped(imap, connection, application):
    connection = untouched(connection)
    imap.add(a_message(message_id="<a@blackmesa.test>"))
    run_sync(connection)
    imap.fetches.clear()

    report = run_sync(connection)  # `2:*` answers with uid 1, the highest, below 2

    assert report.pulled == 0 and report.skipped == 0
    assert not imap.fetches


def test_a_changed_uidvalidity_reads_the_look_back_as_before(imap, connection, application):
    connection = untouched(connection)
    imap.add(a_message(message_id="<a@blackmesa.test>"))
    run_sync(connection)
    imap.fetches.clear()
    imap.searches.clear()
    imap.uidvalidity += 1

    report = run_sync(connection)

    assert imap.searches[0][0] == "SINCE"
    assert report.skipped == 1, "read again, suggested once"
    assert kept_sync_state(connection)["uidvalidity"] == imap.uidvalidity


def test_another_folder_does_not_borrow_the_place(imap, connection, application):
    connection = untouched(connection)
    imap.folders["Recruiters"] = {}
    imap.add(a_message(message_id="<a@blackmesa.test>"))
    run_sync(
        connection, state={"folder": "Recruiters", "uidvalidity": imap.uidvalidity, "last_uid": 9}
    )

    assert imap.searches[0][0] == "SINCE"


def test_a_pass_cut_short_resumes_where_it_stopped(imap, connection, application, monkeypatch):
    connection = untouched(connection)
    monkeypatch.setattr(mailbox, "FETCH_BATCH", 2)
    for number in range(4):
        imap.add(a_message(message_id=f"<m{number}@blackmesa.test>"))
    real = imap.fetch
    calls = {"count": 0}

    def fail_the_second_batch(uids, parts):
        calls["count"] += 1
        if calls["count"] > 2:  # a batch is a survey and a read
            raise ValueError("[UNAVAILABLE] gone")
        return real(uids, parts)

    monkeypatch.setattr(imap, "fetch", fail_the_second_batch)

    report = run_sync(connection)

    assert report.pulled == 2
    assert kept_sync_state(connection)["last_uid"] == 2
    monkeypatch.setattr(imap, "fetch", real)
    imap.fetches.clear()
    assert run_sync(connection).pulled == 2
    assert fetched(imap) == {3, 4}


def test_a_pass_that_read_nothing_keeps_the_place_it_had(imap, connection, application):
    connection = untouched(connection)
    run_sync(connection, state={"folder": "Jobs", "uidvalidity": imap.uidvalidity, "last_uid": 7})

    assert kept_sync_state(connection)["last_uid"] == 7


def test_the_keyword_mode_keeps_nothing(imap, connection, application):
    imap.add(a_message())

    run_sync(connection)

    assert kept_sync_state(connection) == {}
    assert all(search[0] != "UID" for search in imap.searches)


def test_moving_keeps_nothing(imap, connection, application):
    connection.config = {**connection.config, "mark": "move"}
    connection.save()
    imap.add(a_message())

    run_sync(connection)

    assert kept_sync_state(connection) == {}


def test_a_folder_that_keeps_no_keywords_is_remembered_too(imap, connection, application):
    imap.permanent_flags = (b"\\Seen",)
    imap.add(a_message())

    run_sync(connection)

    assert kept_sync_state(connection)["last_uid"] == 1


def test_a_server_that_gives_no_uidvalidity_keeps_nothing(imap, connection, application):
    connection = untouched(connection)
    imap.uidvalidity = None
    imap.add(a_message())

    run_sync(connection)

    assert kept_sync_state(connection) == {}


def test_editing_the_connection_drops_the_place(imap, connection, application):
    connection = untouched(connection)
    imap.add(a_message())
    run_sync(connection)
    assert kept_sync_state(connection)

    connection.config = {**connection.config, "days": 90}
    connection.save()

    assert kept_sync_state(connection) == {}


def test_the_plugin_is_what_ran(imap, connection):
    assert connection.plugin_instance.__class__ is ImapSync
