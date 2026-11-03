import datetime
import json
import pathlib

import pytest

from telethon.tl import types

from workspace.bridges.telegram import conversion


def message(**values):
    return types.Message(
        id=41,
        peer_id=types.PeerUser(8),
        date=datetime.datetime(2026, 1, 2, tzinfo=datetime.timezone.utc),
        message="Hello",
        from_id=types.PeerUser(7),
        **values,
    )


def test_private_and_basic_group_ids_are_account_scoped_but_channel_ids_are_shared():
    for peer in ("user:8", "chat:8"):
        assert conversion.message_identity(7, peer, 41) != conversion.message_identity(
            9, peer, 41
        )
    assert conversion.message_identity(
        7, "channel:8", 41
    ) == conversion.message_identity(9, "channel:8", 41)
    assert conversion.stream_identity(7, "user:8") != conversion.stream_identity(
        7, "chat:8"
    )
    assert conversion.peer_key(types.PeerChannel(8)) == "channel:8"


def test_normal_reply_does_not_create_a_forum_topic():
    data = conversion.normalize_message(
        7, "user:8", message(reply_to=types.MessageReplyHeader(reply_to_msg_id=15))
    )
    assert data["topic_id"] == 0
    assert data["reply_to"] == 15


def test_forum_reply_stays_in_its_root_topic():
    data = conversion.normalize_message(
        7,
        "channel:8",
        message(
            reply_to=types.MessageReplyHeader(
                reply_to_msg_id=15, reply_to_top_id=12, forum_topic=True
            )
        ),
    )
    assert data["topic_id"] == 12


def test_telegram_utf16_entity_offsets_preserve_non_bmp_text():
    value = message()
    value.message = "😀 bold"
    value.entities = [types.MessageEntityBold(offset=3, length=4)]
    assert conversion.normalize_message(7, "user:8", value)["text"] == "😀 **bold**"


def test_source_timestamps_survive_normalization():
    value = message(
        edit_date=datetime.datetime(2026, 1, 3, tzinfo=datetime.timezone.utc)
    )
    data = conversion.normalize_message(7, "user:8", value)
    assert data["date"] == "2026-01-02T00:00:00+00:00"
    assert data["edited_at"] == "2026-01-03T00:00:00+00:00"


EXAMPLES = json.loads(
    (pathlib.Path(__file__).parents[1] / "fixtures/telegram_messages.json").read_text()
)


@pytest.mark.parametrize("example", EXAMPLES, ids=lambda item: item["name"])
def test_telegram_conversion_examples_match_production_converter(example):
    if example.get("direction") == "outgoing":
        plain, entities = conversion.to_telegram(example["expected_payload"]["content"])
        assert plain == example["message"]["content"]
        assert [
            dict(type=type(item).__name__, offset=item.offset, length=item.length)
            for item in entities
        ] == example["message"]["entities"]
        return
    value = message()
    value.message = example["message"]["content"]
    value.entities = [
        getattr(types, item["type"])(
            **{key: value for key, value in item.items() if key != "type"}
        )
        for item in example["message"]["entities"]
    ]
    assert (
        conversion.normalize_message(7, "user:8", value)["text"]
        == example["expected_payload"]["content"]
    )


@pytest.mark.parametrize(
    "source,plain,expected",
    [
        ("*italic*", "italic", [("MessageEntityItalic", 0, 6)]),
        ("__bold__", "bold", [("MessageEntityBold", 0, 4)]),
        (r"\*literal\*", "*literal*", []),
        (
            "😀 **bold *italic***",
            "😀 bold italic",
            [("MessageEntityBold", 3, 11), ("MessageEntityItalic", 8, 6)],
        ),
        ("||secret||", "secret", [("MessageEntitySpoiler", 0, 6)]),
        ("```python\nprint(1)\n```", "print(1)", [("MessageEntityPre", 0, 8)]),
    ],
)
def test_workspace_markdown_uses_telegram_entities(source, plain, expected):
    actual, entities = conversion.to_telegram(source)
    assert actual == plain
    assert (
        sorted((type(item).__name__, item.offset, item.length) for item in entities)
        == expected
    )


def test_outgoing_paragraph_separation_is_preserved():
    assert conversion.to_telegram("first\n\nsecond") == ("first\n\nsecond", [])


@pytest.mark.parametrize("kind", ["file", "image", "video", "quote", "message"])
def test_outgoing_references_are_parsed_outside_code_only(kind):
    identifier = "1333316e-bf9e-449a-a109-84667ea74038"
    target = f"urn:{kind}:{identifier}"
    references = {}
    literal = f"[sample]({target})"
    text, entities = conversion.to_telegram(
        f"```\n{literal}\n```\n\n[actual]({target}?text=example)\n\nAnswer",
        references=references,
    )
    assert text == literal + "\n\nAnswer"
    assert isinstance(entities[0], types.MessageEntityPre)
    assert len(entities) == 1
    assert references == (
        {"files": [identifier]}
        if kind in ("file", "image", "video")
        else {"reply": identifier}
    )


def test_unknown_workspace_reference_preserves_label_without_invalid_external_url():
    assert conversion.to_telegram(
        "[Person](urn:user:1333316e-bf9e-449a-a109-84667ea74038)"
    ) == ("Person", [])


def test_named_telegram_mention_uses_a_verified_workspace_identity():
    user = "1333316e-bf9e-449a-a109-84667ea74038"
    assert (
        conversion.to_workspace(
            "😀 Remote", [types.MessageEntityMentionName(3, 6, 8)], mentions={8: user}
        )
        == f"😀 [Remote](urn:user:{user})"
    )


def test_outgoing_workspace_user_reference_is_collected_outside_code_only():
    user = "1333316e-bf9e-449a-a109-84667ea74038"
    references = {}
    plain, entities = conversion.to_telegram(
        f"😀 [Remote](urn:user:{user}) `urn:user:{user}`", references=references
    )
    assert plain == f"😀 Remote urn:user:{user}"
    assert references["mentions"] == [{"user_uuid": user, "offset": 3, "length": 6}]
    assert len(entities) == 1 and isinstance(entities[0], types.MessageEntityCode)
