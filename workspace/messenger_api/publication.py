# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Persist event payloads with mutations; allocate browser epochs separately."""

import json
import dataclasses
import typing
import uuid as sys_uuid

from restalchemy.storage.sql import sessions

from workspace.messenger_api.api import resource_projection


@dataclasses.dataclass(frozen=True, order=True)
class PendingEvent:
    uuid: sys_uuid.UUID
    user_uuid: sys_uuid.UUID


def enqueue(
    session: sessions.PgSQLSession,
    project_id: object,
    entity_uuid: object,
    recipients: typing.Iterable[object],
    kind: str,
    payload: typing.Any,
    overrides: typing.Any = None,
) -> list[PendingEvent]:
    event_uuid = sys_uuid.uuid4()
    recipients = sorted({sys_uuid.UUID(str(value)) for value in recipients})
    session.execute(
        """
        INSERT INTO messenger_publication_outbox
            (uuid, project_id, entity_uuid, kind, recipients, payload, recipient_payloads)
        VALUES (%s, %s, %s, %s, %s::uuid[], %s::jsonb, %s::jsonb)
        """,
        (
            event_uuid,
            project_id,
            entity_uuid,
            kind,
            recipients,
            json.dumps(resource_projection.simple(payload)),
            json.dumps(resource_projection.simple(overrides)),
        ),
    )
    # Membership guards refer to the final event UUID, not a temporary epoch.
    return [PendingEvent(uuid=event_uuid, user_uuid=user) for user in recipients]
