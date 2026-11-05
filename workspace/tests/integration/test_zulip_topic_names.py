# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import asyncio

import pytest

from workspace.tests.integration import test_zulip_history as history_tests
from workspace.tests.integration import test_zulip_incoming as incoming_tests
from workspace.tests.integration import conftest

incoming = incoming_tests.incoming


def message(incoming, conversation, subject):
    value = incoming_tests.event()
    if subject is None:
        people = [
            {"id": 7, "full_name": "Sender"},
        ]
        if conversation != "self":
            people.append({"id": 8, "full_name": "Peer"})
        if conversation == "group":
            people.append({"id": 9, "full_name": "Third"})
        incoming_tests.apply(
            incoming,
            *[
                {
                    "type": "realm_user",
                    "op": "add",
                    "person": {
                        "user_id": person["id"],
                        "full_name": person["full_name"],
                        "email": f"user{person['id']}@example.com",
                    },
                }
                for person in people[1:]
            ],
        )
        value["message"].update(
            type="private",
            recipient_id=1,
            display_recipient=people,
        )
        value["message"].pop("stream_id")
        value["message"].pop("subject")
    else:
        incoming_tests.apply(
            incoming,
            {
                "type": "subscription",
                "op": "add",
                "subscriptions": [
                    {
                        "stream_id": 42,
                        "name": "Channel",
                        "description": "",
                        "invite_only": conversation == "private_channel",
                    }
                ],
            },
        )
        value["message"]["subject"] = subject
    return value


@pytest.mark.parametrize("historical", [False, True])
@pytest.mark.parametrize(
    ("conversation", "subject"),
    [
        (kind, subject)
        for kind in ("channel", "private_channel")
        for subject in ("", "✔ ", "General", "zulip")
    ]
    + [("direct", None), ("self", None), ("group", None)],
)
def test_zulip_unnamed_topics_follow_conversation_kind_and_preserve_identity(
    incoming, conversation, subject, historical
):
    api, db, account, _http, _url = incoming
    value = message(incoming, conversation, subject)
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
    fallback = "zulip" if conversation in ("direct", "self") else "General"
    assert topic["name"] == ((subject or "").removeprefix("✔ ") or fallback)
    assert topic["is_done"] == (subject == "✔ ")
    assert topic["provider_identity"].endswith(":" + (subject or ""))
    if subject == "":
        assert placement[0] == placement[1]
    # An ordinary named channel also has an empty-subject default container.
    if subject is not None:
        default = api.get(f"/v1/messenger/stream_topics/{placement[1]}").json()
        assert default["name"] == "General" and default["provider_identity"] == "42:"
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


@pytest.mark.parametrize(
    ("before_name", "later_name"),
    [("General", None), ("Custom", None), ("zulip", "General"), ("zulip", "Custom")],
)
def test_label_migration_rollback_preserves_unmodified_and_later_names(
    incoming, before_name, later_name
):
    api, db, account, *_ = incoming
    filename = next(conftest.MIGRATIONS_DIR.glob("*-141e3a.py")).name
    migration = conftest.DataMigration(filename)
    migration.rollback_migration(filename)
    try:
        incoming_tests.apply(incoming, message(incoming, "channel", ""))
        topic_uuid = db.execute(
            "UPDATE messenger_topics SET name=%s WHERE project_id=%s "
            "AND provider_identity='42:' RETURNING uuid",
            (before_name, account["project_id"]),
        ).fetchone()[0]
        migration.apply_migration(filename)
        if later_name is not None:
            db.execute(
                "UPDATE messenger_topics SET name=%s,updated_at=clock_timestamp() "
                "WHERE project_id=%s AND uuid=%s",
                (later_name, account["project_id"], topic_uuid),
            )
        migration.rollback_migration(filename)
        topic = api.get(f"/v1/messenger/stream_topics/{topic_uuid}").json()
        assert topic["name"] == (later_name or before_name)
    finally:
        migration.apply_migration(filename)


@pytest.mark.parametrize(
    "conversation", ["channel", "private_channel", "group", "direct", "self"]
)
def test_upgrade_restores_only_nonprivate_unnamed_topic_labels(incoming, conversation):
    api, db, account, *_ = incoming
    filename = next(conftest.MIGRATIONS_DIR.glob("*-141e3a.py")).name
    migration = conftest.DataMigration(filename)
    migration.rollback_migration(filename)
    try:
        subject = "" if conversation in ("channel", "private_channel") else None
        value = message(incoming, conversation, subject)
        incoming_tests.apply(incoming, value)
        stream, default = db.execute(
            "SELECT stream_uuid,topic_uuid FROM messenger_message_placements WHERE project_id=%s",
            (account["project_id"],),
        ).fetchone()
        repaired = [default]
        if subject is not None:
            closed = incoming_tests.event(identifier=102)
            closed["message"]["subject"] = "✔ "
            explicit = incoming_tests.event(identifier=103)
            explicit["message"]["subject"] = "zulip"
            incoming_tests.apply(incoming, closed, explicit)
            repaired.append(
                db.execute(
                    "SELECT uuid FROM messenger_topics WHERE project_id=%s AND provider_identity='42:✔ '",
                    (account["project_id"],),
                ).fetchone()[0]
            )
        native = api.post("/v1/messenger/streams/", json={"name": "Native"}).json()
        native_topic = api.post(
            "/v1/messenger/stream_topics/",
            json={"stream_uuid": native["uuid"], "name": "zulip"},
        ).json()
        before = db.execute(
            "SELECT uuid,provider_identity,is_done FROM messenger_topics WHERE project_id=%s ORDER BY uuid",
            (account["project_id"],),
        ).fetchall()
        placements = db.execute(
            "SELECT uuid,topic_uuid,message_uuid FROM messenger_message_placements WHERE project_id=%s ORDER BY uuid",
            (account["project_id"],),
        ).fetchall()
        db.execute(
            "UPDATE messenger_topics SET name='zulip' WHERE project_id=%s AND uuid=ANY(%s)",
            (account["project_id"], repaired),
        )
        outgoing = db.execute("SELECT count(*) FROM zulip_outgoing_intents").fetchone()
        migration.apply_migration(filename)
        for identifier in repaired:
            topic = api.get(f"/v1/messenger/stream_topics/{identifier}").json()
            assert topic["name"] == (
                "zulip" if conversation in ("direct", "self") else "General"
            )
        assert (
            api.get(f"/v1/messenger/stream_topics/{native_topic['uuid']}").json()[
                "name"
            ]
            == "zulip"
        )
        if subject is not None:
            assert db.execute(
                "SELECT name FROM messenger_topics WHERE project_id=%s AND provider_identity='42:zulip'",
                (account["project_id"],),
            ).fetchone() == ("zulip",)
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
        assert db.execute(
            "SELECT default_topic_uuid FROM messenger_streams WHERE project_id=%s AND uuid=%s",
            (account["project_id"], stream),
        ).fetchone() == (default,)
        migration.rollback_migration(filename)
        assert db.execute(
            "SELECT DISTINCT name FROM messenger_topics WHERE project_id=%s AND uuid=ANY(%s)",
            (account["project_id"], repaired),
        ).fetchall() == [("zulip",)]
        migration.apply_migration(filename)
    finally:
        migration.apply_migration(filename)
