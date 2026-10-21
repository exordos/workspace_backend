# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Resolve message references against the current project's persisted bridge data."""

import collections.abc
import datetime
import contextlib
import hashlib
import re
import typing
import urllib.parse
import uuid

import requests
from restalchemy.common import contexts
from restalchemy.storage.sql import sessions
from restalchemy.storage.sql import utils

from workspace.bridges.zulip import message_conversion
from workspace.bridges.zulip import client
from workspace.messenger_api import file_storage
from workspace.messenger_api.dm import external_models
from workspace.messenger_api.dm import helpers


VERSION = 1


@contextlib.contextmanager
def preserve_source_edit_time(
    session: sessions.PgSQLSession, project_id: uuid.UUID, message_uuid: uuid.UUID
) -> collections.abc.Iterator[None]:
    """A derived representation change is not a new user edit."""
    with utils.savepoint():
        previous = session.execute(
            "SELECT current_setting('workspace.projected_message_content', true) AS value"
        ).fetchone()["value"]
        session.execute(
            "SELECT set_config('workspace.projected_message_content', %s, true)",
            (f"{project_id}:{message_uuid}",),
        )
        yield
        session.execute(
            "SELECT set_config('workspace.projected_message_content', %s, true)",
            (previous or "",),
        )


class ConversionPendingError(Exception):
    """A referenced object has not finished synchronization yet."""


class ConversionContext:
    """Reuse identity catalogs across a batch; fetch only referenced messages."""

    def __init__(
        self,
        account: external_models.ExternalAccount,
        session: sessions.PgSQLSession | None = None,
    ) -> None:
        self.account = account
        self.session = session or contexts.Context().get_session()
        self.host = typing.cast(str, account.get_host())
        self.server_url = account.settings["server_url"].rstrip("/")
        self.namespace = uuid.uuid5(uuid.NAMESPACE_DNS, self.host)
        self.context: dict[str, typing.Any] = {
            "server_url": self.server_url,
            "strict_identities": True,
            "normalize_emoji": True,
            "mention_uuids": {},
            "user_ids": {},
            "stream_uuids": {},
            "stream_ids": {},
            "stream_urls": {},
            "dm_stream_uuids": set(),
            "zulip_namespace": str(self.namespace),
            "own_user_id": account.external_account_id.rsplit(":", 1)[1],
            "topic_uuids": {},
            "topic_urls": {},
            "message_uuids": {},
            "message_ids": {},
            "canonical_message_ids": {},
            "messages": {},
            "files": {},
        }
        self._remote_messages: set[int] = set()
        self._workspace_messages: set[uuid.UUID] = set()
        self._topic_references: set[tuple[str, str]] = set()
        self._message_versions: dict[str, datetime.datetime | None] = {}
        self._outgoing = False
        self._delivered: dict[int, dict[str, typing.Any]] = {}
        self._users()
        self._channels()

    def _users(self) -> None:
        rows = self.session.execute(
            """SELECT account.uuid, account.external_account_id, account.owner_user_uuid,
                      COALESCE(imported.first_name, owner.first_name, owner.username) AS name
               FROM m_external_accounts_v2 AS account
               JOIN m_workspace_users AS owner ON owner.uuid = account.owner_user_uuid
               LEFT JOIN m_workspace_users AS imported ON imported.uuid = messenger_uuid_v5(
                   %s, 'user:' || substring(account.external_account_id FROM %s))
               WHERE account.project_id = %s AND account.provider = 'zulip'
                 AND starts_with(account.external_account_id, %s)""",
            (
                self.namespace,
                len(self.host) + 2,
                self.account.project_id,
                self.host + ":",
            ),
        ).fetchall()
        self.account_ids = [row["uuid"] for row in rows]
        self.names = {}
        names: dict[str, set[str]] = {}
        owners: dict[str, list[tuple[str, str]]] = {}
        for row in rows:
            remote_id = row["external_account_id"].rsplit(":", 1)[1]
            owner = str(row["owner_user_uuid"])
            self.names[remote_id] = row["name"]
            self.context["mention_uuids"][f"id:{remote_id}"] = owner
            names.setdefault(row["name"], set()).add(owner)
            owners.setdefault(owner, []).append((remote_id, row["name"]))
        for name, matches in names.items():
            if len(matches) == 1:
                self.context["mention_uuids"][f"name:{name}"] = matches.pop()
        for owner, identities in owners.items():
            if len(identities) == 1:
                self.context["user_ids"][owner] = identities[0]

    def _channels(self) -> None:
        rows = self.session.execute(
            """SELECT DISTINCT channel.stream_uuid, channel.stream_id, channel.name,
                      channel.channel_type, peer.external_account_id AS peer_id,
                      channel.external_account_uuid = %s AS own
               FROM bridge_zulip_v5_channels AS channel
               LEFT JOIN m_external_accounts_v2 AS peer ON peer.uuid = channel.direct_user_uuid
               WHERE channel.external_account_uuid = ANY(%s)
               ORDER BY channel.stream_uuid, own""",
            (self.account.uuid, self.account_ids),
        ).fetchall()
        for row in rows:
            stream = str(row["stream_uuid"])
            if row["channel_type"] != "channel":
                self.context["dm_stream_uuids"].add(stream)
            if row["channel_type"] == "channel":
                self.context["stream_uuids"][row["name"]] = stream
                self.context["stream_ids"][row["name"]] = row["stream_id"]
                self.context["stream_urls"][stream] = (
                    f"{self.server_url}/#narrow/channel/{row['stream_id']}"
                )
            elif row["channel_type"] == "private" and row["peer_id"] is not None:
                remote_id = row["peer_id"].rsplit(":", 1)[1]
                self.context["stream_urls"][stream] = (
                    f"{self.server_url}/#narrow/dm/{remote_id}"
                )
        groups = self.session.execute(
            """SELECT channel.stream_uuid,
                      array_agg(DISTINCT split_part(account.external_account_id, ':', -1)::bigint) AS users
               FROM bridge_zulip_v5_channels AS channel
               JOIN messenger_stream_bindings AS binding
                 ON binding.project_id=%s AND binding.stream_uuid=channel.stream_uuid AND binding.active
               JOIN m_external_accounts_v2 AS account
                 ON account.project_id=binding.project_id AND account.owner_user_uuid=binding.user_uuid
                AND account.uuid=ANY(%s)
               WHERE channel.external_account_uuid=%s AND channel.channel_type='group'
               GROUP BY channel.stream_uuid""",
            (self.account.project_id, self.account_ids, self.account.uuid),
        ).fetchall()
        for row in groups:
            users = sorted(row["users"])
            if (
                uuid.uuid5(self.namespace, "dm:" + ",".join(map(str, users)))
                == row["stream_uuid"]
            ):
                peers = [
                    str(value)
                    for value in users
                    if str(value) != self.context["own_user_id"]
                ]
                self.context["stream_urls"][str(row["stream_uuid"])] = (
                    f"{self.server_url}/#narrow/dm/{','.join(peers)}-group"
                )

    def _topics(self, text: str) -> None:
        references = set()
        for value in re.findall(r"#\*\*([^*\n]+)\*\*", text):
            channel, separator, topic = value.partition(">")
            if separator and channel in self.context["stream_uuids"]:
                references.add(
                    (self.context["stream_uuids"][channel], re.sub(r"@\d+$", "", topic))
                )
        for stream_id, encoded in re.findall(
            r"channel/(\d+)[^/\s)]*/topic/([^/\s)]+)", text
        ):
            for name, remote_id in self.context["stream_ids"].items():
                if str(remote_id) == stream_id:
                    topic = urllib.parse.unquote(
                        re.sub(r"\.([\da-fA-F]{2})", r"%\1", encoded)
                    )
                    references.add((self.context["stream_uuids"][name], topic))
        references -= self._topic_references
        self._topic_references.update(references)
        identifiers = {
            uuid.UUID(value)
            for kind, value, _query in message_conversion.workspace_references(text)
            if kind == "topic"
        }
        if not references and not identifiers:
            return
        candidates = list(
            identifiers
            | {uuid.uuid5(uuid.UUID(stream), name) for stream, name in references}
        )
        streams = [uuid.UUID(stream) for stream, _name in references]
        names = [name or "General Topic" for _stream, name in references]
        topics = self.session.execute(
            """SELECT topic.uuid, topic.stream_uuid, topic.name, channel.name AS channel_name,
                      channel.stream_id, 0 AS source_order
               FROM messenger_topics AS topic
               JOIN bridge_zulip_v5_channels AS channel ON channel.stream_uuid = topic.stream_uuid
               WHERE topic.project_id = %s AND topic.deleted_at IS NULL
                 AND channel.external_account_uuid = ANY(%s)
                 AND (topic.uuid=ANY(%s) OR (topic.stream_uuid=ANY(%s) AND topic.name=ANY(%s)))
               UNION ALL
               SELECT topic.uuid, channel.stream_uuid, topic.name, channel.name, channel.stream_id, 1
               FROM bridge_zulip_v5_topics AS topic
               JOIN bridge_zulip_v5_channels AS channel ON channel.stream_id=topic.stream_id
                AND topic.uuid IN (messenger_uuid_v5(channel.stream_uuid, topic.name),
                    messenger_uuid_v5(channel.stream_uuid, ''))
               WHERE topic.uuid=ANY(%s) AND channel.external_account_uuid=ANY(%s)
               ORDER BY source_order""",
            (
                self.account.project_id,
                self.account_ids,
                candidates,
                streams,
                names,
                candidates,
                self.account_ids,
            ),
        ).fetchall()
        for row in topics:
            if row["stream_id"] is None:
                url = self.context["stream_urls"].get(str(row["stream_uuid"]))
                if url is not None:
                    self.context["topic_urls"][str(row["uuid"])] = url
                continue
            name = (
                "" if row["uuid"] == uuid.uuid5(row["stream_uuid"], "") else row["name"]
            )
            self.context["topic_uuids"][f"{row['channel_name']}>{name}"] = str(
                row["uuid"]
            )
            self.context["topic_urls"][str(row["uuid"])] = (
                f"{self.server_url}/#narrow/channel/{row['stream_id']}/topic/"
                + message_conversion.encode_hash(name)
            )

    def prefetch(self, messages: list[dict[str, typing.Any]]) -> None:
        """Resolve identities and deliveries once for the whole ordered batch."""
        text = "\n".join(
            message["content"] + f" /near/{message['id']}" for message in messages
        )
        self._topics(text)
        self._messages(text)
        ids = [message["id"] for message in messages]
        self._delivered.update(dict.fromkeys(ids))
        for row in self.session.execute(
            """SELECT delivery.zulip_message_id,message.payload->>'content' AS content,
                      message.content_updated_at
               FROM bridge_zulip_v5_deliveries AS delivery
               JOIN messenger_messages AS message
                 ON message.project_id=delivery.project_id AND message.uuid=delivery.message_uuid
               WHERE delivery.project_id=%s AND delivery.hostname=%s
                 AND delivery.zulip_message_id=ANY(%s)
                 AND message.deleted_at IS NULL AND message.payload->>'kind'='markdown'""",
            (self.account.project_id, self.host, ids),
        ).fetchall():
            self._delivered[row["zulip_message_id"]] = row

    def _messages(self, text: str) -> None:
        remote_ids = {
            int(value)
            for match in re.findall(r"(?:near|with)/(\d+)|@(\d+)\*\*", text)
            for value in match
            if value
        }
        workspace_ids = {
            uuid.UUID(value)
            for kind, value, _query in message_conversion.workspace_references(text)
            if kind in ("message", "quote")
        }
        remote_ids -= self._remote_messages
        workspace_ids -= self._workspace_messages
        if not remote_ids and not workspace_ids:
            return
        self._remote_messages.update(remote_ids)
        self._workspace_messages.update(workspace_ids)
        placements = []
        if workspace_ids:
            placements = self.session.execute(
                """SELECT message_uuid, uuid, legacy_public_uuid FROM messenger_message_placements
                   WHERE project_id=%s AND (uuid=ANY(%s) OR legacy_public_uuid=ANY(%s))""",
                (self.account.project_id, list(workspace_ids), list(workspace_ids)),
            ).fetchall()
        canonical_ids = [row["message_uuid"] for row in placements]
        rows = self.session.execute(
            """SELECT DISTINCT ON (observed.message_id)
                      observed.message_id, observed.sender_id, observed.timestamp,
                      content.zulip_content AS content, content.zulip_content_updated_at AS content_updated_at,
                      true AS original_available,
                      channel.stream_id,
                      channel.name AS channel_name, topic.name AS topic,
                      topic.uuid AS topic_uuid, channel.stream_uuid,
                      COALESCE(delivery.message_uuid, messenger_uuid_v5(observed.project_id,
                          observed.message_uuid::text)) AS canonical_uuid,
                      COALESCE(placement.uuid,
                          messenger_uuid_v5(messenger_uuid_v5(observed.project_id,
                              observed.message_uuid::text), 'zulip-placement')) AS public_uuid
               FROM bridge_zulip_v5_messages AS observed
               JOIN bridge_zulip_v5_message_contents AS content ON content.uuid = observed.content
               JOIN bridge_zulip_v5_channels AS channel ON channel.uuid = observed.channel
               JOIN bridge_zulip_v5_topics AS topic ON topic.uuid = observed.topic
               LEFT JOIN bridge_zulip_v5_deliveries AS delivery
                 ON delivery.project_id = observed.project_id AND delivery.hostname = %s
                AND delivery.zulip_message_id = observed.message_id
               LEFT JOIN messenger_message_placements AS placement
                 ON placement.project_id = observed.project_id
                AND placement.stream_uuid = channel.stream_uuid
                AND placement.message_uuid = COALESCE(delivery.message_uuid,
                    messenger_uuid_v5(observed.project_id, observed.message_uuid::text))
               WHERE observed.project_id = %s AND observed.external_account_uuid = ANY(%s)
                 AND (observed.message_id = ANY(%s) OR
                      messenger_uuid_v5(observed.project_id, observed.message_uuid::text)=ANY(%s))
                 AND ((NOT %s AND observed.external_account_uuid=%s) OR EXISTS (
                     SELECT 1 FROM messenger_api_user_messages_v1 AS visible
                     WHERE visible.project_id=observed.project_id AND visible.user_uuid=%s
                       AND visible.uuid=placement.uuid
                       AND visible.visible))
               ORDER BY observed.message_id, content.zulip_content_updated_at DESC,
                        observed.external_account_uuid = %s DESC, placement.uuid""",
            (
                self.host,
                self.account.project_id,
                self.account_ids,
                list(remote_ids),
                canonical_ids,
                self._outgoing,
                self.account.uuid,
                self.account.owner_user_uuid,
                self.account.uuid,
            ),
        ).fetchall()
        for row in rows:
            self._add_message(row)
        deliveries = self.session.execute(
            """SELECT delivery.zulip_message_id AS message_id,
                      split_part(delivery.external_account_id, ':', -1)::bigint AS sender_id,
                      EXTRACT(EPOCH FROM message.created_at)::bigint AS timestamp,
                      COALESCE(delivery.observed_zulip_content, snapshot.zulip_content, message.payload->>'content') AS content,
                      delivery.content_observed_at AS content_updated_at,
                      delivery.observed_zulip_content IS NOT NULL OR snapshot.zulip_content IS NOT NULL AS original_available,
                      channel.stream_id, channel.name AS channel_name,
                      COALESCE(delivery.topic_name, '') AS topic,
                      placement.topic_uuid, channel.stream_uuid,
                      delivery.message_uuid AS canonical_uuid,
                      placement.uuid AS public_uuid
               FROM bridge_zulip_v5_deliveries AS delivery
               JOIN messenger_messages AS message
                 ON message.project_id = delivery.project_id AND message.uuid = delivery.message_uuid
               JOIN messenger_message_placements AS placement
                 ON placement.project_id = delivery.project_id
                AND placement.message_uuid = delivery.message_uuid
                AND placement.stream_uuid = delivery.stream_uuid
               JOIN bridge_zulip_v5_channels AS channel
                 ON channel.external_account_uuid = delivery.external_account_uuid
                AND channel.stream_uuid = delivery.stream_uuid
               LEFT JOIN LATERAL (
                   SELECT zulip_content FROM bridge_zulip_v5_content_snapshots
                   WHERE delivery_uuid = delivery.uuid ORDER BY created_at DESC, uuid DESC LIMIT 1
               ) AS snapshot ON true
               WHERE delivery.project_id = %s AND delivery.hostname = %s
                 AND delivery.zulip_message_id IS NOT NULL AND message.deleted_at IS NULL
                 AND (delivery.zulip_message_id = ANY(%s) OR delivery.message_uuid=ANY(%s))
                 AND ((NOT %s AND delivery.external_account_uuid=%s) OR EXISTS (
                     SELECT 1 FROM messenger_api_user_messages_v1 AS visible
                     WHERE visible.project_id=delivery.project_id AND visible.user_uuid=%s
                       AND visible.uuid=placement.uuid
                       AND visible.visible))""",
            (
                self.account.project_id,
                self.host,
                list(remote_ids),
                canonical_ids,
                self._outgoing,
                self.account.uuid,
                self.account.owner_user_uuid,
            ),
        ).fetchall()
        for row in deliveries:
            self._add_message(row)
        for placement in placements:
            remote_id = self.context["canonical_message_ids"].get(
                str(placement["message_uuid"])
            )
            if remote_id is not None:
                self.context["message_ids"][str(placement["uuid"])] = remote_id
                if placement["legacy_public_uuid"] is not None:
                    self.context["message_ids"][
                        str(placement["legacy_public_uuid"])
                    ] = remote_id

    def _add_message(self, row: dict[str, typing.Any]) -> None:
        remote_id = str(row["message_id"])
        if str(row["sender_id"]) not in self.names:
            raise ConversionPendingError(
                "Waiting for the referenced author's Zulip identity"
            )
        previous = self._message_versions.get(remote_id)
        if previous is not None and (
            row["content_updated_at"] is None or row["content_updated_at"] < previous
        ):
            return
        self._message_versions[remote_id] = row["content_updated_at"]
        public_uuid = str(row["public_uuid"])
        self.context["message_uuids"][remote_id] = public_uuid
        self.context["message_ids"][public_uuid] = remote_id
        self.context["canonical_message_ids"][str(row["canonical_uuid"])] = remote_id
        self.context["messages"][remote_id] = {
            "id": row["message_id"],
            "content": row["content"],
            "original_available": row["original_available"],
            "type": "stream" if row["stream_id"] is not None else "private",
            "stream_id": row["stream_id"],
            "display_recipient": row["channel_name"],
            "subject": ""
            if row["topic_uuid"] == uuid.uuid5(row["stream_uuid"], "")
            else row["topic"],
            "sender_id": row["sender_id"],
            "sender_full_name": self.names[str(row["sender_id"])],
        }

    def _file_link(
        self, stream_uuid: uuid.UUID, file_uuid: uuid.UUID, url: str
    ) -> None:
        self.session.execute(
            """INSERT INTO bridge_zulip_v5_file_links
                   (uuid, project_id, hostname, stream_uuid, file_uuid, zulip_url)
               VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
            (
                uuid.uuid5(stream_uuid, f"{self.host}:{file_uuid}"),
                self.account.project_id,
                self.host,
                stream_uuid,
                file_uuid,
                url,
            ),
        )

    def _import_files(
        self,
        message: dict[str, typing.Any],
        stream_uuid: uuid.UUID,
        zulip: client.ZulipClient | None,
    ) -> None:
        deleted_files = self.context["deleted_files"] = set()
        inaccessible_files = self.context["inaccessible_files"] = set()
        unavailable_paths: dict[str, set[str]] = {}
        for url in message_conversion.get_zulip_files(
            message, self.context, include_quotes=True
        ):
            parsed = message_conversion.parse_url(url, self.server_url + "/")
            server = urllib.parse.urlsplit(self.server_url)
            if (
                parsed is None
                or (parsed.scheme, parsed.netloc)
                != (
                    server.scheme,
                    server.netloc,
                )
                or not parsed.path.startswith("/user_uploads/")
            ):
                continue
            path = parsed.path
            if path in unavailable_paths:
                unavailable_paths[path].add(url)
                continue
            row = self.session.execute(
                """SELECT file.* FROM bridge_zulip_v5_file_links AS link
                   JOIN m_workspace_files AS file ON file.uuid = link.file_uuid
                   WHERE link.project_id=%s AND link.hostname=%s AND link.stream_uuid=%s
                     AND link.zulip_url=%s""",
                (self.account.project_id, self.host, stream_uuid, path),
            ).fetchone()
            if row is None:
                stream = self.session.execute(
                    "SELECT uuid FROM messenger_streams WHERE project_id=%s AND uuid=%s AND deleted_at IS NULL",
                    (self.account.project_id, stream_uuid),
                ).fetchone()
                if stream is None:
                    raise ConversionPendingError(
                        "Waiting for the attachment's Workspace stream"
                    )
                try:
                    if zulip is None:
                        if not self.account.can_stream():
                            continue
                        downloader = client.ZulipClient(self.account)
                        try:
                            data, content_type = downloader.download_file(path)
                        finally:
                            downloader.close()
                    else:
                        data, content_type = zulip.download_file(path)
                except requests.HTTPError as error:
                    response = error.response
                    if response is None or response.status_code not in (403, 404, 410):
                        raise
                    unavailable = (
                        inaccessible_files
                        if response.status_code == 403
                        else deleted_files
                    )
                    unavailable_paths[path] = unavailable
                    unavailable.add(url)
                    continue
                file_uuid = uuid.uuid5(
                    self.account.project_id,
                    f"zulip-file:{self.host}:{stream_uuid}:{path}",
                )
                storage = file_storage.save_workspace_file(file_uuid, data)
                name = urllib.parse.unquote(path.rsplit("/", 1)[1])
                owner = uuid.UUID(
                    self.context["mention_uuids"][f"id:{message['sender_id']}"]
                )
                digest = hashlib.sha256(data).hexdigest()
                file_storage.save_workspace_file_metadata(
                    file_storage.WorkspaceFileMetadata(
                        uuid=file_uuid,
                        project_id=self.account.project_id,
                        stream_uuid=stream_uuid,
                        owner_uuid=owner,
                        name=name,
                        description="",
                        content_type=content_type,
                        size_bytes=len(data),
                        sha256=digest,
                        created_at=datetime.datetime.fromtimestamp(
                            message["timestamp"], datetime.timezone.utc
                        ),
                    ),
                    storage_type=storage.storage_type,
                )
                file = helpers.create_workspace_file(
                    self.account.project_id,
                    owner,
                    file_uuid,
                    session=self.session,
                    name=name,
                    description="",
                    stream_uuid=stream_uuid,
                    content_type=content_type,
                    size_bytes=len(data),
                    hash=digest,
                    storage_type=storage.storage_type,
                    storage_id=storage.storage_id,
                    storage_object_id=storage.storage_object_id,
                )
                row = dict(file)
                self._file_link(stream_uuid, file_uuid, path)
            kind = (
                "image"
                if row["content_type"].startswith("image/")
                else "video"
                if row["content_type"].startswith("video/")
                else "file"
            )
            self.context["files"][url] = f"urn:{kind}:{row['uuid']}"

    def to_workspace(
        self,
        message: dict[str, typing.Any],
        stream_uuid: uuid.UUID,
        zulip: client.ZulipClient | None,
    ) -> str:
        self._topics(message["content"])
        self._messages(message["content"] + f" /near/{message['id']}")
        self._import_files(message, stream_uuid, zulip)
        self.context["original_url"] = f"{self.server_url}/#narrow/near/{message['id']}"
        canonical = uuid.uuid5(
            self.account.project_id, str(uuid.uuid5(self.namespace, str(message["id"])))
        )
        self.context["current_message_uuid"] = self.context["message_uuids"].get(
            str(message["id"]), str(uuid.uuid5(canonical, "zulip-placement"))
        )
        return message_conversion.zulip_to_workspace(message, self.context)["content"]

    def delivered_content(self, message_id: int) -> dict[str, typing.Any] | None:
        if message_id in self._delivered:
            return self._delivered[message_id]
        return self.session.execute(
            """SELECT message.payload->>'content' AS content, message.content_updated_at
               FROM bridge_zulip_v5_deliveries AS delivery
               JOIN messenger_messages AS message
                 ON message.project_id=delivery.project_id AND message.uuid=delivery.message_uuid
               WHERE delivery.project_id=%s AND delivery.hostname=%s AND delivery.zulip_message_id=%s
                 AND message.deleted_at IS NULL AND message.payload->>'kind'='markdown'""",
            (self.account.project_id, self.host, message_id),
        ).fetchone()

    def to_zulip(
        self,
        payload: dict[str, typing.Any],
        stream_uuid: uuid.UUID,
        zulip: client.ZulipClient,
    ) -> str:
        self._outgoing = True
        text = message_conversion.workspace_reference_text(payload["content"])
        self._topics(text)
        self._messages(text)
        references = list(message_conversion.workspace_references(text))
        quoted = {
            self.context["message_ids"].get(value)
            for kind, value, _query in references
            if kind == "quote"
        } - {None}
        for remote_id in quoted:
            source = self.context["messages"][remote_id]
            if not source["original_available"]:
                (original,) = zulip.get_messages(int(remote_id), int(remote_id))
                source["content"] = original["content"]
                source["original_available"] = True
        unresolved = [
            uuid.UUID(value)
            for kind, value, _query in references
            if kind in ("message", "quote") and value not in self.context["message_ids"]
        ]
        if unresolved:
            pending = self.session.execute(
                """SELECT visible.uuid FROM messenger_api_user_messages_v1 AS visible
                   JOIN bridge_zulip_v5_channels AS channel ON channel.stream_uuid=visible.stream_uuid
                    AND channel.external_account_uuid=%s
                   WHERE visible.project_id=%s AND visible.user_uuid=%s AND visible.visible
                     AND visible.uuid=ANY(%s) LIMIT 1""",
                (
                    self.account.uuid,
                    self.account.project_id,
                    self.account.owner_user_uuid,
                    unresolved,
                ),
            ).fetchone()
            if pending is not None:
                raise ConversionPendingError(
                    "Waiting for the referenced message's Zulip identity"
                )
        for urn in message_conversion.get_workspace_files(payload, include_quotes=True):
            file_uuid = uuid.UUID(urn.split(":", 2)[2].partition("?")[0])
            file = self.session.execute(
                """SELECT file.* FROM m_workspace_visible_files_v1 AS file
                   WHERE file.uuid=%s AND (file.acl_mode='public' OR
                       (file.project_id=%s AND file.viewer_user_uuid=%s))""",
                (
                    file_uuid,
                    self.account.project_id,
                    self.account.owner_user_uuid,
                ),
            ).fetchone()
            if file is None:
                raise ValueError(f"Workspace attachment is not accessible: {file_uuid}")
            link = self.session.execute(
                """SELECT zulip_url FROM bridge_zulip_v5_file_links
                   WHERE project_id=%s AND hostname=%s AND stream_uuid=%s AND file_uuid=%s""",
                (self.account.project_id, self.host, stream_uuid, file_uuid),
            ).fetchone()
            if link is None:
                data = file_storage.read_workspace_file(
                    file_uuid,
                    storage_type=file["storage_type"],
                    storage_object_id=file["storage_object_id"],
                )
                url = zulip.upload_file(file["name"], data, file["content_type"])
                self._file_link(stream_uuid, file_uuid, url)
            else:
                url = link["zulip_url"]
            self.context["files"][url] = urn
        return message_conversion.workspace_to_zulip(payload, self.context)
