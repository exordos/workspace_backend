import hashlib
import uuid
from typing import Any

from workspace_providers.common import daemon
from workspace_providers.common import models
from workspace_providers.common import reconciliation
from workspace_providers.mail import protocol


class MailProviderDaemon(daemon.ProviderDaemon):
    provider_kind = models.ProviderKind.MAIL
    provider_domain = models.ProviderDomain.MAIL

    def __init__(
        self,
        *args,
        imap_client_class=protocol.ImapClient,
        smtp_client_class=protocol.SmtpClient,
        scheduler: reconciliation.DynamicReconciliationScheduler | None = None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.imap_client_class = imap_client_class
        self.smtp_client_class = smtp_client_class
        self.scheduler = scheduler or reconciliation.DynamicReconciliationScheduler()

    @staticmethod
    def _flags_payload(flags: frozenset[bytes]) -> dict[str, bool]:
        return {
            "seen": b"\\Seen" in flags,
            "flagged": b"\\Flagged" in flags,
            "draft": b"\\Draft" in flags,
        }

    @staticmethod
    def _flags_hash(flags: frozenset[bytes]) -> str:
        return hashlib.sha256(b"\0".join(sorted(flags))).hexdigest()

    @staticmethod
    def _external_message_id(
        folder_path: str,
        uid_validity: int | None,
        uid: int,
    ) -> str:
        return f"{uid_validity or 0}:{folder_path}:{uid}"

    @staticmethod
    def _urn_uuid(value: str) -> uuid.UUID:
        return uuid.UUID(value.rsplit(":", 1)[-1])

    def _mapped_entity_uuid(
        self,
        account_uuid,
        entity_kind: str,
        external_id: str,
    ) -> uuid.UUID | None:
        mapping = self.repository.get_entity_map(
            account_uuid,
            entity_kind,
            external_id,
        )
        if mapping is None:
            return None
        return self._urn_uuid(mapping["workspace_urn"])

    def _upload_attachments(
        self,
        account_uuid,
        parsed: protocol.ParsedMail,
    ) -> list[dict[str, Any]]:
        result = []
        for attachment in parsed.attachments:
            digest = hashlib.sha256(attachment.data).hexdigest()
            reference = self.client.upload_blob(
                account_uuid,
                attachment.name,
                attachment.content_type,
                attachment.data,
                digest,
            )
            result.append(
                {
                    "urn": reference.urn,
                    "name": attachment.name,
                    "content_type": attachment.content_type,
                    "content_id": attachment.content_id,
                    "size_bytes": len(attachment.data),
                    "hash": digest,
                }
            )
        return result

    def _sync_message(
        self,
        account: models.ExternalAccount,
        folder_urn: str,
        folder_path: str,
        uid_validity: int | None,
        uid: int,
        flags: frozenset[bytes],
        raw_message: bytes,
    ) -> None:
        parsed = protocol.parse_message(raw_message)
        external_id = self._external_message_id(folder_path, uid_validity, uid)
        body = parsed.body_text or ""
        payload = {
            "folder_urn": folder_urn,
            "external_uid": uid,
            "from_address": parsed.from_address,
            "to_addresses": parsed.to_addresses,
            "cc_addresses": parsed.cc_addresses,
            "bcc_addresses": parsed.bcc_addresses,
            "reply_to": parsed.reply_to,
            "subject": parsed.subject,
            "snippet": " ".join(body.split())[:4096],
            "body_html": parsed.body_html,
            "body_text": parsed.body_text,
            "message_id": parsed.message_id,
            "references": parsed.references,
            "sent_at": parsed.sent_at.isoformat().replace("+00:00", "Z"),
            "attachments": self._upload_attachments(account.uuid, parsed),
            **self._flags_payload(flags),
        }
        reference = self.client.upsert_mail(
            "messages",
            external_id,
            account.uuid,
            payload,
            entity_uuid=self._mapped_entity_uuid(
                account.uuid,
                "mail_message",
                external_id,
            ),
        )
        self.repository.save_entity_map(
            account.uuid,
            "mail_message",
            external_id,
            reference.urn,
            hashlib.sha256(raw_message).hexdigest(),
            {"folder_path": folder_path, "uid": uid},
        )
        self.repository.save_mail_message_state(
            account.uuid,
            folder_path,
            uid,
            parsed.message_id,
            reference.urn,
            self._flags_hash(flags),
        )

    @staticmethod
    def _mail_payload(entity: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "folder_urn",
            "external_uid",
            "from_address",
            "to_addresses",
            "cc_addresses",
            "bcc_addresses",
            "reply_to",
            "subject",
            "snippet",
            "body_html",
            "body_text",
            "message_id",
            "references",
            "sent_at",
            "seen",
            "flagged",
            "draft",
            "deleted",
        }
        return {key: value for key, value in entity.items() if key in allowed}

    def _update_workspace_flags(
        self,
        account: models.ExternalAccount,
        external_id: str,
        flags: frozenset[bytes],
    ) -> None:
        entities = self.client.list_entities(
            models.ProviderDomain.MAIL,
            "messages",
            account.uuid,
            {"provider_external_id": external_id},
            limit=1,
        )
        if not entities:
            return
        payload = self._mail_payload(entities[0])
        payload.update(self._flags_payload(flags))
        self.client.upsert_mail(
            "messages",
            external_id,
            account.uuid,
            payload,
            entity_uuid=self._urn_uuid(entities[0]["urn"]),
        )

    def _reset_uid_validity(
        self,
        account: models.ExternalAccount,
        folder_path: str,
        old_uid_validity: int | None,
    ) -> int:
        rows = self.repository.list_mail_message_states(account.uuid, folder_path)
        for row in rows:
            external_id = self._external_message_id(
                folder_path, old_uid_validity, row["uid"]
            )
            self.client.delete_entity(
                models.ProviderDomain.MAIL,
                "messages",
                external_id,
                account.uuid,
                entity_uuid=self._mapped_entity_uuid(
                    account.uuid,
                    "mail_message",
                    external_id,
                ),
            )
            self.repository.delete_mail_message_state(
                account.uuid, folder_path, row["uid"]
            )
        return len(rows)

    def _reconcile_folder(
        self,
        account: models.ExternalAccount,
        imap: protocol.ImapClient,
        folder_urn: str,
        folder_path: str,
        metadata: protocol.FolderMetadata,
        initial_mismatches: int,
    ) -> None:
        now = self.repository.now()
        partitions = self.repository.load_partitions(account.uuid, "mail_message")
        partition = next(
            (item for item in partitions if item.partition_key == folder_path),
            reconciliation.ReconciliationPartition(
                account_uuid=str(account.uuid),
                entity_kind="mail_message",
                partition_key=folder_path,
                next_due_at=now,
            ),
        )
        selected = self.scheduler.select(
            [partition], now, budget=partition.estimated_cost
        )
        if not selected:
            return
        start_uid = max(1, (metadata.uid_next or 1) - max(100, partition.depth * 100))
        remote_flags = imap.fetch_flags(folder_path, start_uid)
        rows = self.repository.list_mail_message_states(
            account.uuid, folder_path, start_uid
        )
        local = {row["uid"]: row for row in rows}
        mismatches = initial_mismatches
        for uid, row in local.items():
            flags = remote_flags.get(uid)
            external_id = self._external_message_id(
                folder_path, metadata.uid_validity, uid
            )
            if flags is None:
                self.client.delete_entity(
                    models.ProviderDomain.MAIL,
                    "messages",
                    external_id,
                    account.uuid,
                    entity_uuid=self._mapped_entity_uuid(
                        account.uuid,
                        "mail_message",
                        external_id,
                    ),
                )
                self.repository.delete_mail_message_state(
                    account.uuid, folder_path, uid
                )
                mismatches += 1
            elif row["flags_hash"] != self._flags_hash(flags):
                self._update_workspace_flags(account, external_id, flags)
                self.repository.save_mail_message_state(
                    account.uuid,
                    folder_path,
                    uid,
                    row["message_id"],
                    row["workspace_urn"],
                    self._flags_hash(flags),
                )
                mismatches += 1
        missing_uids = sorted(set(remote_flags) - set(local))
        for uid, flags, raw_message in imap.fetch_uids(folder_path, missing_uids):
            self._sync_message(
                account,
                folder_urn,
                folder_path,
                metadata.uid_validity,
                uid,
                flags,
                raw_message,
            )
            mismatches += 1
        completed = self.scheduler.complete(
            partition,
            now,
            mismatches=mismatches,
            actual_cost=max(0.01, (len(local) + len(remote_flags)) / 100),
            cursor=str(metadata.uid_next or 1),
        )
        self.repository.save_partition(completed)

    def _sync_folder(
        self,
        account: models.ExternalAccount,
        imap: protocol.ImapClient,
        folder: protocol.FolderInfo,
    ) -> None:
        stored = self.repository.get_mail_folder_state(account.uuid, folder.path)
        metadata = imap.select(folder.path)
        old_uid_validity = stored["uid_validity"] if stored is not None else None
        validity_changed = (
            stored is not None and old_uid_validity != metadata.uid_validity
        )
        mismatches = (
            self._reset_uid_validity(account, folder.path, old_uid_validity)
            if validity_changed
            else 0
        )
        last_uid = 0
        if stored is not None and not validity_changed:
            last_uid = max(0, (stored["uid_next"] or 1) - 1)
        folder_reference = self.client.upsert_mail(
            "folders",
            folder.path,
            account.uuid,
            {
                "path": folder.path,
                "name": folder.display_path.rsplit(folder.delimiter, 1)[-1],
                "delimiter": folder.delimiter,
                "special_use": folder.special_use,
            },
            entity_uuid=self._mapped_entity_uuid(
                account.uuid,
                "mail_folder",
                folder.path,
            ),
        )
        self.repository.save_entity_map(
            account.uuid,
            "mail_folder",
            folder.path,
            folder_reference.urn,
            provider_payload={"path": folder.path},
        )
        for uid, flags, raw_message in imap.fetch_since(folder.path, last_uid):
            self._sync_message(
                account,
                folder_reference.urn,
                folder.path,
                metadata.uid_validity,
                uid,
                flags,
                raw_message,
            )
        self._reconcile_folder(
            account,
            imap,
            folder_reference.urn,
            folder.path,
            metadata,
            mismatches,
        )
        self.repository.save_mail_folder_state(
            account.uuid,
            folder.path,
            delimiter=folder.delimiter,
            special_use=folder.special_use,
            uid_validity=metadata.uid_validity,
            uid_next=metadata.uid_next,
            highest_modseq=metadata.highest_modseq,
            workspace_urn=folder_reference.urn,
        )

    def sync_account(self, account: models.ExternalAccount) -> None:
        self.repository.save_account(account.uuid, account.settings, "syncing")
        with self.imap_client_class(account.settings) as imap:
            for folder in imap.list_folders():
                self._sync_folder(account, imap, folder)
        self.repository.save_account(account.uuid, account.settings, "active")
        self.client.report_external_account_status(account.uuid, "confirmed")

    @staticmethod
    def _account_for_command(
        command: models.ProviderCommand,
        accounts: list[models.ExternalAccount],
    ) -> models.ExternalAccount:
        return next(
            account
            for account in accounts
            if account.uuid == command.external_account_uuid
        )

    def _command_attachments(
        self,
        command: models.ProviderCommand,
    ) -> list[tuple[str, str, bytes]]:
        return [
            (
                item["name"],
                item["content_type"],
                self.client.download_blob(item["urn"]),
            )
            for item in command.payload.get("attachments", [])
        ]

    @staticmethod
    def _message_location(external_id: str) -> tuple[str, int]:
        _uid_validity, rest = external_id.split(":", 1)
        folder_path, uid = rest.rsplit(":", 1)
        return folder_path, int(uid)

    def _folder_path(self, account_uuid, folder_urn: str) -> str:
        row = self.repository.get_entity_map_by_urn(
            account_uuid, "mail_folder", folder_urn
        )
        if row is None:
            raise ValueError(f"Unknown mail folder: {folder_urn}")
        return row["external_key"]

    def _command_external_id(
        self,
        command: models.ProviderCommand,
        entity_kind: str,
    ) -> str | None:
        row = self.repository.get_entity_map_by_urn(
            command.external_account_uuid, entity_kind, command.entity_urn
        )
        return None if row is None else row["external_key"]

    @staticmethod
    def _sent_folder(
        settings: dict[str, Any],
        imap: protocol.ImapClient,
    ) -> str | None:
        configured = settings.get("sent_folder")
        if isinstance(configured, str) and configured:
            return configured
        return next(
            (
                folder.path
                for folder in imap.list_folders()
                if folder.special_use == "sent"
            ),
            None,
        )

    def handle_command(
        self,
        command: models.ProviderCommand,
        accounts: list[models.ExternalAccount],
    ) -> models.CommandResult:
        account = self._account_for_command(command, accounts)
        payload = command.payload
        if command.operation == "message.send":
            mapping = self.repository.get_entity_map_by_urn(
                account.uuid,
                "mail_message",
                command.entity_urn,
            )
            external_id = None if mapping is None else mapping["external_key"]
            message_payload = payload.copy()
            if mapping is not None:
                mapped_message_id = mapping["provider_payload"].get("message_id")
                if mapped_message_id and not message_payload.get("message_id"):
                    message_payload["message_id"] = mapped_message_id
            message = protocol.build_message(
                message_payload,
                self._command_attachments(command),
            )
            with self.imap_client_class(account.settings) as imap:
                sent_folder = self._sent_folder(account.settings, imap)
                self.smtp_client_class(account.settings).send(message)
                if sent_folder:
                    append_uid = imap.append(
                        sent_folder,
                        message.as_bytes(),
                        ("\\Seen",),
                    )
                    if append_uid is not None:
                        sent_external_id = self._external_message_id(
                            sent_folder,
                            append_uid.uid_validity,
                            append_uid.uid,
                        )
                        if external_id is not None and external_id != sent_external_id:
                            old_folder, old_uid = self._message_location(external_id)
                            imap.update_flags(
                                old_folder,
                                old_uid,
                                False,
                                False,
                                True,
                            )
                            self.repository.delete_mail_message_state(
                                account.uuid,
                                old_folder,
                                old_uid,
                            )
                        external_id = sent_external_id
                        self.repository.replace_entity_map(
                            account.uuid,
                            "mail_message",
                            sent_external_id,
                            command.entity_urn,
                            {
                                "folder_path": sent_folder,
                                "uid": append_uid.uid,
                                "message_id": str(message["Message-ID"]),
                            },
                        )
                        self.repository.save_mail_message_state(
                            account.uuid,
                            sent_folder,
                            append_uid.uid,
                            str(message["Message-ID"]),
                            command.entity_urn,
                            self._flags_hash(frozenset({b"\\Seen"})),
                        )
            return models.CommandResult(
                models.DeliveryStatus.DELIVERED,
                provider_external_id=external_id,
            )
        if command.operation.startswith("folder."):
            path = payload["path"]
            old_path = self._command_external_id(command, "mail_folder")
            with self.imap_client_class(account.settings) as imap:
                if command.operation == "folder.create":
                    imap.create_folder(path)
                elif command.operation == "folder.update":
                    if old_path is None:
                        raise ValueError("Mail folder update lacks provider mapping")
                    if old_path != path:
                        imap.rename_folder(old_path, path)
                elif command.operation == "folder.delete":
                    if old_path is None:
                        raise ValueError("Mail folder delete lacks provider mapping")
                    imap.delete_folder(old_path)
                else:
                    raise ValueError(f"Unsupported mail operation: {command.operation}")
            if command.operation != "folder.delete":
                self.repository.replace_entity_map(
                    account.uuid,
                    "mail_folder",
                    path,
                    command.entity_urn,
                    {"path": path},
                )
            return models.CommandResult(
                models.DeliveryStatus.DELIVERED,
                provider_external_id=path,
            )
        if command.operation in ("message.create", "message.update"):
            external_id = self._command_external_id(command, "mail_message")
            with self.imap_client_class(account.settings) as imap:
                if external_id:
                    folder_path, uid = self._message_location(external_id)
                    imap.update_flags(
                        folder_path,
                        uid,
                        bool(payload.get("seen")),
                        bool(payload.get("flagged")),
                        bool(payload.get("deleted")),
                    )
                elif payload.get("draft"):
                    message = protocol.build_message(
                        payload, self._command_attachments(command)
                    )
                    folder_path = self._folder_path(account.uuid, payload["folder_urn"])
                    append_uid = imap.append(
                        folder_path,
                        message.as_bytes(),
                        ("\\Draft",),
                    )
                    if append_uid is None:
                        return models.CommandResult(
                            models.DeliveryStatus.FAILED,
                            error=(
                                "IMAP APPEND completed without APPENDUID; "
                                "the draft cannot be correlated safely"
                            ),
                        )
                    external_id = self._external_message_id(
                        folder_path,
                        append_uid.uid_validity,
                        append_uid.uid,
                    )
                    self.repository.replace_entity_map(
                        account.uuid,
                        "mail_message",
                        external_id,
                        command.entity_urn,
                        {
                            "folder_path": folder_path,
                            "uid": append_uid.uid,
                            "message_id": str(message["Message-ID"]),
                        },
                    )
                    self.repository.save_mail_message_state(
                        account.uuid,
                        folder_path,
                        append_uid.uid,
                        str(message["Message-ID"]),
                        command.entity_urn,
                        self._flags_hash(frozenset({b"\\Draft"})),
                    )
            return models.CommandResult(
                models.DeliveryStatus.DELIVERED,
                provider_external_id=external_id,
            )
        if command.operation in ("message.delete", "message.move"):
            external_id = self._command_external_id(command, "mail_message")
            if external_id is None:
                raise ValueError("Mail message command lacks provider mapping")
            source_path, uid = self._message_location(external_id)
            with self.imap_client_class(account.settings) as imap:
                if command.operation == "message.delete":
                    imap.update_flags(source_path, uid, False, False, True)
                else:
                    target_path = self._folder_path(account.uuid, payload["folder_urn"])
                    imap.move(source_path, target_path, uid)
            return models.CommandResult(models.DeliveryStatus.DELIVERED)
        raise ValueError(f"Unsupported mail operation: {command.operation}")
