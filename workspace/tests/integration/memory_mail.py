# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import collections
import contextlib
import datetime
import email
import email.policy
import re
import uuid as sys_uuid

from workspace.messenger_mail import protocol
from workspace.messenger_mail import repository
from workspace.messenger_mail import service


class MemoryImapClient:
    def __init__(self):
        self.mailboxes = collections.defaultdict(list)
        self.next_uids = collections.defaultdict(lambda: 1)
        self.current_mailbox = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return None

    def ensure_mailbox(self, path):
        created = path not in self.mailboxes
        self.mailboxes[path]
        return created

    def append(self, path, raw_message, flags=(), keywords=()):
        uid = self.next_uids[path]
        self.next_uids[path] += 1
        self.mailboxes[path].append(
            protocol.FetchedMessage(
                uid,
                frozenset((*flags, *keywords)),
                raw_message,
            )
        )
        return protocol.AppendUid(1, uid)

    def select(self, path, readonly=True):
        del readonly
        self.ensure_mailbox(path)
        self.current_mailbox = path
        return protocol.MailboxMetadata(
            1,
            self.next_uids[path],
            len(self.mailboxes[path]),
        )

    def search(self, criteria="ALL"):
        messages = self.mailboxes[self.current_mailbox]
        if criteria == "ALL":
            return [message.uid for message in messages]
        match = re.fullmatch(r"UID (\d+):\*", criteria)
        if match is None:
            raise ValueError(f"Unsupported integration IMAP search: {criteria}")
        first_uid = int(match.group(1))
        return [message.uid for message in messages if message.uid >= first_uid]

    def fetch(self, uids):
        requested = set(uids)
        return [
            message
            for message in self.mailboxes[self.current_mailbox]
            if message.uid in requested
        ]

    def delete_by_message_id(self, path, message_id):
        deleted = []
        retained = []
        for message in self.mailboxes[path]:
            parsed = email.message_from_bytes(
                message.raw_message,
                policy=email.policy.default,
            )
            if parsed["Message-ID"] == message_id:
                deleted.append(message.uid)
            else:
                retained.append(message)
        self.mailboxes[path] = retained
        return deleted


class MemorySmtpClient:
    def __init__(self, user_mailboxes):
        self.user_mailboxes = user_mailboxes

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return None

    def send(self, message):
        raw_message = message.as_bytes()
        for address in message["To"].addresses:
            local_part = address.addr_spec.split("@", 1)[0]
            if not local_part.startswith("u-"):
                raise ValueError("Messenger recipient is not an IAM technical address")
            user_uuid = sys_uuid.UUID(hex=local_part.removeprefix("u-"))
            mailbox = self.user_mailboxes.setdefault(user_uuid, MemoryImapClient())
            mailbox.append(service.MESSAGE_MAILBOX, raw_message)


class RuntimeFactory:
    def __init__(self):
        self.project_mailboxes = {}
        self.user_mailboxes = {}

    def _repository(self, project_uuid, rebuild=True):
        project_uuid = sys_uuid.UUID(str(project_uuid))
        client = self.project_mailboxes.setdefault(project_uuid, MemoryImapClient())
        mail_repository = repository.MessengerMailRepository(client, project_uuid)
        if rebuild:
            mail_repository.rebuild()
        return mail_repository

    @contextlib.contextmanager
    def messenger_service(self, project_uuid):
        mail_repository = self._repository(project_uuid, rebuild=False)
        yield service.MessengerMailService(
            mail_repository,
            self.smtp_client,
            self.user_imap_client,
        )

    @contextlib.contextmanager
    def smtp_client(self):
        yield MemorySmtpClient(self.user_mailboxes)

    @contextlib.contextmanager
    def user_imap_client(self, user_uuid):
        user_uuid = sys_uuid.UUID(str(user_uuid))
        yield self.user_mailboxes.setdefault(user_uuid, MemoryImapClient())

    def seed_stream(self, project_uuid, actor_uuid, stream_uuid, payload):
        mail_repository = self._repository(project_uuid)
        stream_uuid = sys_uuid.UUID(str(stream_uuid))
        if stream_uuid in mail_repository.projection.streams:
            return
        mail_repository.append_operation(
            self._operation(
                project_uuid,
                actor_uuid,
                "stream.create",
                stream_uuid,
                payload,
            )
        )

    def seed_binding(
        self,
        project_uuid,
        actor_uuid,
        binding_uuid,
        stream_uuid,
        user_uuid,
        role,
    ):
        mail_repository = self._repository(project_uuid)
        stream_uuid = sys_uuid.UUID(str(stream_uuid))
        user_uuid = sys_uuid.UUID(str(user_uuid))
        if any(
            binding.get("stream_uuid") == str(stream_uuid)
            and binding.get("user_uuid") == str(user_uuid)
            for binding in mail_repository.projection.bindings.values()
        ):
            return
        mail_repository.append_operation(
            self._operation(
                project_uuid,
                actor_uuid,
                "stream_binding.create",
                binding_uuid,
                {
                    "stream_uuid": str(stream_uuid),
                    "user_uuid": str(user_uuid),
                    "role": role,
                },
            )
        )

    def seed_topic(
        self,
        project_uuid,
        actor_uuid,
        topic_uuid,
        stream_uuid,
        name,
        is_default=False,
    ):
        mail_repository = self._repository(project_uuid)
        topic_uuid = sys_uuid.UUID(str(topic_uuid))
        if topic_uuid in mail_repository.projection.topics:
            return
        mail_repository.append_operation(
            self._operation(
                project_uuid,
                actor_uuid,
                "topic.create",
                topic_uuid,
                {
                    "stream_uuid": str(stream_uuid),
                    "name": name,
                    "source_name": "native",
                    "source": {"kind": "native"},
                },
            )
        )
        if is_default:
            mail_repository.append_operation(
                self._operation(
                    project_uuid,
                    actor_uuid,
                    "stream.update",
                    stream_uuid,
                    {"default_topic_uuid": str(topic_uuid)},
                )
            )

    @staticmethod
    def _operation(project_uuid, actor_uuid, name, entity_uuid, payload):
        return repository.OperationRecord(
            project_uuid=sys_uuid.UUID(str(project_uuid)),
            operation_uuid=sys_uuid.uuid4(),
            actor_uuid=sys_uuid.UUID(str(actor_uuid)),
            operation=name,
            entity_uuid=sys_uuid.UUID(str(entity_uuid)),
            payload=payload,
            occurred_at=datetime.datetime.now(datetime.timezone.utc),
        )
