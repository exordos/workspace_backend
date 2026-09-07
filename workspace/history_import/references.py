#    Copyright 2026 Genesis Corporation.
#
#    All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""Bounded canonical routing receipts for the bridge's existing message actions."""

import typing
import uuid as sys_uuid

from workspace.external_bridge_control import provider_v2
from workspace.history_import import contract
from workspace.history_import import repository


PAGE_SIZE = 500


def page(
    session: typing.Any,
    identity: typing.Any,
    job: dict,
    account_uuid: sys_uuid.UUID,
    kind: str,
    cursor: str,
) -> dict:
    sources = repository.guard(session, identity, job)
    routes = [source for source in sources if source["account_uuid"] == account_uuid]
    if not routes or job["status"] != "complete":
        raise contract.ImportError("history_references_not_ready", 409)
    if kind == "users":
        if not cursor.isascii() or not cursor.isdecimal():
            raise contract.ImportError("invalid_history_cursor", 400)
        rows = session.execute(
            """SELECT link.provider_user_id, link.workspace_user_uuid, user_row.first_name
               FROM m_external_provider_identity_links_v1 AS link
               JOIN m_workspace_users AS user_row ON user_row.uuid = link.workspace_user_uuid
               WHERE link.provider = 'zulip' AND link.provider_realm_uuid = %s
                 AND link.provider_user_id > %s
                 AND EXISTS (SELECT 1 FROM messenger_project_users AS member
                             WHERE member.project_id = %s AND member.user_uuid = link.workspace_user_uuid)
               ORDER BY link.provider_user_id LIMIT %s""",
            (job["provider_realm_uuid"], cursor, job["project_uuid"], PAGE_SIZE),
        ).fetchall()
        return {
            "mappings": [
                {
                    "kind": "identity",
                    "provider_id": row["provider_user_id"],
                    "workspace_uuid": str(row["workspace_user_uuid"]),
                    "metadata": {"display_name": row["first_name"]},
                }
                for row in rows
            ],
            "next_cursor": rows[-1]["provider_user_id"]
            if len(rows) == PAGE_SIZE
            else None,
        }
    if kind != "messages" or not cursor.isascii() or not cursor.isdecimal():
        raise contract.ImportError("invalid_history_cursor", 400)
    after = int(cursor)
    if not job["from_id"] - 1 <= after <= job["to_id"]:
        raise contract.ImportError("invalid_history_cursor", 400)
    end = min(job["to_id"], after + PAGE_SIZE // 2)
    rows = session.execute(
        """SELECT message.provider_message_id, message.author_uuid, message.created_at,
                  placement.uuid, placement.legacy_public_uuid, placement.stream_uuid, placement.topic_uuid,
                  topic.name AS topic_name, topic.provider AS topic_provider
           FROM messenger_messages AS message
           JOIN LATERAL (
               SELECT p.* FROM messenger_message_placements AS p
               WHERE p.project_id = message.project_id AND p.message_uuid = message.uuid
                 AND p.stream_uuid = ANY(%s::uuid[])
                 AND EXISTS (SELECT 1 FROM messenger_api_user_messages_v1 AS visible
                     WHERE visible.project_id = p.project_id AND visible.uuid = p.uuid
                       AND visible.user_uuid = %s AND visible.visible)
               ORDER BY p.created_at, p.uuid LIMIT 1
           ) AS placement ON true
           JOIN messenger_topics AS topic
             ON topic.project_id = message.project_id AND topic.uuid = placement.topic_uuid
           WHERE message.provider_realm_uuid = %s AND message.project_id = %s
             AND message.provider_message_id = ANY(%s::text[]) AND message.deleted_at IS NULL""",
        (
            [route["projection_stream_uuid"] for route in routes],
            routes[0]["owner_user_uuid"],
            job["provider_realm_uuid"],
            job["project_uuid"],
            [str(value) for value in range(after + 1, end + 1)],
        ),
    ).fetchall()
    by_stream = {route["projection_stream_uuid"]: route for route in routes}
    mappings = []
    topic_mappings = {}
    for row in rows:
        route = by_stream[row["stream_uuid"]]
        topics = {
            str(topic["topic_uuid"]): topic for topic in route["source"]["topics"]
        }
        topic = topics.get(str(row["topic_uuid"]))
        provider = row["topic_provider"]
        if topic is None and (
            provider.get("kind") == "zulip"
            and provider.get("provider_realm_uuid") == str(job["provider_realm_uuid"])
            and provider.get("chat_key") == route["chat_key"]
        ):
            topic = {
                "provider_topic_id": provider["external_id"],
                "name": row["topic_name"],
            }
        if topic is None:
            continue
        topic_mappings[str(row["topic_uuid"])] = {
            "kind": "topic",
            "provider_id": topic["provider_topic_id"],
            "workspace_uuid": str(row["topic_uuid"]),
            "metadata": {
                "workspace_delivery_state": "committed",
                "stream_uuid": str(row["stream_uuid"]),
                "chat_key": provider_v2._legacy_chat_key(route["chat_key"]),
                "name": topic["name"],
            },
        }
        mappings.append(
            {
                "kind": "message",
                "provider_id": row["provider_message_id"],
                "workspace_uuid": str(row["legacy_public_uuid"] or row["uuid"]),
                "metadata": {
                    "workspace_delivery_state": "committed",
                    "chat_key": provider_v2._legacy_chat_key(route["chat_key"]),
                    "stream_uuid": str(row["stream_uuid"]),
                    "topic_uuid": str(row["topic_uuid"]),
                    "topic_provider_id": topic["provider_topic_id"],
                    "author_uuid": str(row["author_uuid"]),
                    "provider_timestamp": row["created_at"].timestamp(),
                },
            }
        )
    return {
        "mappings": [topic_mappings[key] for key in sorted(topic_mappings)]
        + sorted(mappings, key=lambda item: int(item["provider_id"])),
        "next_cursor": str(end) if end < job["to_id"] else None,
    }
