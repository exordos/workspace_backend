import datetime
import hashlib
import json
import mimetypes
import urllib.parse
import uuid

from workspace_providers.common import daemon
from workspace_providers.common import models
from workspace_providers.common import reconciliation
from workspace_providers.zulip import codec
from workspace_providers.zulip import protocol


class ZulipProviderDaemon(daemon.ProviderDaemon):
    provider_kind = models.ProviderKind.ZULIP
    provider_domain = models.ProviderDomain.MESSENGER
    message_page_size = 100

    def __init__(
        self,
        *args,
        client_class=protocol.ZulipClient,
        scheduler: reconciliation.DynamicReconciliationScheduler | None = None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.client_class = client_class
        self.scheduler = scheduler or reconciliation.DynamicReconciliationScheduler()

    @staticmethod
    def _urn_uuid(value: str) -> uuid.UUID:
        return uuid.UUID(value.rsplit(":", 1)[-1])

    def _realm_scope(self, account) -> uuid.UUID:
        parsed = urllib.parse.urlsplit(account.settings.get("server_url", ""))
        normalized_url = urllib.parse.urlunsplit(
            (
                parsed.scheme.lower(),
                parsed.netloc.lower(),
                parsed.path.rstrip("/"),
                "",
                "",
            ),
        )
        return uuid.uuid5(self.provider_uuid, f"zulip-realm:{normalized_url}")

    def _get_entity_map(self, account, entity_kind, external_key):
        return self.repository.get_entity_map(
            self._realm_scope(account),
            entity_kind,
            external_key,
        )

    def _get_entity_map_by_urn(self, account, entity_kind, workspace_urn):
        return self.repository.get_entity_map_by_urn(
            self._realm_scope(account),
            entity_kind,
            workspace_urn,
        )

    def _list_entity_maps(self, account, entity_kind):
        return self.repository.list_entity_maps(
            self._realm_scope(account),
            entity_kind,
        )

    def _save_entity_map(
        self,
        account,
        entity_kind,
        external_key,
        workspace_urn,
        **kwargs,
    ):
        return self.repository.save_entity_map(
            self._realm_scope(account),
            entity_kind,
            external_key,
            workspace_urn,
            **kwargs,
        )

    def _replace_entity_map(
        self,
        account,
        entity_kind,
        external_key,
        workspace_urn,
        provider_payload=None,
    ):
        return self.repository.replace_entity_map(
            self._realm_scope(account),
            entity_kind,
            external_key,
            workspace_urn,
            provider_payload,
        )

    def _delete_entity_map(self, account, entity_kind, external_key):
        return self.repository.delete_entity_map(
            self._realm_scope(account),
            entity_kind,
            external_key,
        )

    def _mapped_entity_uuid(
        self,
        account,
        entity_kind: str,
        external_id: str,
    ) -> uuid.UUID | None:
        mapping = self.repository.get_entity_map(
            self._realm_scope(account),
            entity_kind,
            external_id,
        )
        if mapping is None:
            return uuid.uuid5(
                self.provider_uuid,
                ":".join(
                    (
                        str(self._realm_scope(account)),
                        "messenger",
                        entity_kind,
                        external_id,
                    ),
                ),
            )
        return self._urn_uuid(mapping["workspace_urn"])

    def _upsert_user(self, account, raw_user):
        external_id = str(raw_user.get("user_id") or raw_user.get("sender_id"))
        full_name = raw_user.get("full_name") or raw_user.get("sender_full_name") or ""
        first_name, _, last_name = full_name.partition(" ")
        email = raw_user.get("email") or raw_user.get("sender_email")
        reference = self.client.upsert_messenger(
            "users",
            external_id,
            account.uuid,
            {
                "username": email or f"zulip-{external_id}",
                "first_name": first_name or None,
                "last_name": last_name or None,
                "email": email,
                "status": "active",
            },
            entity_uuid=self._mapped_entity_uuid(
                account,
                "user",
                external_id,
            ),
        )
        self._save_entity_map(account, "user", external_id, reference.urn)
        return reference.urn

    def _sync_streams(self, account, zulip, owner_urn):
        streams = zulip.streams()
        remote_ids = set()
        for stream in streams:
            external_id = str(stream["stream_id"])
            remote_ids.add(external_id)
            reference = self.client.upsert_messenger(
                "streams",
                external_id,
                account.uuid,
                {
                    "owner_urn": owner_urn,
                    "name": stream["name"],
                    "description": stream.get("description", ""),
                    "invite_only": bool(stream.get("invite_only")),
                    "private": False,
                    "is_archived": bool(stream.get("is_archived")),
                },
                entity_uuid=self._mapped_entity_uuid(
                    account,
                    "stream",
                    external_id,
                ),
            )
            self._save_entity_map(
                account,
                "stream",
                external_id,
                reference.urn,
                provider_payload={"kind": "stream"},
            )
        for row in self._list_entity_maps(account, "stream"):
            if row["provider_payload"].get("kind") == "direct":
                continue
            if row["external_key"] in remote_ids:
                continue
            self.client.delete_entity(
                models.ProviderDomain.MESSENGER,
                "streams",
                row["external_key"],
                account.uuid,
                entity_uuid=self._urn_uuid(row["workspace_urn"]),
            )
            self._delete_entity_map(account, "stream", row["external_key"])
        return len(streams)

    def _sync_users(self, account, zulip):
        for raw_user in zulip.users():
            self._upsert_user(account, raw_user)

    def _import_file(self, account, zulip, remote_url, display_name):
        mapping = self._get_entity_map(account, "file", remote_url)
        if mapping is not None:
            return mapping["workspace_urn"]
        data, content_type, remote_name = zulip.download_file(remote_url)
        name = display_name or remote_name
        reference = self.client.upload_blob(
            account.uuid,
            name,
            content_type,
            data,
            hashlib.sha256(data).hexdigest(),
        )
        self._save_entity_map(
            account,
            "file",
            remote_url,
            reference.urn,
            provider_payload={
                "name": name,
                "content_type": content_type,
            },
        )
        return reference.urn

    def _export_file(self, account, zulip, workspace_urn, display_name):
        mapping = self._get_entity_map_by_urn(account, "file", workspace_urn)
        if mapping is not None:
            return mapping["external_key"]
        name = display_name or "attachment"
        content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        remote_url = zulip.upload_file(
            name,
            content_type,
            self.client.download_blob(workspace_urn),
        )
        self._save_entity_map(
            account,
            "file",
            remote_url,
            workspace_urn,
            provider_payload={
                "name": name,
                "content_type": content_type,
            },
        )
        return remote_url

    def _sync_message(self, account, raw_message, zulip=None):
        def resolve_file(remote_url, display_name):
            nonlocal zulip
            if zulip is None:
                zulip = self.client_class(account.settings)
            return self._import_file(account, zulip, remote_url, display_name)

        normalized = codec.normalize_message(
            raw_message,
            account.settings["server_url"],
            resolve_file,
        )
        author_urn = self._upsert_user(account, raw_message)
        stream = self._get_entity_map(
            account, "stream", normalized["stream_external_id"]
        )
        if stream is None and normalized["is_direct"]:
            participant_urns = {}
            participants_by_id = {}
            for recipient in normalized["recipients"]:
                if not isinstance(recipient, dict) or recipient.get("id") is None:
                    continue
                recipient_id = int(recipient["id"])
                participants_by_id[recipient_id] = recipient
                participant_urns[recipient_id] = self._upsert_user(
                    account,
                    {
                        "user_id": recipient["id"],
                        "full_name": recipient.get("full_name", ""),
                        "email": recipient.get("email"),
                    },
                )
            login = account.settings.get("credentials", {}).get("login")
            current_user_id = next(
                (
                    user_id
                    for user_id, recipient in participants_by_id.items()
                    if login is not None and recipient.get("email") == login
                ),
                int(raw_message["sender_id"]),
            )
            owner_urn = participant_urns.get(current_user_id, author_urn)
            direct_user_urn = None
            if len(participant_urns) == 2:
                peer_urns = [
                    urn
                    for user_id, urn in participant_urns.items()
                    if user_id != current_user_id
                ]
                if peer_urns:
                    direct_user_urn = peer_urns[0]
            outbound_recipient_ids = [
                user_id
                for user_id in normalized["recipient_ids"]
                if user_id != current_user_id
            ]
            stream_payload = {
                "owner_urn": owner_urn,
                "name": ", ".join(
                    recipient.get("full_name") or recipient.get("email") or "User"
                    for recipient in normalized["recipients"]
                    if isinstance(recipient, dict)
                )
                or "Direct message",
                "description": "",
                "invite_only": True,
                "private": True,
                "is_archived": False,
            }
            if direct_user_urn is not None:
                stream_payload["direct_user_urn"] = direct_user_urn
            stream_reference = self.client.upsert_messenger(
                "streams",
                normalized["stream_external_id"],
                account.uuid,
                stream_payload,
                entity_uuid=self._mapped_entity_uuid(
                    account,
                    "stream",
                    normalized["stream_external_id"],
                ),
            )
            self._save_entity_map(
                account,
                "stream",
                normalized["stream_external_id"],
                stream_reference.urn,
                provider_payload={
                    "kind": "direct",
                    "recipient_ids": outbound_recipient_ids,
                },
            )
            stream = self._get_entity_map(
                account,
                "stream",
                normalized["stream_external_id"],
            )
        if stream is None:
            return None
        topic_id = normalized["topic_external_id"]
        topic = self.client.upsert_messenger(
            "topics",
            topic_id,
            account.uuid,
            {
                "stream_urn": stream["workspace_urn"],
                "name": "private"
                if normalized["is_direct"]
                else raw_message.get("subject", ""),
            },
            entity_uuid=self._mapped_entity_uuid(
                account,
                "topic",
                topic_id,
            ),
        )
        self._save_entity_map(
            account,
            "topic",
            topic_id,
            topic.urn,
            provider_payload={
                "name": "private"
                if normalized["is_direct"]
                else raw_message.get("subject", ""),
            },
        )
        message_id = normalized["external_message_id"]
        message = self.client.upsert_messenger(
            "messages",
            message_id,
            account.uuid,
            {
                "stream_urn": stream["workspace_urn"],
                "topic_urn": topic.urn,
                "author_urn": author_urn,
                "payload": normalized["payload"],
                "created_at": datetime.datetime.fromtimestamp(
                    normalized["created_at"], tz=datetime.timezone.utc
                ).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            },
            entity_uuid=self._mapped_entity_uuid(
                account,
                "message",
                message_id,
            ),
        )
        self._save_entity_map(account, "message", message_id, message.urn)
        self.client.sync_messenger_message_flags(
            self._urn_uuid(message.urn),
            account.uuid,
            read="read" in normalized["flags"],
            starred="starred" in normalized["flags"],
        )
        remote_reactions = set()
        for raw_reaction in raw_message.get("reactions", []):
            user_id = str(raw_reaction["user_id"])
            author = self._get_entity_map(account, "user", user_id)
            if author is None:
                author_urn = self._upsert_user(
                    account,
                    {
                        "user_id": user_id,
                        "full_name": raw_reaction.get("user", {}).get(
                            "full_name", f"Zulip user {user_id}"
                        ),
                        "email": raw_reaction.get("user", {}).get("email"),
                    },
                )
            else:
                author_urn = author["workspace_urn"]
            external_reaction_id = (
                f"{message_id}:{user_id}:{raw_reaction['emoji_name']}"
            )
            remote_reactions.add(external_reaction_id)
            reaction = self.client.upsert_messenger(
                "reactions",
                external_reaction_id,
                account.uuid,
                {
                    "message_urn": message.urn,
                    "author_urn": author_urn,
                    "emoji_name": raw_reaction["emoji_name"],
                },
                entity_uuid=self._mapped_entity_uuid(
                    account,
                    "reaction",
                    external_reaction_id,
                ),
            )
            self._save_entity_map(
                account,
                "reaction",
                external_reaction_id,
                reaction.urn,
                provider_payload={"message_id": message_id},
            )
        for row in self._list_entity_maps(account, "reaction"):
            if row["provider_payload"].get("message_id") != message_id:
                continue
            if row["external_key"] in remote_reactions:
                continue
            self.client.delete_entity(
                models.ProviderDomain.MESSENGER,
                "reactions",
                row["external_key"],
                account.uuid,
                entity_uuid=self._urn_uuid(row["workspace_urn"]),
            )
            self._delete_entity_map(account, "reaction", row["external_key"])
        return int(message_id)

    def _delete_message(self, account, message_id):
        external_id = str(message_id)
        self.client.delete_entity(
            models.ProviderDomain.MESSENGER,
            "messages",
            external_id,
            account.uuid,
            entity_uuid=self._mapped_entity_uuid(
                account,
                "message",
                external_id,
            ),
        )
        self._delete_entity_map(account, "message", external_id)
        for row in self._list_entity_maps(account, "reaction"):
            if row["provider_payload"].get("message_id") == external_id:
                self._delete_entity_map(account, "reaction", row["external_key"])

    def _process_events(self, account, zulip, events, owner_urn):
        last_message_id = 0
        for event in events:
            event_type = event.get("type")
            if event_type == "message":
                message_id = self._sync_message(account, event["message"], zulip)
                last_message_id = max(last_message_id, message_id or 0)
            elif event_type in (
                "update_message",
                "update_message_flags",
                "reaction",
            ):
                message_id = int(
                    event.get("message_id") or event.get("message", {}).get("id")
                )
                self._sync_message(account, zulip.message(message_id), zulip)
                last_message_id = max(last_message_id, message_id)
            elif event_type == "delete_message":
                message_id = int(event["message_id"])
                self._delete_message(account, message_id)
                last_message_id = max(last_message_id, message_id)
            elif event_type in ("subscription", "stream"):
                self._sync_streams(account, zulip, owner_urn)
            elif event_type == "realm_user" and event.get("person"):
                self._upsert_user(account, event["person"])
        return last_message_id

    def _message_history(self, zulip, anchor="newest", max_messages=None):
        forward = anchor != "newest"
        first_message_id = int(anchor) if forward else None
        page_anchor = anchor
        messages_by_id = {}
        while max_messages is None or len(messages_by_id) < max_messages:
            page_size = self.message_page_size
            page = zulip.messages(
                page_anchor,
                limit=page_size,
                before=0 if forward else page_size,
                after=page_size if forward else 0,
            )
            if not page:
                break
            previous_count = len(messages_by_id)
            for message in page:
                message_id = int(message["id"])
                if first_message_id is not None and message_id <= first_message_id:
                    continue
                messages_by_id[message_id] = message
            if len(messages_by_id) == previous_count:
                break
            page_anchor = (
                max(int(message["id"]) for message in page)
                if forward
                else min(int(message["id"]) for message in page)
            )
            if len(page) < page_size:
                break
        ordered = [messages_by_id[key] for key in sorted(messages_by_id)]
        if max_messages is None:
            return ordered
        return ordered[:max_messages] if forward else ordered[-max_messages:]

    def _reconcile_history(self, account, zulip, partition):
        limit = max(100, partition.depth * 100)
        messages = self._message_history(
            zulip,
            "newest",
            max_messages=limit,
        )
        remote_ids = {str(message["id"]) for message in messages}
        for message in messages:
            self._sync_message(account, message, zulip)
        numeric_ids = [int(value) for value in remote_ids]
        mismatches = 0
        if numeric_ids:
            lower = min(numeric_ids)
            for row in self._list_entity_maps(account, "message"):
                if (
                    int(row["external_key"]) < lower
                    or row["external_key"] in remote_ids
                ):
                    continue
                self._delete_message(account, row["external_key"])
                mismatches += 1
        return messages, mismatches

    def sync_account(self, account: models.ExternalAccount) -> None:
        self.repository.save_account(account.uuid, account.settings, "syncing")
        zulip = self.client_class(account.settings)
        queue_state = self.repository.get_zulip_queue_state(account.uuid)
        current_user = zulip.current_user()
        owner_urn = self._upsert_user(account, current_user)
        last_message_id = 0 if queue_state is None else queue_state["last_message_id"]
        last_event_id = -1 if queue_state is None else queue_state["last_event_id"]
        queue_id = None if queue_state is None else queue_state["queue_id"]
        if queue_id is None:
            registered = zulip.register_queue()
            queue_id = registered["queue_id"]
            last_event_id = int(registered["last_event_id"])
            self._sync_users(account, zulip)
            self._sync_streams(account, zulip, owner_urn)
            initial = self._message_history(zulip)
            for raw_message in initial:
                synced_id = self._sync_message(account, raw_message, zulip)
                last_message_id = max(last_message_id, synced_id or 0)
        try:
            events = zulip.events(queue_id, last_event_id)
        except Exception:
            registered = zulip.register_queue()
            queue_id = registered["queue_id"]
            last_event_id = int(registered["last_event_id"])
            events = []
            self._sync_users(account, zulip)
            self._sync_streams(account, zulip, owner_urn)
            for raw_message in self._message_history(zulip, last_message_id):
                synced_id = self._sync_message(account, raw_message, zulip)
                last_message_id = max(last_message_id, synced_id or 0)
        else:
            if events:
                last_event_id = max(int(event["id"]) for event in events)
                last_message_id = max(
                    last_message_id,
                    self._process_events(account, zulip, events, owner_urn),
                )
        now = self.repository.now()
        partitions = self.repository.load_partitions(account.uuid, "zulip_message")
        partition = next(
            iter(partitions),
            reconciliation.ReconciliationPartition(
                account_uuid=str(account.uuid),
                entity_kind="zulip_message",
                partition_key="recent",
                next_due_at=now,
            ),
        )
        if self.scheduler.select([partition], now, budget=partition.estimated_cost):
            if queue_state is None:
                messages, mismatches = initial, 0
            else:
                messages, mismatches = self._reconcile_history(
                    account, zulip, partition
                )
                if messages:
                    last_message_id = max(
                        last_message_id,
                        max(int(message["id"]) for message in messages),
                    )
            self.repository.save_partition(
                self.scheduler.complete(
                    partition,
                    now,
                    mismatches=mismatches,
                    actual_cost=max(0.01, len(messages) / 100),
                    cursor=str(last_message_id),
                )
            )
        self.repository.save_zulip_queue_state(
            account.uuid, queue_id, last_event_id, last_message_id
        )
        self.repository.save_account(account.uuid, account.settings, "active")
        self.client.report_external_account_status(account.uuid, "confirmed")

    @staticmethod
    def _account_for_command(command, accounts):
        return next(
            account
            for account in accounts
            if account.uuid == command.external_account_uuid
        )

    def handle_command(self, command, accounts):
        account = self._account_for_command(command, accounts)
        zulip = self.client_class(account.settings)
        payload = command.payload
        if command.operation == "message.create":
            stream = self._get_entity_map_by_urn(
                account, "stream", payload["stream_urn"]
            )
            topic = self._get_entity_map_by_urn(account, "topic", payload["topic_urn"])
            if stream is None or topic is None:
                raise ValueError("Message command contains unknown stream or topic")
            message_payload = {
                "content": codec.rewrite_file_urns(
                    payload["payload"]["content"],
                    lambda workspace_urn, display_name: self._export_file(
                        account,
                        zulip,
                        workspace_urn,
                        display_name,
                    ),
                ),
            }
            if stream["provider_payload"].get("kind") == "direct":
                message_payload.update(
                    {
                        "type": "direct",
                        "to": json.dumps(
                            stream["provider_payload"]["recipient_ids"],
                        ),
                    },
                )
            else:
                message_payload.update(
                    {
                        "type": "stream",
                        "to": stream["external_key"],
                        "topic": topic["provider_payload"]["name"],
                    },
                )
            external_id = zulip.send_message(message_payload)
            self._replace_entity_map(
                account,
                "message",
                str(external_id),
                command.entity_urn,
                {"message_id": external_id},
            )
            return models.CommandResult(
                models.DeliveryStatus.DELIVERED,
                provider_external_id=str(external_id),
            )
        if command.operation == "message.update":
            mapping = self._get_entity_map_by_urn(
                account, "message", command.entity_urn
            )
            if mapping is None:
                raise ValueError("Message update lacks provider mapping")
            zulip.update_message(
                int(mapping["external_key"]),
                codec.rewrite_file_urns(
                    payload["payload"]["content"],
                    lambda workspace_urn, display_name: self._export_file(
                        account,
                        zulip,
                        workspace_urn,
                        display_name,
                    ),
                ),
            )
            return models.CommandResult(models.DeliveryStatus.DELIVERED)
        if command.operation == "message.delete":
            mapping = self._get_entity_map_by_urn(
                account, "message", command.entity_urn
            )
            if mapping is None:
                raise ValueError("Message delete lacks provider mapping")
            zulip.delete_message(int(mapping["external_key"]))
            return models.CommandResult(models.DeliveryStatus.DELIVERED)
        if command.operation in ("reaction.create", "reaction.update"):
            message = self._get_entity_map_by_urn(
                account, "message", payload["message_urn"]
            )
            if message is None:
                raise ValueError("Reaction command contains unknown message")
            author = self._get_entity_map_by_urn(
                account,
                "user",
                payload["author_urn"],
            )
            if author is None:
                raise ValueError("Reaction command contains unknown author")
            if command.operation == "reaction.update":
                mapping = self._get_entity_map_by_urn(
                    account, "reaction", command.entity_urn
                )
                if mapping is not None:
                    zulip.remove_reaction(
                        int(message["external_key"]),
                        mapping["provider_payload"]["emoji_name"],
                    )
            zulip.add_reaction(int(message["external_key"]), payload["emoji_name"])
            self._replace_entity_map(
                account,
                "reaction",
                ":".join(
                    (
                        message["external_key"],
                        author["external_key"],
                        payload["emoji_name"],
                    ),
                ),
                command.entity_urn,
                {
                    "message_id": message["external_key"],
                    "user_id": author["external_key"],
                    "emoji_name": payload["emoji_name"],
                },
            )
            return models.CommandResult(models.DeliveryStatus.DELIVERED)
        if command.operation == "reaction.delete":
            mapping = self._get_entity_map_by_urn(
                account, "reaction", command.entity_urn
            )
            message = self._get_entity_map_by_urn(
                account, "message", payload["message_urn"]
            )
            if mapping is None or message is None:
                raise ValueError("Reaction delete lacks provider mapping")
            zulip.remove_reaction(
                int(message["external_key"]),
                mapping["provider_payload"]["emoji_name"],
            )
            return models.CommandResult(models.DeliveryStatus.DELIVERED)
        if command.operation in ("stream.update", "stream.delete"):
            mapping = self._get_entity_map_by_urn(account, "stream", command.entity_urn)
            if mapping is None:
                raise ValueError("Stream command lacks provider mapping")
            stream_id = int(mapping["external_key"])
            if command.operation == "stream.update":
                zulip.update_stream(
                    stream_id, payload["name"], payload.get("description", "")
                )
            else:
                zulip.delete_stream(stream_id)
            return models.CommandResult(models.DeliveryStatus.DELIVERED)
        if command.operation in ("topic.create", "topic.update", "topic.delete"):
            if command.operation == "topic.create":
                raise ValueError("Zulip cannot create an empty topic without a message")
            mapping = self._get_entity_map_by_urn(account, "topic", command.entity_urn)
            if mapping is None:
                raise ValueError("Topic command lacks provider mapping")
            stream_id, old_name = mapping["external_key"].split(":", 1)
            if command.operation == "topic.update":
                zulip.update_topic(int(stream_id), old_name, payload["name"])
            else:
                zulip.delete_topic(int(stream_id), old_name)
            return models.CommandResult(models.DeliveryStatus.DELIVERED)
        raise ValueError(f"Unsupported Zulip operation: {command.operation}")
