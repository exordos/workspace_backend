# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import asyncio

import pytest

from workspace.tests.integration import test_zulip_history as history_tests
from workspace.tests.integration import test_zulip_incoming as incoming_tests
from workspace.tests.integration import conftest

incoming = incoming_tests.incoming


@pytest.mark.parametrize("historical", [False, True])
@pytest.mark.parametrize("subject", ["", "✔ ", "General", "zulip", None])
def test_zulip_unnamed_topics_have_provider_name_and_preserve_identity(
    incoming, subject, historical
):
    api, db, account, _http, _url = incoming
    value = incoming_tests.event()
    if subject is None:
        value["message"].update(
            type="private",
            recipient_id=1,
            display_recipient=[{"id": 7, "full_name": "Sender"}],
        )
        value["message"].pop("stream_id")
        value["message"].pop("subject")
    else:
        value["message"]["subject"] = subject
    if historical:

        async def run():
            async with history_tests.worker(incoming, {101: value["message"]}) as (
                service,
                _calls,
            ):
                history_tests.request(db, account)
                await history_tests.drain(service, account)

        asyncio.run(run())
    else:
        incoming_tests.apply(incoming, value)
    placement = db.execute(
        "SELECT placement.topic_uuid,stream.default_topic_uuid FROM messenger_message_placements placement "
        "JOIN messenger_streams stream ON stream.project_id=placement.project_id AND stream.uuid=placement.stream_uuid "
        "WHERE placement.project_id=%s",
        (account["project_id"],),
    ).fetchone()
    topic = api.get(f"/v1/messenger/stream_topics/{placement[0]}").json()
    assert topic["name"] == ((subject or "").removeprefix("✔ ") or "zulip")
    assert topic["is_done"] == (subject == "✔ ")
    assert topic["provider_identity"].endswith(":" + (subject or ""))
    if subject == "":
        assert placement[0] == placement[1]
    # An ordinary named channel also has an empty-subject default container.
    if subject is not None:
        default = api.get(f"/v1/messenger/stream_topics/{placement[1]}").json()
        assert default["name"] == "zulip" and default["provider_identity"] == "42:"
        if subject:
            assert placement[0] != placement[1]


def test_upgrade_names_existing_zulip_defaults_without_renaming_provider_topics(
    incoming,
):
    api, db, account, *_ = incoming
    filename = next(conftest.MIGRATIONS_DIR.glob("*-a228fe.py")).name
    migration = conftest.DataMigration(filename)
    migration.rollback_migration(filename)
    try:
        first = incoming_tests.event()
        closed = incoming_tests.event(identifier=102)
        closed["message"]["subject"] = "✔ "
        incoming_tests.apply(incoming, first, closed)
        db.execute(
            "UPDATE messenger_topics SET name='General' WHERE project_id=%s AND provider_identity IN ('42:','42:✔ ')",
            (account["project_id"],),
        )
        before = db.execute(
            "SELECT uuid,provider_identity,is_done FROM messenger_topics WHERE project_id=%s ORDER BY uuid",
            (account["project_id"],),
        ).fetchall()
        placements = db.execute(
            "SELECT uuid,topic_uuid,message_uuid FROM messenger_message_placements WHERE project_id=%s ORDER BY uuid",
            (account["project_id"],),
        ).fetchall()
        outgoing = db.execute("SELECT count(*) FROM zulip_outgoing_intents").fetchone()
        migration.apply_migration(filename)
        for identifier, identity, _done in before:
            topic = api.get(f"/v1/messenger/stream_topics/{identifier}").json()
            assert topic["name"] == ("General" if identity == "42:General" else "zulip")
        assert (
            db.execute(
                "SELECT uuid,provider_identity,is_done FROM messenger_topics WHERE project_id=%s ORDER BY uuid",
                (account["project_id"],),
            ).fetchall()
            == before
        )
        assert (
            db.execute(
                "SELECT uuid,topic_uuid,message_uuid FROM messenger_message_placements WHERE project_id=%s ORDER BY uuid",
                (account["project_id"],),
            ).fetchall()
            == placements
        )
        assert (
            db.execute("SELECT count(*) FROM zulip_outgoing_intents").fetchone()
            == outgoing
        )
    finally:
        migration.apply_migration(filename)
