# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Opt-in asynchronous mutations; canonical creation remains synchronous.

Admission validates the persisted resource and writes only the immutable
command. The response is the accepted representation, not an application ACK.
"""

import json
import typing
import uuid

from oslo_config import cfg
from restalchemy.common import contexts
from restalchemy.common import exceptions

from workspace.common import emoji
from workspace.common import mutation_journal_opts
from workspace.messenger_api import exceptions as messenger_exceptions
from workspace.messenger_api.api import resource_projection
from workspace.messenger_api.api import v2_store
from workspace.messenger_api.dm import v2_models


class JournalStore(v2_store.MessengerV2Store):
    def _admit(
        self,
        operation: str,
        resource_uuid: uuid.UUID,
        values: dict[str, typing.Any],
        key: str,
        topics: list[dict[str, typing.Any]],
        response: typing.Any,
    ) -> typing.Any:
        command_uuid = uuid.uuid4()
        request_uuid = command_uuid
        try:
            request_context = contexts.get_context()
        except contexts.ContextIsNotExistsInStorage:
            pass
        else:
            request_uuid = getattr(
                request_context, "messenger_mutation_uuid", command_uuid
            )
            request_context.messenger_mutation_uuid = request_uuid
            request_context.request.environ["workspace.mutation_uuid"] = request_uuid
        if operation in {"read", "read_up_to", "star", "unstar", "pin", "unpin"}:
            state = (
                contexts.Context()
                .get_session()
                .execute(
                    """SELECT state.membership_generation,placement.message_uuid AS canonical_message_uuid,
                          canonical.ingest_sequence,placement.topic_uuid
                   FROM messenger_user_message_states AS state
                   JOIN messenger_message_placements AS placement
                     ON placement.project_id=state.project_id AND placement.uuid=state.placement_uuid
                   JOIN messenger_messages AS canonical
                     ON canonical.project_id=placement.project_id AND canonical.uuid=placement.message_uuid
                   WHERE state.project_id=%s AND state.placement_uuid=%s AND state.user_uuid=%s""",
                    (self.project_uuid, resource_uuid, self.user_uuid),
                )
                .fetchone()
            )
            values = {
                **values,
                "_membership_generation": state["membership_generation"],
                "_topic_uuid": str(state["topic_uuid"]),
            }
            topics = [
                {
                    **dict(topic),
                    "canonical_message_uuid": state["canonical_message_uuid"],
                    "ingest_sequence": state["ingest_sequence"],
                }
                for topic in topics
            ]
        capacity = cfg.CONF[mutation_journal_opts.DOMAIN].max_pending_per_user
        session = contexts.Context().get_session()
        for attempt in range(3):
            admitted = session.execute(
                """
            INSERT INTO messenger_mutation_commands
                (uuid, request_uuid, project_id, user_uuid, scope_key, operation,
                 resource_uuid, payload, admission_slot)
            SELECT %s, %s, %s, %s, %s, %s, %s, %s::jsonb, candidate.slot
            FROM LATERAL (
                SELECT (probe.value + %s) %% %s AS slot
                FROM generate_series(0, %s - 1) AS probe(value)
                WHERE NOT EXISTS (
                    SELECT 1 FROM messenger_mutation_commands AS pending
                    WHERE pending.project_id=%s AND pending.user_uuid=%s
                      AND pending.status='pending'
                      AND pending.admission_slot=(probe.value + %s) %% %s
                ) LIMIT 1
            ) AS candidate
            ON CONFLICT (project_id, user_uuid, admission_slot)
                WHERE status='pending' DO NOTHING
            RETURNING uuid
            """,
                (
                    command_uuid,
                    request_uuid,
                    self.project_uuid,
                    self.user_uuid,
                    key,
                    operation,
                    resource_uuid,
                    json.dumps(
                        resource_projection.simple(
                            {
                                "values": values,
                                "topics": topics,
                                "accepted": response,
                            }
                        )
                    ),
                    (command_uuid.int + attempt) % capacity,
                    capacity,
                    capacity,
                    self.project_uuid,
                    self.user_uuid,
                    (command_uuid.int + attempt) % capacity,
                    capacity,
                ),
            ).fetchone()
            if admitted is not None:
                return response
        raise messenger_exceptions.MutationQueueFullError()

    def get_resource(
        self, resource: str, resource_uuid: uuid.UUID
    ) -> dict[str, typing.Any]:
        if resource == "message_reactions":
            pending = (
                contexts.Context()
                .get_session()
                .execute(
                    """SELECT payload->'accepted' AS accepted FROM messenger_mutation_commands
                   WHERE project_id=%s AND user_uuid=%s AND resource_uuid=%s
                     AND operation IN ('reaction_create','reaction_update') AND status='pending'
                   ORDER BY sequence DESC LIMIT 1""",
                    (self.project_uuid, self.user_uuid, resource_uuid),
                )
                .fetchone()
            )
            if pending is not None:
                return pending["accepted"]
        return super().get_resource(resource, resource_uuid)

    def _topics(
        self, message: v2_models.WorkspaceUserMessage
    ) -> list[dict[str, typing.Any]]:
        return [
            dict(row)
            for row in contexts.Context()
            .get_session()
            .execute(
                """SELECT placement.topic_uuid,placement.stream_uuid,placement.message_uuid AS canonical_message_uuid,
                      canonical.ingest_sequence
               FROM messenger_message_placements AS placement
               JOIN messenger_messages AS canonical
                 ON canonical.project_id=placement.project_id AND canonical.uuid=placement.message_uuid
               WHERE placement.project_id=%s AND placement.message_uuid=%s ORDER BY placement.topic_uuid""",
                (self.project_uuid, message.canonical_message_uuid),
            )
            .fetchall()
        ]

    def update_message(
        self, message_uuid: uuid.UUID, values: dict[str, typing.Any]
    ) -> dict[str, typing.Any]:
        message = self._message(message_uuid)
        if message.author_uuid != self.user_uuid:
            raise exceptions.ValidationErrorException()
        message.validate_payload_update(
            resource_projection.simple(values["payload"]),
            contexts.Context().get_session(),
        )
        response = v2_store._public(message, "messages")
        response["payload"] = resource_projection.simple(values["payload"])
        return self._admit(
            "edit",
            message.uuid,
            values,
            f"{self.project_uuid}:message:{message.canonical_message_uuid}",
            self._topics(message),
            response,
        )

    def delete_message(self, message_uuid: uuid.UUID) -> dict[str, typing.Any] | None:
        message = self._message(message_uuid)
        if message.author_uuid != self.user_uuid:
            raise exceptions.ValidationErrorException()
        return self._admit(
            "delete",
            message.uuid,
            {},
            f"{self.project_uuid}:message:{message.canonical_message_uuid}",
            self._topics(message),
            None,
        )

    def perform_action(
        self,
        resource: str,
        resource_uuid: uuid.UUID,
        action: str,
        values: dict[str, typing.Any],
    ) -> dict[str, typing.Any] | list[dict[str, typing.Any]]:
        if resource == "messages" and action in {
            "read",
            "read_up_to",
            "star",
            "unstar",
            "pin",
            "unpin",
        }:
            message = self._message(resource_uuid)
            response = v2_store._public(message, "messages")
            if action in {"star", "unstar", "pin", "unpin"}:
                response["starred" if "star" in action else "pinned"] = action in {
                    "star",
                    "pin",
                }
            return self._admit(
                action,
                message.uuid,
                values,
                f"{self.project_uuid}:topic:{message.topic_uuid}",
                [
                    {
                        "topic_uuid": message.topic_uuid,
                        "stream_uuid": message.stream_uuid,
                    }
                ],
                response,
            )
        if resource in {"streams", "stream_topics"} and action == "read":
            session = contexts.Context().get_session()
            if resource == "streams":
                response = v2_store._public(self._stream(resource_uuid), resource)
                topics = session.execute(
                    """SELECT uuid AS topic_uuid, stream_uuid FROM messenger_topics
                       WHERE project_id=%s AND stream_uuid=%s AND deleted_at IS NULL
                       ORDER BY uuid""",
                    (self.project_uuid, resource_uuid),
                ).fetchall()
            else:
                topic = self._topic(resource_uuid)
                response = v2_store._public(topic, resource)
                topics = [{"topic_uuid": topic.uuid, "stream_uuid": topic.stream_uuid}]
            # Resolve each topic's exact visible boundary in the admission
            # snapshot. Messages arriving later must remain unread.
            for topic in topics:
                boundary = session.execute(
                    """SELECT placement.uuid FROM messenger_message_placements AS placement
                       JOIN messenger_messages AS message
                         ON message.project_id=placement.project_id AND message.uuid=placement.message_uuid
                       JOIN messenger_user_message_states AS state
                         ON state.project_id=placement.project_id AND state.placement_uuid=placement.uuid
                        AND state.user_uuid=%s
                       JOIN messenger_user_message_bindings AS access
                         ON access.project_id=state.project_id AND access.user_uuid=state.user_uuid
                        AND access.placement_uuid=placement.uuid AND access.visibility='visible'
                        AND access.membership_generation=state.membership_generation
                       JOIN messenger_stream_bindings AS membership
                         ON membership.project_id=state.project_id AND membership.user_uuid=state.user_uuid
                        AND membership.stream_uuid=placement.stream_uuid AND membership.active
                        AND membership.membership_generation=state.membership_generation
                       WHERE placement.project_id=%s AND placement.topic_uuid=%s
                       ORDER BY message.created_at DESC, placement.uuid DESC LIMIT 1""",
                    (self.user_uuid, self.project_uuid, topic["topic_uuid"]),
                ).fetchone()
                if boundary is not None:
                    self._admit(
                        "read_up_to",
                        boundary["uuid"],
                        {},
                        f"{self.project_uuid}:topic:{topic['topic_uuid']}",
                        [dict(topic)],
                        None,
                    )
            return response
        return super().perform_action(resource, resource_uuid, action, values)

    def _reaction_identity(
        self, message: v2_models.WorkspaceUserMessage, name: str, requested: object
    ) -> uuid.UUID:
        # Resolve the accepted state in one MVCC snapshot. A second create
        # must return an identity the worker will actually persist, including
        # when the earlier create/update/delete has not been applied yet.
        rows = (
            contexts.Context()
            .get_session()
            .execute(
                """SELECT 0::bigint AS sequence, 'fact' AS operation, uuid,
                      canonical_message_uuid, emoji_name
               FROM messenger_message_reaction_facts
               WHERE project_id=%s AND user_uuid=%s
               UNION ALL
               SELECT command.sequence, command.operation, command.resource_uuid,
                      placement.message_uuid,
                      command.payload->'accepted'->>'emoji_name'
               FROM messenger_mutation_commands AS command
               LEFT JOIN messenger_message_placements AS placement
                 ON placement.project_id=command.project_id
                AND placement.uuid=NULLIF(command.payload->'accepted'->>'message_uuid','')::uuid
               WHERE command.project_id=%s AND command.user_uuid=%s
                 AND command.status='pending'
                 AND command.operation IN ('reaction_create','reaction_update','reaction_delete')
               ORDER BY sequence""",
                (self.project_uuid, self.user_uuid, self.project_uuid, self.user_uuid),
            )
            .fetchall()
        )
        effective: dict[uuid.UUID, tuple[uuid.UUID, str]] = {}
        generation = 0
        for row in rows:
            generation = max(generation, row["sequence"])
            if row["operation"] == "reaction_delete":
                effective.pop(row["uuid"], None)
            else:
                effective[row["uuid"]] = (
                    row["canonical_message_uuid"],
                    row["emoji_name"],
                )
        identity = (message.canonical_message_uuid, name)
        if requested is not None:
            requested = uuid.UUID(str(requested))
            if requested in effective and effective[requested] != identity:
                raise exceptions.ValidationErrorException()
        for identifier, value in effective.items():
            if value == identity:
                return identifier
        # Concurrent admission of the same new fact shares its identity. The
        # pending sequence generation also separates a moved fact from a new
        # reaction using its former emoji.
        return requested or uuid.uuid5(
            self.project_uuid,
            f"reaction:{message.canonical_message_uuid}:{self.user_uuid}:{name}:{generation}",
        )

    def create_resource(
        self, resource: str, values: dict[str, typing.Any]
    ) -> dict[str, typing.Any]:
        if resource != "message_reactions":
            return super().create_resource(resource, values)
        message = self._message(values["message_uuid"])
        values = {
            **values,
            "emoji_name": emoji.normalize_name(values["emoji_name"]),
            "uuid": self._reaction_identity(
                message, emoji.normalize_name(values["emoji_name"]), values.get("uuid")
            ),
        }
        # Use the real domain model's field validation without persisting it.
        fact = v2_models.WorkspaceMessageReactionFact(
            uuid=values["uuid"],
            project_id=self.project_uuid,
            canonical_message_uuid=message.canonical_message_uuid,
            placement_uuid=message.uuid,
            user_uuid=self.user_uuid,
            emoji_name=values["emoji_name"],
        )
        response = resource_projection.simple(
            {
                "uuid": fact.uuid,
                "project_id": fact.project_id,
                "message_uuid": message.uuid,
                "user_uuid": fact.user_uuid,
                "emoji_name": fact.emoji_name,
                "created_at": fact.created_at,
                "updated_at": fact.updated_at,
            }
        )
        return self._admit(
            "reaction_create",
            fact.uuid,
            values,
            f"{self.project_uuid}:message:{message.canonical_message_uuid}",
            self._topics(message),
            response,
        )

    def update_resource(
        self, resource: str, resource_uuid: uuid.UUID, values: dict[str, typing.Any]
    ) -> dict[str, typing.Any]:
        if resource != "message_reactions":
            return super().update_resource(resource, resource_uuid, values)
        reaction = self.get_resource(resource, resource_uuid)
        if uuid.UUID(str(reaction["user_uuid"])) != self.user_uuid:
            raise exceptions.ValidationErrorException()
        old_message = self._message(reaction["message_uuid"])
        message = self._message(values.get("message_uuid", reaction["message_uuid"]))
        if "emoji_name" in values:
            values = {
                **values,
                "emoji_name": emoji.normalize_name(values["emoji_name"]),
            }
        return self._admit(
            "reaction_update",
            resource_uuid,
            values,
            f"{self.project_uuid}:message:{old_message.canonical_message_uuid}",
            self._topics(old_message) + self._topics(message),
            {**reaction, **values},
        )

    def delete_resource(
        self, resource: str, resource_uuid: uuid.UUID
    ) -> dict[str, typing.Any] | None:
        if resource != "message_reactions":
            return super().delete_resource(resource, resource_uuid)
        reaction = self.get_resource(resource, resource_uuid)
        if uuid.UUID(str(reaction["user_uuid"])) != self.user_uuid:
            raise exceptions.ValidationErrorException()
        message = self._message(reaction["message_uuid"])
        return self._admit(
            "reaction_delete",
            resource_uuid,
            {},
            f"{self.project_uuid}:message:{message.canonical_message_uuid}",
            self._topics(message),
            None,
        )
