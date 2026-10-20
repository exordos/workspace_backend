# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

"""One-time legacy data conversion using the unmodified v5 import pipeline."""

import datetime
import hashlib
import json
import urllib.parse
import uuid

import requests
from restalchemy.common import contexts
from restalchemy.dm import filters

from migrations import zulip_upgrade_data
from workspace.bridges.zulip import client, message_conversion
from workspace.bridges.zulip.dm import models
from workspace.messenger_api.dm import external_models
from workspace.services.messenger_workers import v2_projection


def promote_observed(session):
    """Adopt converted rows only after v5 has a genuine provider observation."""
    session.execute("""
        -- Legacy provider DMs could omit the native peer FK. Use the owning
        -- reader's observed counterpart before v5 restores the stream model.
        UPDATE messenger_streams AS stream
        SET direct_user_uuid=peer.owner_user_uuid
        FROM bridge_zulip_v5_channels channel
        JOIN m_external_accounts_v2 account
          ON account.uuid=channel.external_account_uuid
        JOIN m_external_accounts_v2 peer
          ON peer.uuid=channel.direct_user_uuid AND peer.project_id=account.project_id
        WHERE stream.private AND stream.direct_user_uuid IS NULL
          AND channel.channel_type='private'
          AND stream.project_id=account.project_id
          AND stream.uuid=channel.stream_uuid
          AND stream.owner_uuid=account.owner_user_uuid
          AND EXISTS (SELECT 1 FROM workspace_zulip_v5_legacy_state mapping
                      WHERE mapping.kind='mapping' AND mapping.data->>'kind'='stream'
                        AND mapping.data->>'project_id'=stream.project_id::text
                        AND mapping.data->>'new_uuid'=stream.uuid::text);
        WITH observed AS (
            SELECT DISTINCT ON (account.project_id,channel.stream_uuid)
                   account.project_id,channel.stream_uuid,channel.invite_only
            FROM bridge_zulip_v5_channels channel
            JOIN m_external_accounts_v2 account ON account.uuid=channel.external_account_uuid
            ORDER BY account.project_id,channel.stream_uuid,channel.updated_at DESC,channel.uuid
        )
        UPDATE messenger_streams AS stream
        SET invite_only=observed.invite_only
        FROM observed
        WHERE stream.project_id=observed.project_id AND stream.uuid=observed.stream_uuid
          AND stream.invite_only IS DISTINCT FROM observed.invite_only
          AND EXISTS (SELECT 1 FROM workspace_zulip_v5_legacy_state mapping
                      WHERE mapping.kind='mapping' AND mapping.data->>'kind'='stream'
                        AND mapping.data->>'project_id'=stream.project_id::text
                        AND mapping.data->>'new_uuid'=stream.uuid::text);
        UPDATE messenger_streams AS stream
        SET source_name='zulip',source='{"kind":"zulip"}'::jsonb
        FROM workspace_zulip_v5_legacy_state AS mapping
        WHERE mapping.kind='mapping' AND mapping.data->>'kind'='stream'
          AND stream.project_id=(mapping.data->>'project_id')::uuid
          AND stream.uuid=(mapping.data->>'new_uuid')::uuid
          AND stream.source_name='native'
          AND EXISTS (SELECT 1 FROM bridge_zulip_v5_channels channel
                      JOIN m_external_accounts_v2 account ON account.uuid=channel.external_account_uuid
                      WHERE account.project_id=stream.project_id AND channel.stream_uuid=stream.uuid);
        UPDATE messenger_topics AS topic
        SET source_name='zulip',source='{"kind":"zulip"}'::jsonb
        FROM workspace_zulip_v5_legacy_state AS mapping
        WHERE mapping.kind='mapping' AND mapping.data->>'kind'='topic'
          AND topic.project_id=(mapping.data->>'project_id')::uuid
          AND topic.uuid=(mapping.data->>'new_uuid')::uuid
          AND topic.source_name='native'
          AND EXISTS (SELECT 1 FROM bridge_zulip_v5_messages observed
                      WHERE observed.project_id=topic.project_id AND observed.topic=topic.uuid);
        UPDATE messenger_messages AS message
        SET source_name='zulip',source='{"kind":"zulip"}'::jsonb,
            content_updated_at=LEAST(message.content_updated_at,(
                SELECT max(content.workspace_content_updated_at)
                FROM bridge_zulip_v5_messages observed
                JOIN bridge_zulip_v5_message_contents content ON content.uuid=observed.content
                WHERE observed.project_id=message.project_id
                  AND messenger_uuid_v5(observed.project_id,observed.message_uuid::text)=message.uuid))
        FROM workspace_zulip_v5_legacy_state AS mapping
        WHERE mapping.kind='mapping' AND mapping.data->>'kind'='message'
          AND message.project_id=(mapping.data->>'project_id')::uuid
          AND message.uuid=(mapping.data->>'new_uuid')::uuid
          AND message.source_name='native'
          AND EXISTS (SELECT 1 FROM bridge_zulip_v5_messages observed
                      WHERE observed.project_id=message.project_id
                        AND messenger_uuid_v5(observed.project_id,observed.message_uuid::text)=message.uuid);
    """)


LEGACY_NAMESPACE = uuid.UUID("9a1d0e75-50a5-413c-b3e8-d070232ef57f")


def load_state(session):
    saved = {}
    for row in session.execute(
        "SELECT kind,data FROM workspace_zulip_v5_legacy_state"
    ).fetchall():
        saved.setdefault(row["kind"], []).append(row["data"])
    return saved


def seed_file_links(session, account, messages, saved, downloader=None):
    """Reuse copied attachment objects by their original deterministic identity."""
    host = account.get_host()
    namespace = uuid.uuid5(uuid.NAMESPACE_DNS, host)
    stream_ids = {
        (item["project_id"], item["old_uuid"]): item["new_uuid"]
        for item in saved.get("mapping", [])
        if item["kind"] == "stream"
    }
    files = {item["uuid"]: item for item in saved.get("file", [])}
    chats = {}
    for item in saved.get("chat", []):
        if item["project_id"] is None or item["projection_stream_uuid"] is None:
            continue
        stream = stream_ids.get(
            (item["project_id"], item["projection_stream_uuid"]),
            item["projection_stream_uuid"],
        )
        chats.setdefault(stream, []).append(item)
    linked = 0
    for message in messages:
        if message["type"] == "stream":
            stream = uuid.uuid5(namespace, str(message["stream_id"]))
        else:
            members = ",".join(
                str(person["id"])
                for person in sorted(
                    message["display_recipient"], key=lambda person: person["id"]
                )
            )
            stream = uuid.uuid5(namespace, "dm:" + members)
        for url in message_conversion.get_zulip_files(message, {}, include_quotes=True):
            parsed = message_conversion.parse_url(
                url, account.settings["server_url"].rstrip("/") + "/"
            )
            if (
                parsed is None
                or parsed.hostname != host
                or not parsed.path.startswith("/user_uploads/")
            ):
                continue
            existing = session.execute(
                "SELECT uuid FROM bridge_zulip_v5_file_links WHERE project_id=%s AND hostname=%s AND stream_uuid=%s AND zulip_url=%s",
                (account.project_id, host, stream, parsed.path),
            ).fetchone()
            if existing is not None:
                continue
            urls = {
                url,
                parsed.path,
                urllib.parse.urljoin(
                    account.settings["server_url"].rstrip("/") + "/", parsed.path
                ),
            }
            candidates = set()
            for chat in chats.get(str(stream), []):
                for source in urls:
                    candidates.add(
                        uuid.uuid5(
                            LEGACY_NAMESPACE,
                            f"zulip-file:{chat['external_account_uuid']}:{chat['uuid']}:{source}",
                        )
                    )
            matching = [
                candidate
                for candidate in sorted(candidates, key=str)
                if str(candidate) in files
            ]
            reusable = None
            for candidate in matching:
                row = session.execute(
                    "SELECT uuid FROM m_workspace_files WHERE uuid=%s AND project_id=%s AND stream_uuid=%s AND acl_mode='stream'",
                    (candidate, account.project_id, stream),
                ).fetchone()
                if row is not None:
                    reusable = candidate
                    break
            if reusable is None and downloader is not None:
                # Older importers used another UUID formula. Verify byte identity
                # before reusing an object, without writing another S3 copy.
                try:
                    digest, size = _file_fingerprint(account, downloader, parsed.path)
                except requests.HTTPError as error:
                    if error.response is None or error.response.status_code not in (
                        403,
                        404,
                        410,
                    ):
                        raise
                    continue
                row = session.execute(
                    """SELECT file.uuid FROM m_workspace_files file
                    WHERE file.project_id=%s AND file.stream_uuid=%s AND file.acl_mode='stream'
                      AND file.hash=%s AND file.size_bytes=%s
                      AND EXISTS(SELECT 1 FROM workspace_zulip_v5_legacy_state legacy
                                 WHERE legacy.kind='file' AND legacy.identity_key=file.uuid::text)
                    ORDER BY file.uuid LIMIT 1""",
                    (account.project_id, stream, digest, size),
                ).fetchone()
                if row is not None:
                    reusable = row["uuid"]
            if reusable is not None:
                session.execute(
                    "INSERT INTO bridge_zulip_v5_file_links(uuid,project_id,hostname,stream_uuid,file_uuid,zulip_url) VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                    (
                        uuid.uuid5(stream, f"{host}:{reusable}"),
                        account.project_id,
                        host,
                        stream,
                        reusable,
                        parsed.path,
                    ),
                )
                linked += 1
    return linked


def _file_fingerprint(account, downloader, path):
    temporary = downloader._get(path)["url"]
    url = urllib.parse.urljoin(
        account.settings["server_url"].rstrip("/") + "/", temporary
    )
    digest = hashlib.sha256()
    size = 0
    # The temporary object URL uses its own authorization. Provider credentials
    # stay inside the existing client and are never forwarded to object storage.
    with requests.Session() as transport:
        transport.trust_env = False
        with transport.get(url, stream=True, timeout=(5, 30)) as response:
            response.raise_for_status()
            for chunk in response.iter_content(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
    return digest.hexdigest(), size


def _content(account, content, label, *, prepared=None, saved=None, downloader=None):
    """Commit a retryable installation step with its normal v5 task record."""
    with contexts.Context().session_manager() as session:
        task_uuid = uuid.uuid5(account.uuid, "legacy-upgrade:" + label)
        task = models.Task.objects.get_one_or_none(
            filters={"uuid": filters.EQ(task_uuid)}
        )
        if task is not None and task.status == models.TaskStatus.DONE.value:
            return
        if task is None:
            task = models.Task(
                uuid=task_uuid,
                project_id=account.project_id,
                external_account_uuid=account.uuid,
                content=content,
            )
            task.insert()
        if prepared is not None:
            content._prepared_import = prepared
            release_misattributed_deliveries(session, account, prepared[2], saved)
            seed_file_links(session, account, prepared[2], saved, downloader)
        content.execute(task)
        content.on_done(task)
        task.status = models.TaskStatus.DONE.value
        task.update()
        # Defer reconciliation until every legacy reader's observations exist.
        # Otherwise the first account would erase other readers' local state.
        for state in models.ZulipProjectUserState.objects.get_all(limit=1000):
            state.do()
        for state in models.ZulipStreamState.objects.get_all(
            filters={"action": filters.EQ("create")}, limit=1000
        ):
            state.do()


def migrate():
    with contexts.Context().session_manager() as session:
        if (
            session.execute(
                "SELECT to_regclass('public.workspace_zulip_v5_legacy_state') AS name"
            ).fetchone()["name"]
            is None
        ):
            return 0
        # Resume an interrupted installation with the same data conversion,
        # including provider-linked rows previously labelled native.
        zulip_upgrade_data.prepare(session)
        saved = load_state(session)
        accounts = external_models.ExternalAccount.objects.get_all(
            filters={"provider": filters.EQ("zulip")}
        )
        account_ids = {uuid.UUID(item["uuid"]) for item in saved.get("account", [])}
        accounts = [
            account
            for account in accounts
            if account.uuid in account_ids
            and not account.disabled
            and bool(account.settings.get("api_key"))
        ]
    for account in accounts:
        _content(account, models.InitializeConnectionContent(), "initialize")
    for account in accounts:
        _content(account, models.ImportUsersContent(), "users")
        _content(account, models.ImportChannelsContent(), "channels")
    # Seed original outgoing identities before observing their echoes. The
    # installer never dispatches outgoing tasks or replays old send operations.
    with contexts.Context().session_manager() as session:
        seed_deliveries(session, saved)
    for account in accounts:
        with contexts.Context().session_manager():
            account = external_models.ExternalAccount.objects.get_one(
                filters={"uuid": filters.EQ(account.uuid)}
            )
        zulip = client.ZulipClient(account)
        try:
            last = zulip.get_last_message_id()
            for first in range(1, last + 1, 100):
                end = min(first + 99, last)
                label = f"history:{first}:{end}"
                with contexts.Context().session_manager():
                    done = models.Task.objects.get_one_or_none(
                        filters={
                            "uuid": filters.EQ(
                                uuid.uuid5(account.uuid, "legacy-upgrade:" + label)
                            ),
                            "status": filters.EQ(models.TaskStatus.DONE.value),
                        }
                    )
                if done is not None:
                    continue
                observed_at = datetime.datetime.now(datetime.timezone.utc)
                messages = zulip.get_messages(first, end)
                _content(
                    account,
                    models.ImportHistoryContent(
                        first_message_id=first, last_message_id=end
                    ),
                    label,
                    prepared=(account.realtime_revision(), observed_at, messages),
                    saved=saved,
                    downloader=zulip,
                )
        finally:
            zulip.close()
    reconcile_converted_data()
    refresh_derived_data(saved)
    with contexts.Context().session_manager() as session:
        for account in accounts:
            completed = external_models.ExternalAccount.objects.get_one(
                filters={
                    "uuid": filters.EQ(account.uuid),
                    "project_id": filters.EQ(account.project_id),
                }
            )
            completed.update_status(external_models.ExternalAccountStatus.ACTIVE.value)
        session.execute(
            "UPDATE m_workspace_event_cursors SET epoch_generation=gen_random_uuid()"
        )
        # Keep the original authors, payloads, UUIDs, placements and conversion
        # mapping for audit and recovery; account API keys were never captured.
        session.execute(
            "ALTER TABLE workspace_zulip_v5_legacy_state RENAME TO workspace_zulip_v5_legacy_archive"
        )
    return len(accounts)


def seed_deliveries(session, saved):
    """Retain acknowledged outgoing provider IDs so history cannot resend them."""
    mapping = {
        (item["kind"], item["project_id"], item["old_uuid"]): item["new_uuid"]
        for item in saved.get("mapping", [])
    }
    topics = {
        (item["project_id"], item["uuid"]): item for item in saved.get("topic", [])
    }
    accounts = {
        str(account.uuid): account
        for account in external_models.ExternalAccount.objects.get_all(
            filters={"provider": filters.EQ("zulip")}
        )
    }
    chats = {}
    for chat in saved.get("chat", []):
        if chat["projection_stream_uuid"] is not None:
            chats.setdefault(
                (chat["project_id"], chat["projection_stream_uuid"]), []
            ).append(chat)
    for message in saved.get("message", []):
        if message["source_name"] != "native":
            continue
        for placement in message["placements"]:
            key = (message["project_id"], placement["stream_uuid"])
            for chat in chats.get(key, []):
                account = accounts.get(chat["external_account_uuid"])
                if (
                    account is None
                    or str(account.owner_user_uuid) != message["author_uuid"]
                    or account.external_account_id is None
                ):
                    continue
                kind, identifier = chat["provider_chat_id"].split(":", 1)
                recipient = (
                    int(identifier)
                    if kind == "channel"
                    else sorted(
                        {
                            int(person["provider_user_id"])
                            for person in chat["source"]["participants"]
                        }
                    )
                )
                topic = topics[(message["project_id"], placement["topic_uuid"])]
                stream_uuid = uuid.UUID(
                    mapping.get(
                        ("stream", message["project_id"], placement["stream_uuid"]),
                        placement["stream_uuid"],
                    )
                )
                session.execute(
                    """INSERT INTO bridge_zulip_v5_deliveries
                    (uuid,project_id,message_uuid,stream_uuid,external_account_uuid,hostname,
                     external_account_id,recipient,topic_name,zulip_message_id,delivered_at)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,jsonb_build_object('to',%s::jsonb),%s,%s,%s)
                    ON CONFLICT DO NOTHING""",
                    (
                        uuid.uuid5(
                            uuid.UUID(message["uuid"]),
                            "legacy-delivery:" + str(stream_uuid),
                        ),
                        message["project_id"],
                        mapping.get(
                            ("message", message["project_id"], message["uuid"]),
                            message["uuid"],
                        ),
                        stream_uuid,
                        account.uuid,
                        account.get_host(),
                        account.external_account_id,
                        json.dumps(recipient),
                        topic["source"].get("topic_name", topic["name"])
                        if kind == "channel"
                        else None,
                        message["provider_message_id"],
                        message["updated_at"],
                    ),
                )


def release_misattributed_deliveries(session, account, messages, saved):
    """Check factual senders by legacy delivery PK, independent of table stats."""
    if not messages:
        return
    if "_delivery_guesses" not in saved:
        streams = {
            (item["project_id"], item["old_uuid"]): item["new_uuid"]
            for item in saved.get("mapping", [])
            if item["kind"] == "stream"
        }
        guesses = {}
        for message in saved.get("message", []):
            if message["source_name"] != "native":
                continue
            key = (message["project_id"], str(message["provider_message_id"]))
            for placement in message["placements"]:
                stream = streams.get(
                    (message["project_id"], placement["stream_uuid"]),
                    placement["stream_uuid"],
                )
                guesses.setdefault(key, set()).add(
                    uuid.uuid5(uuid.UUID(message["uuid"]), "legacy-delivery:" + stream)
                )
        saved["_delivery_guesses"] = guesses
    host = account.get_host()
    for message in messages:
        for delivery in saved["_delivery_guesses"].get(
            (str(account.project_id), str(message["id"])), ()
        ):
            session.execute(
                """DELETE FROM bridge_zulip_v5_deliveries
                WHERE uuid=%s AND project_id=%s AND hostname=%s
                  AND zulip_message_id=%s AND external_account_id<>%s""",
                (
                    delivery,
                    account.project_id,
                    host,
                    message["id"],
                    host + ":" + str(message["sender_id"]),
                ),
            )


def reconcile_converted_data():
    with contexts.Context().session_manager() as session:
        promote_observed(session)
        for state in models.ZulipProjectUserState.objects.get_all(limit=10000):
            state.do()
        for state in models.ZulipStreamState.objects.get_all(limit=10000):
            state.do()
        for state in models.ZulipStreamBindingState.objects.get_all(limit=10000):
            state.do()
    with contexts.Context().session_manager() as session:
        topic_budget = (
            1000
            + 4
            * session.execute(
                "SELECT count(*) AS n FROM bridge_zulip_v5_topic_changes"
            ).fetchone()["n"]
        )
        message_budget = (
            1000
            + 4
            * session.execute(
                "SELECT (SELECT count(*) FROM bridge_zulip_v5_message_changes)+(SELECT count(*) FROM messenger_message_changes) AS n"
            ).fetchone()["n"]
        )
    while True:
        with contexts.Context().session_manager():
            count = models.ZulipTopicState.reconcile_batch(1000)
        if not count:
            break
        topic_budget -= count
        if topic_budget < 0:
            raise RuntimeError("Legacy topic conversion did not converge")
    while True:
        with contexts.Context().session_manager():
            count = models.ZulipMessageState.reconcile_batch(100)
        if not count:
            break
        message_budget -= count
        if message_budget < 0:
            raise RuntimeError("Legacy message conversion did not converge")


def refresh_derived_data(saved):
    """Rebuild caches from preserved facts using the existing v5 projections."""
    account_ids = [uuid.UUID(item["uuid"]) for item in saved.get("account", [])]
    with contexts.Context().session_manager() as session:
        projects = session.execute(
            "SELECT DISTINCT project_id FROM m_external_accounts_v2 WHERE uuid=ANY(%s)",
            (account_ids,),
        ).fetchall()
    for row in projects:
        project = row["project_id"]
        with contexts.Context().session_manager() as session:
            session.execute(
                "DELETE FROM messenger_message_counter_totals WHERE project_id=%s",
                (project,),
            )
            session.execute(
                "DELETE FROM messenger_message_counter_contributions WHERE project_id=%s",
                (project,),
            )
            session.execute(
                "DELETE FROM messenger_message_counter_changes WHERE project_id=%s",
                (project,),
            )
            session.execute(
                "INSERT INTO messenger_message_counter_contributions SELECT * FROM messenger_current_message_counter_contributions WHERE project_id=%s",
                (project,),
            )
            session.execute(
                """INSERT INTO messenger_message_counter_totals
                SELECT project_id,user_uuid,stream_uuid,topic_uuid,membership_generation,membership_started_at,
                       count(*),count(*) FILTER(WHERE mentioned)
                FROM messenger_message_counter_contributions WHERE project_id=%s
                GROUP BY project_id,user_uuid,stream_uuid,topic_uuid,membership_generation,membership_started_at""",
                (project,),
            )
            streams = session.execute(
                "SELECT stream_uuid,array_agg(user_uuid) AS users FROM messenger_stream_bindings WHERE project_id=%s AND active GROUP BY stream_uuid",
                (project,),
            ).fetchall()
            topics = session.execute(
                """SELECT topic.stream_uuid,binding.topic_uuid,array_agg(binding.user_uuid) AS users
                FROM messenger_user_topic_bindings binding JOIN messenger_topics topic
                  ON topic.project_id=binding.project_id AND topic.uuid=binding.topic_uuid
                WHERE binding.project_id=%s GROUP BY topic.stream_uuid,binding.topic_uuid""",
                (project,),
            ).fetchall()
            folders = session.execute(
                "SELECT user_uuid,folder_uuid FROM messenger_user_folder_bindings WHERE project_id=%s",
                (project,),
            ).fetchall()
        for stream in streams:
            with contexts.Context().session_manager() as session:
                v2_projection._refresh_recipient_counters(
                    session,
                    project,
                    stream["stream_uuid"],
                    None,
                    stream["users"],
                    "user-stream",
                )
        for topic in topics:
            with contexts.Context().session_manager() as session:
                v2_projection._refresh_recipient_counters(
                    session,
                    project,
                    topic["stream_uuid"],
                    topic["topic_uuid"],
                    topic["users"],
                    "user-topic",
                )
        for folder in folders:
            with contexts.Context().session_manager() as session:
                v2_projection._process_folder_projection(
                    session,
                    {
                        "project_id": project,
                        "payload": {
                            "user_uuid": str(folder["user_uuid"]),
                            "folder_uuid": str(folder["folder_uuid"]),
                            "source_kind": "migration",
                            "emit_public_event": False,
                        },
                    },
                )
