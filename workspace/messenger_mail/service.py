# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import dataclasses
import typing
import uuid as sys_uuid

from workspace.messenger_mail import codec
from workspace.messenger_mail import protocol
from workspace.messenger_mail import repository


DEFAULT_TECHNICAL_DOMAIN = "messenger.workspace.internal"
MESSAGE_MAILBOX = "INBOX"
INTERNAL_RECIPIENTS_FIELD = "_recipient_uuids"
EVENT_RECIPIENTS_FIELD = "_event_recipient_uuids"


def technical_address(
    user_uuid: sys_uuid.UUID,
    domain: str = DEFAULT_TECHNICAL_DOMAIN,
) -> str:
    return f"u-{user_uuid.hex}@{domain}"


class MessengerMailService:
    def __init__(
        self,
        mail_repository: repository.MessengerMailRepository,
        smtp_factory: typing.Callable[[], typing.ContextManager[protocol.SmtpClient]],
        imap_factory: typing.Callable[
            [sys_uuid.UUID], typing.ContextManager[protocol.ImapClient]
        ],
        technical_domain: str = DEFAULT_TECHNICAL_DOMAIN,
        message_mailbox: str = MESSAGE_MAILBOX,
    ) -> None:
        self.repository = mail_repository
        self.smtp_factory = smtp_factory
        self.imap_factory = imap_factory
        self.technical_domain = technical_domain
        self.message_mailbox = message_mailbox

    def stream_participants(
        self, stream_uuid: sys_uuid.UUID
    ) -> tuple[sys_uuid.UUID, ...]:
        values = {
            sys_uuid.UUID(binding["user_uuid"])
            for binding in self.repository.projection.bindings.values()
            if binding["stream_uuid"] == str(stream_uuid)
        }
        return tuple(sorted(values, key=str))

    def validate_stream_participants(
        self,
        stream_uuid: sys_uuid.UUID,
        participants: tuple[sys_uuid.UUID, ...],
    ) -> None:
        stream = self.repository.projection.streams[stream_uuid]
        if stream.get("kind") != "direct":
            return
        if len(participants) != 2 or participants[0] == participants[1]:
            raise repository.InvalidJournalRecord(
                "Direct streams require exactly two distinct participants"
            )
        expected_uuid = repository.deterministic_dm_uuid(
            self.repository.project_uuid,
            participants[0],
            participants[1],
        )
        if stream_uuid != expected_uuid:
            raise repository.InvalidJournalRecord(
                "Direct stream UUID must be deterministic for its participants"
            )

    def deliver_message(
        self,
        record: repository.OperationRecord,
    ) -> protocol.AppendUid:
        if record.operation != "message.create":
            raise ValueError("Message delivery requires message.create")

        stream_uuid = sys_uuid.UUID(record.payload["stream_uuid"])
        topic_uuid = sys_uuid.UUID(record.payload["topic_uuid"])
        author_uuid = sys_uuid.UUID(record.payload["author_uuid"])
        markdown_payload = record.payload["payload"]
        if markdown_payload["kind"] != "markdown":
            raise repository.InvalidJournalRecord(
                "Messenger mail delivery only supports markdown"
            )
        participants = self.stream_participants(stream_uuid)
        self.validate_stream_participants(stream_uuid, participants)
        if author_uuid not in participants:
            raise repository.InvalidJournalRecord(
                "Message author must be an ordinary stream participant"
            )

        source_name = record.payload.get("source_name", "native")
        source = record.payload.get("source", {"kind": source_name})
        recipient_values = [str(user_uuid) for user_uuid in participants]
        persisted_payload = dict(record.payload)
        persisted_payload["source_name"] = source_name
        persisted_payload["source"] = source
        persisted_payload[INTERNAL_RECIPIENTS_FIELD] = recipient_values
        persisted_record = dataclasses.replace(record, payload=persisted_payload)
        previous = self.repository.projection.message_positions.get(record.entity_uuid)
        if previous is not None:
            existing = self.repository.projection.messages[record.entity_uuid]
            identity_fields = (
                "stream_uuid",
                "topic_uuid",
                "author_uuid",
                "payload",
                "source_name",
                "source",
            )
            if any(
                existing.get(name) != persisted_record.payload[name]
                for name in identity_fields
            ):
                raise repository.InvalidJournalRecord(
                    "Existing message UUID belongs to a different message"
                )
            return previous
        envelope = codec.MessengerEnvelope(
            from_address=technical_address(author_uuid, self.technical_domain),
            to_addresses=tuple(
                technical_address(user_uuid, self.technical_domain)
                for user_uuid in participants
            ),
            project_uuid=record.project_uuid,
            message_uuid=record.entity_uuid,
            stream_uuid=stream_uuid,
            topic_uuid=topic_uuid,
            author_uuid=author_uuid,
            operation_uuid=record.operation_uuid,
            markdown=markdown_payload["content"],
            sent_at=record.occurred_at,
            source_name=source_name,
            source=source,
        )

        for user_uuid in participants:
            self._ensure_mailbox(user_uuid)
            self._delete_message_copy(user_uuid, codec.message_id(record.entity_uuid))
        with self.smtp_factory() as smtp_client:
            smtp_client.send(codec.build_message(envelope))
        return self.repository.append_operation(persisted_record)

    def delete_message(
        self,
        record: repository.OperationRecord,
    ) -> protocol.AppendUid:
        if record.operation != "message.delete":
            raise ValueError("Message deletion requires message.delete")
        previous = self.repository.projection.operation_positions.get(
            record.operation_uuid
        )
        if previous is not None:
            return previous
        existing_tombstone = self.repository.projection.message_tombstones.get(
            record.entity_uuid
        )
        if existing_tombstone is not None:
            operation_uuid = sys_uuid.UUID(existing_tombstone["operation_uuid"])
            return self.repository.projection.operation_positions[operation_uuid]

        message = self.repository.projection.messages[record.entity_uuid]
        if INTERNAL_RECIPIENTS_FIELD in message:
            participants = tuple(
                sys_uuid.UUID(value) for value in message[INTERNAL_RECIPIENTS_FIELD]
            )
        else:
            participants = self.stream_participants(
                sys_uuid.UUID(message["stream_uuid"])
            )
        message_id = codec.message_id(record.entity_uuid)
        for user_uuid in participants:
            self._delete_message_copy(user_uuid, message_id)

        tombstone_payload = {
            "stream_uuid": message["stream_uuid"],
            "topic_uuid": message["topic_uuid"],
            "author_uuid": message["author_uuid"],
            "source_name": message.get("source_name", "native"),
            "source": message.get("source", {"kind": "native"}),
            INTERNAL_RECIPIENTS_FIELD: [str(value) for value in participants],
            EVENT_RECIPIENTS_FIELD: record.payload.get(
                EVENT_RECIPIENTS_FIELD,
                message.get(
                    EVENT_RECIPIENTS_FIELD,
                    [str(value) for value in participants],
                ),
            ),
        }
        tombstone = dataclasses.replace(record, payload=tombstone_payload)
        return self.repository.append_operation(tombstone)

    def _ensure_mailbox(self, user_uuid: sys_uuid.UUID) -> None:
        with self.imap_factory(user_uuid) as imap_client:
            imap_client.ensure_mailbox(self.message_mailbox)

    def _delete_message_copy(
        self,
        user_uuid: sys_uuid.UUID,
        message_id: str,
    ) -> None:
        with self.imap_factory(user_uuid) as imap_client:
            imap_client.delete_by_message_id(self.message_mailbox, message_id)
