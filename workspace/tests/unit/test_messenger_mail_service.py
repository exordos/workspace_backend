# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import datetime
import email
import email.policy
import imaplib
import uuid as sys_uuid

import pytest

from workspace.messenger_mail import codec
from workspace.messenger_mail import protocol
from workspace.messenger_mail import repository
from workspace.messenger_mail import service


PROJECT_UUID = sys_uuid.UUID("10000000-0000-0000-0000-000000000001")
OWNER_UUID = sys_uuid.UUID("20000000-0000-0000-0000-000000000002")
MEMBER_UUID = sys_uuid.UUID("30000000-0000-0000-0000-000000000003")
OUTSIDER_UUID = sys_uuid.UUID("40000000-0000-0000-0000-000000000004")
TOPIC_UUID = sys_uuid.UUID("50000000-0000-0000-0000-000000000005")
MESSAGE_UUID = sys_uuid.UUID("60000000-0000-0000-0000-000000000006")
NOW = datetime.datetime(2026, 7, 15, 13, 0, tzinfo=datetime.timezone.utc)


def _operation(name, entity_uuid, payload, second=0):
    return repository.OperationRecord(
        project_uuid=PROJECT_UUID,
        operation_uuid=sys_uuid.uuid4(),
        actor_uuid=OWNER_UUID,
        operation=name,
        entity_uuid=entity_uuid,
        payload=payload,
        occurred_at=NOW + datetime.timedelta(seconds=second),
    )


class FakeJournalImap:
    def __init__(self, timeline):
        self.timeline = timeline
        self.messages = []

    def ensure_mailbox(self, path):
        assert path == repository.STATE_MAILBOX
        return len(self.messages) == 0

    def append(self, path, raw_message, flags=(), keywords=()):
        assert path == repository.STATE_MAILBOX
        record = repository.decode_operation(raw_message)
        self.messages.append(
            protocol.FetchedMessage(len(self.messages) + 1, frozenset(), raw_message)
        )
        self.timeline.append(f"journal:{record.operation}")
        return protocol.AppendUid(91, len(self.messages))


class FakeUserImap:
    def __init__(self, user_uuid, timeline):
        self.user_uuid = user_uuid
        self.timeline = timeline
        self.created = False
        self.create_count = 0
        self.messages = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return None

    def select(self, path, readonly=True):
        assert path == service.MESSAGE_MAILBOX
        if not self.created:
            raise imaplib.IMAP4.error("mailbox does not exist")
        return protocol.MailboxMetadata(51, len(self.messages) + 1, len(self.messages))

    def create_mailbox(self, path):
        assert path == service.MESSAGE_MAILBOX
        self.created = True
        self.create_count += 1

    def ensure_mailbox(self, path):
        assert path == service.MESSAGE_MAILBOX
        if self.created:
            return False
        self.create_mailbox(path)
        return True

    def delete_by_message_id(self, path, message_id):
        assert path == service.MESSAGE_MAILBOX
        self.timeline.append(f"delete:{self.user_uuid}")
        deleted = []
        retained = []
        for uid, raw_message in enumerate(self.messages, start=1):
            message = email.message_from_bytes(
                raw_message,
                policy=email.policy.default,
            )
            if message["Message-ID"] == message_id:
                deleted.append(uid)
            else:
                retained.append(raw_message)
        self.messages = retained
        return deleted


class FakeSmtp:
    def __init__(self, mailboxes, timeline):
        self.mailboxes = mailboxes
        self.timeline = timeline
        self.send_count = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return None

    def send(self, message):
        self.send_count += 1
        self.timeline.append("smtp")
        raw_message = message.as_bytes()
        addresses = {
            service.technical_address(user_uuid): user_uuid
            for user_uuid in self.mailboxes
        }
        for address in message["To"].addresses:
            self.mailboxes[addresses[address.addr_spec]].messages.append(raw_message)


def _service_with_projection(stream_uuid, kind="stream", users=None):
    users = users or (OWNER_UUID, MEMBER_UUID)
    timeline = []
    journal_imap = FakeJournalImap(timeline)
    mail_repository = repository.MessengerMailRepository(journal_imap, PROJECT_UUID)
    mail_repository.append_operation(
        _operation(
            "stream.create",
            stream_uuid,
            {"kind": kind, "name": "Chat"},
        )
    )
    for index, user_uuid in enumerate(users):
        mail_repository.append_operation(
            _operation(
                "stream_binding.create",
                sys_uuid.uuid4(),
                {
                    "stream_uuid": str(stream_uuid),
                    "user_uuid": str(user_uuid),
                    "role": "owner" if index == 0 else "member",
                },
            )
        )
    mailboxes = {user_uuid: FakeUserImap(user_uuid, timeline) for user_uuid in users}
    smtp = FakeSmtp(mailboxes, timeline)
    mail_service = service.MessengerMailService(
        mail_repository,
        lambda: smtp,
        lambda user_uuid: mailboxes[user_uuid],
    )
    timeline.clear()
    return mail_service, mail_repository, mailboxes, smtp, timeline


def test_technical_address_is_deterministic_and_contains_only_iam_uuid():
    first = service.technical_address(OWNER_UUID)

    assert first == service.technical_address(OWNER_UUID)
    assert first == "u-20000000000000000000000000000002@messenger.workspace.internal"
    assert service.technical_address(MEMBER_UUID) != first


def test_deliver_and_delete_use_all_bindings_preserve_provenance_and_are_idempotent():
    stream_uuid = repository.deterministic_dm_uuid(
        PROJECT_UUID,
        OWNER_UUID,
        MEMBER_UUID,
    )
    mail_service, mail_repository, mailboxes, smtp, timeline = _service_with_projection(
        stream_uuid, kind="direct"
    )
    create = _operation(
        "message.create",
        MESSAGE_UUID,
        {
            "stream_uuid": str(stream_uuid),
            "topic_uuid": str(TOPIC_UUID),
            "author_uuid": str(OWNER_UUID),
            "payload": {
                "kind": "markdown",
                "content": "hello [file](urn:file:70000000-0000-0000-0000-000000000007)",
            },
            "source_name": "native",
            "source": {"kind": "native"},
            service.EVENT_RECIPIENTS_FIELD: [
                str(OWNER_UUID),
                str(MEMBER_UUID),
            ],
        },
        second=1,
    )

    created_position = mail_service.deliver_message(create)
    duplicate_position = mail_service.deliver_message(create)

    assert created_position == duplicate_position
    assert smtp.send_count == 1
    assert timeline[-2:] == ["smtp", "journal:message.create"]
    for mailbox in mailboxes.values():
        assert mailbox.created is True
        assert mailbox.create_count == 1
        assert len(mailbox.messages) == 1
        envelope = codec.parse_message(mailbox.messages[0])
        assert envelope.markdown.endswith(
            "urn:file:70000000-0000-0000-0000-000000000007)"
        )
        assert envelope.source_name == "native"
        assert envelope.source == {"kind": "native"}
    persisted = mail_repository.projection.messages[MESSAGE_UUID]
    assert persisted["source_name"] == "native"
    assert persisted["source"] == {"kind": "native"}
    assert persisted[service.INTERNAL_RECIPIENTS_FIELD] == [
        str(OWNER_UUID),
        str(MEMBER_UUID),
    ]
    assert persisted[service.EVENT_RECIPIENTS_FIELD] == [
        str(OWNER_UUID),
        str(MEMBER_UUID),
    ]

    timeline.clear()
    delete = _operation("message.delete", MESSAGE_UUID, {}, second=2)
    deleted_position = mail_service.delete_message(delete)

    assert all(mailbox.messages == [] for mailbox in mailboxes.values())
    assert timeline == [
        f"delete:{OWNER_UUID}",
        f"delete:{MEMBER_UUID}",
        "journal:message.delete",
    ]
    tombstone = mail_repository.projection.message_tombstones[MESSAGE_UUID]
    assert tombstone["source_name"] == "native"
    assert tombstone["source"] == {"kind": "native"}
    assert "payload" not in tombstone
    assert tombstone[service.INTERNAL_RECIPIENTS_FIELD] == [
        str(OWNER_UUID),
        str(MEMBER_UUID),
    ]
    assert tombstone[service.EVENT_RECIPIENTS_FIELD] == [
        str(OWNER_UUID),
        str(MEMBER_UUID),
    ]

    timeline.clear()
    assert (
        mail_service.delete_message(
            _operation("message.delete", MESSAGE_UUID, {}, second=3)
        )
        == deleted_position
    )
    assert timeline == []


def test_deliver_rejects_reusing_message_uuid_for_different_content():
    stream_uuid = repository.deterministic_dm_uuid(
        PROJECT_UUID,
        OWNER_UUID,
        MEMBER_UUID,
    )
    mail_service, _, mailboxes, smtp, _ = _service_with_projection(
        stream_uuid,
        kind="direct",
    )
    original = _operation(
        "message.create",
        MESSAGE_UUID,
        {
            "stream_uuid": str(stream_uuid),
            "topic_uuid": str(TOPIC_UUID),
            "author_uuid": str(OWNER_UUID),
            "payload": {"kind": "markdown", "content": "original"},
        },
    )
    conflicting_retry = _operation(
        "message.create",
        MESSAGE_UUID,
        {
            "stream_uuid": str(stream_uuid),
            "topic_uuid": str(TOPIC_UUID),
            "author_uuid": str(OWNER_UUID),
            "payload": {"kind": "markdown", "content": "different"},
        },
        second=1,
    )

    mail_service.deliver_message(original)

    with pytest.raises(
        repository.InvalidJournalRecord,
        match="message UUID",
    ):
        mail_service.deliver_message(conflicting_retry)

    assert smtp.send_count == 1
    for mailbox in mailboxes.values():
        assert len(mailbox.messages) == 1
        assert codec.parse_message(mailbox.messages[0]).markdown == "original"


def test_group_stream_delivers_to_every_current_ordinary_binding():
    stream_uuid = sys_uuid.UUID("80000000-0000-0000-0000-000000000008")
    users = (OWNER_UUID, MEMBER_UUID, OUTSIDER_UUID)
    mail_service, _, mailboxes, smtp, _ = _service_with_projection(
        stream_uuid,
        users=users,
    )
    create = _operation(
        "message.create",
        MESSAGE_UUID,
        {
            "stream_uuid": str(stream_uuid),
            "topic_uuid": str(TOPIC_UUID),
            "author_uuid": str(OWNER_UUID),
            "payload": {"kind": "markdown", "content": "group message"},
        },
    )

    mail_service.deliver_message(create)

    assert smtp.send_count == 1
    assert all(len(mailbox.messages) == 1 for mailbox in mailboxes.values())


def test_direct_stream_enforces_two_bindings_and_deterministic_uuid():
    wrong_uuid = sys_uuid.UUID("90000000-0000-0000-0000-000000000009")
    mail_service, _, _, smtp, _ = _service_with_projection(
        wrong_uuid,
        kind="direct",
    )
    create = _operation(
        "message.create",
        MESSAGE_UUID,
        {
            "stream_uuid": str(wrong_uuid),
            "topic_uuid": str(TOPIC_UUID),
            "author_uuid": str(OWNER_UUID),
            "payload": {"kind": "markdown", "content": "must fail"},
        },
    )

    with pytest.raises(
        repository.InvalidJournalRecord,
        match="UUID must be deterministic",
    ):
        mail_service.deliver_message(create)

    assert smtp.send_count == 0

    deterministic_uuid = repository.deterministic_dm_uuid(
        PROJECT_UUID,
        OWNER_UUID,
        MEMBER_UUID,
    )
    three_user_service, _, _, _, _ = _service_with_projection(
        deterministic_uuid,
        kind="direct",
        users=(OWNER_UUID, MEMBER_UUID, OUTSIDER_UUID),
    )
    participants = three_user_service.stream_participants(deterministic_uuid)
    with pytest.raises(
        repository.InvalidJournalRecord,
        match="exactly two distinct participants",
    ):
        three_user_service.validate_stream_participants(
            deterministic_uuid,
            participants,
        )
