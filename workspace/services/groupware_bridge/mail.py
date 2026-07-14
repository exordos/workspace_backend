# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import base64
import datetime
import email
import email.header
import email.message
import email.policy
import email.utils
import hashlib
import imaplib
import re
import smtplib
import ssl
import uuid as sys_uuid

from restalchemy.dm import filters as dm_filters

from workspace.groupware.dm import models
from workspace.messenger_api import file_storage
from workspace.messenger_api import events as workspace_events


IMAP_LIST_RE = re.compile(
    rb'^\((?P<flags>[^)]*)\)\s+"(?P<delimiter>[^"]*)"\s+(?P<name>.+)$',
)
SPECIAL_USE_FLAGS = {
    b"\\Inbox": "inbox",
    b"\\Sent": "sent",
    b"\\Drafts": "drafts",
    b"\\Trash": "trash",
    b"\\Junk": "junk",
}
UID_FLAGS_RE = re.compile(rb"UID\s+(?P<uid>\d+).*FLAGS\s+\((?P<flags>[^)]*)\)")
SMTP_TIMEOUT_SECONDS = 30


def _decode_header(value):
    if value is None:
        return ""
    return str(email.header.make_header(email.header.decode_header(value)))


def _message_addresses(message, name):
    return [
        email.utils.formataddr((display_name, address))
        for display_name, address in email.utils.getaddresses(
            message.get_all(name, []),
        )
    ]


def _message_date(message):
    try:
        value = email.utils.parsedate_to_datetime(message.get("Date", ""))
    except (TypeError, ValueError):
        value = None
    if value is None:
        return datetime.datetime.now(datetime.timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=datetime.timezone.utc)
    return value.astimezone(datetime.timezone.utc)


def _body_parts(message):
    body_html = None
    body_text = None
    attachments = []
    parts = message.walk() if message.is_multipart() else [message]
    for part in parts:
        if part.is_multipart():
            continue
        content = part.get_payload(decode=True) or b""
        content_type = part.get_content_type()
        disposition = part.get_content_disposition()
        filename = part.get_filename()
        if disposition == "attachment" or filename is not None:
            attachments.append(
                (
                    _decode_header(filename) or "attachment",
                    content_type,
                    part.get("Content-ID"),
                    content,
                )
            )
            continue
        charset = part.get_content_charset() or "utf-8"
        decoded = content.decode(charset, errors="replace")
        if content_type == "text/html" and body_html is None:
            body_html = decoded
        elif content_type == "text/plain" and body_text is None:
            body_text = decoded
    return body_html, body_text, attachments


def _decode_imap_utf7(value):
    chunks = []
    position = 0
    while position < len(value):
        marker = value.find("&", position)
        if marker < 0:
            chunks.append(value[position:])
            break
        chunks.append(value[position:marker])
        end = value.find("-", marker)
        if end < 0:
            chunks.append(value[marker:])
            break
        encoded = value[marker + 1 : end]
        if not encoded:
            chunks.append("&")
        else:
            padding = "=" * (-len(encoded) % 4)
            decoded = base64.b64decode(
                encoded.replace(",", "/") + padding,
            ).decode("utf-16-be")
            chunks.append(decoded)
        position = end + 1
    return "".join(chunks)


def _parse_list_row(row):
    match = IMAP_LIST_RE.match(row)
    if match is None:
        raise ValueError("Unsupported IMAP LIST response")
    raw_name = match.group("name")
    if raw_name.startswith(b'"') and raw_name.endswith(b'"'):
        raw_name = raw_name[1:-1]
    path = raw_name.decode("ascii")
    display_path = _decode_imap_utf7(path)
    delimiter = match.group("delimiter").decode("ascii") or "/"
    flags = match.group("flags").split()
    special_use = next(
        (value for flag, value in SPECIAL_USE_FLAGS.items() if flag in flags),
        None,
    )
    return path, delimiter, special_use, display_path


class ImapClient:
    def __init__(self, settings):
        self.settings = settings
        self.connection = None

    def __enter__(self):
        if self.settings.imap_security == "tls":
            self.connection = imaplib.IMAP4_SSL(
                self.settings.imap_host,
                self.settings.imap_port,
            )
        else:
            self.connection = imaplib.IMAP4(
                self.settings.imap_host,
                self.settings.imap_port,
            )
            if self.settings.imap_security == "starttls":
                self.connection.starttls(ssl_context=ssl.create_default_context())
        credentials = self.settings.credentials
        self.connection.login(credentials.username, credentials.password)
        return self

    def __exit__(self, exc_type, exc, traceback):
        try:
            self.connection.logout()
        except (imaplib.IMAP4.error, OSError):
            pass

    def list_folders(self):
        status, rows = self.connection.list()
        if status != "OK":
            raise imaplib.IMAP4.error("Unable to list mail folders")
        return [_parse_list_row(row) for row in rows if row is not None]

    def fetch_since(self, path, external_uid):
        status, _ = self.connection.select(path, readonly=True)
        if status != "OK":
            raise imaplib.IMAP4.error(f"Unable to select {path}")
        criterion = f"UID {external_uid + 1}:*"
        status, rows = self.connection.uid("search", None, criterion)
        if status != "OK" or not rows or not rows[0]:
            return []
        messages = []
        for uid_value in rows[0].split():
            status, response = self.connection.uid(
                "fetch",
                uid_value,
                "(RFC822 FLAGS)",
            )
            if status != "OK":
                continue
            metadata, raw_message = response[0]
            flags = imaplib.ParseFlags(metadata)
            messages.append((int(uid_value), flags, raw_message))
        return messages

    def fetch_flags(self, path):
        status, _ = self.connection.select(path, readonly=True)
        if status != "OK":
            raise imaplib.IMAP4.error(f"Unable to select {path}")
        status, response = self.connection.uid("fetch", "1:*", "(UID FLAGS)")
        if status != "OK":
            raise imaplib.IMAP4.error(f"Unable to fetch flags for {path}")
        result = {}
        for item in response:
            metadata = item[0] if isinstance(item, tuple) else item
            if not isinstance(metadata, bytes):
                continue
            match = UID_FLAGS_RE.search(metadata)
            if match is None:
                continue
            result[int(match.group("uid"))] = frozenset(
                imaplib.ParseFlags(metadata),
            )
        return result

    def append(self, path, raw_message, flags=()):
        flag_value = "(" + " ".join(flags) + ")" if flags else None
        status, _ = self.connection.append(path, flag_value, None, raw_message)
        if status != "OK":
            raise imaplib.IMAP4.error(f"Unable to append message to {path}")

    def create_folder(self, path):
        status, _ = self.connection.create(path)
        if status != "OK":
            raise imaplib.IMAP4.error(f"Unable to create folder {path}")

    def rename_folder(self, source_path, target_path):
        status, _ = self.connection.rename(source_path, target_path)
        if status != "OK":
            raise imaplib.IMAP4.error(
                f"Unable to rename folder {source_path} to {target_path}",
            )

    def delete_folder(self, path):
        status, _ = self.connection.delete(path)
        if status != "OK":
            raise imaplib.IMAP4.error(f"Unable to delete folder {path}")

    def update(self, path, external_uid, seen, flagged, deleted):
        status, _ = self.connection.select(path)
        if status != "OK":
            raise imaplib.IMAP4.error(f"Unable to select {path}")
        flags = []
        if seen:
            flags.append("\\Seen")
        if flagged:
            flags.append("\\Flagged")
        if deleted:
            flags.append("\\Deleted")
        status, _ = self.connection.uid(
            "store",
            str(external_uid),
            "FLAGS",
            "(" + " ".join(flags) + ")",
        )
        if status != "OK":
            raise imaplib.IMAP4.error("Unable to update message flags")
        if deleted:
            self.connection.expunge()

    def move(self, source_path, target_path, external_uid):
        status, _ = self.connection.select(source_path)
        if status != "OK":
            raise imaplib.IMAP4.error(f"Unable to select {source_path}")
        status, _ = self.connection.uid(
            "copy",
            str(external_uid),
            target_path,
        )
        if status != "OK":
            raise imaplib.IMAP4.error("Unable to copy message")
        self.connection.uid("store", str(external_uid), "+FLAGS", "(\\Deleted)")
        self.connection.expunge()


class SmtpClient:
    def __init__(self, settings):
        self.settings = settings

    def send(self, message):
        if self.settings.smtp_security == "tls":
            connection = smtplib.SMTP_SSL(
                self.settings.smtp_host,
                self.settings.smtp_port,
                context=ssl.create_default_context(),
                timeout=SMTP_TIMEOUT_SECONDS,
            )
        else:
            connection = smtplib.SMTP(
                self.settings.smtp_host,
                self.settings.smtp_port,
                timeout=SMTP_TIMEOUT_SECONDS,
            )
            if self.settings.smtp_security == "starttls":
                connection.starttls(context=ssl.create_default_context())
        try:
            credentials = self.settings.credentials
            connection.login(credentials.username, credentials.password)
            connection.send_message(message)
        finally:
            connection.quit()


class MailSynchronizer:
    def __init__(self, imap_client_class=ImapClient, smtp_client_class=SmtpClient):
        self.imap_client_class = imap_client_class
        self.smtp_client_class = smtp_client_class

    @staticmethod
    def _folder_name(path, delimiter):
        return path.rsplit(delimiter, 1)[-1]

    def _upsert_folder(
        self,
        account,
        path,
        delimiter,
        special_use,
        display_path,
    ):
        folder = models.MailFolder.objects.get_one_or_none(
            filters={
                "project_id": dm_filters.EQ(account.project_id),
                "user_uuid": dm_filters.EQ(account.user_uuid),
                "external_user_uuid": dm_filters.EQ(account.uuid),
                "path": dm_filters.EQ(path),
            },
        )
        values = {
            "external_user_uuid": account.uuid,
            "name": self._folder_name(display_path, delimiter),
            "delimiter": delimiter,
            "special_use": special_use,
            "source_name": models.MailSource.IMAP.value,
            "source": {"path": path},
        }
        if folder is None:
            folder = models.MailFolder(
                uuid=sys_uuid.uuid4(),
                project_id=account.project_id,
                user_uuid=account.user_uuid,
                path=path,
                **values,
            )
            folder.insert()
            workspace_events.create_groupware_event(
                folder,
                workspace_events.MAIL_FOLDER_CREATED_EVENT,
            )
            return folder
        changed = any(
            getattr(folder, name) != value
            for name, value in values.items()
        )
        folder.update_dm(values=values)
        folder.update()
        if changed:
            workspace_events.create_groupware_event(
                folder,
                workspace_events.MAIL_FOLDER_UPDATED_EVENT,
            )
        return folder

    def _save_attachment(self, message, name, content_type, content_id, data):
        attachment_uuid = sys_uuid.uuid4()
        storage = file_storage.save_workspace_file(attachment_uuid, data)
        attachment = models.MailAttachment(
            uuid=attachment_uuid,
            project_id=message.project_id,
            user_uuid=message.user_uuid,
            message_uuid=message.uuid,
            content_id=content_id,
            name=name,
            content_type=content_type,
            size_bytes=len(data),
            hash=hashlib.sha256(data).hexdigest(),
            storage_type=storage.storage_type,
            storage_id=storage.storage_id,
            storage_object_id=storage.storage_object_id,
        )
        attachment.insert()

    def _import_message(self, account, folder, external_uid, flags, raw_message):
        existing = models.MailMessage.objects.get_one_or_none(
            filters={
                "external_user_uuid": dm_filters.EQ(account.uuid),
                "folder_uuid": dm_filters.EQ(folder.uuid),
                "external_uid": dm_filters.EQ(external_uid),
            },
        )
        if existing is not None:
            return existing
        parsed = email.message_from_bytes(raw_message, policy=email.policy.default)
        message_id = parsed.get("Message-ID")
        if message_id is not None:
            pending_messages = models.MailMessage.objects.get_all(
                filters={
                    "project_id": dm_filters.EQ(account.project_id),
                    "user_uuid": dm_filters.EQ(account.user_uuid),
                    "external_user_uuid": dm_filters.EQ(account.uuid),
                    "folder_uuid": dm_filters.EQ(folder.uuid),
                    "deleted": dm_filters.EQ(False),
                },
            )
            pending_message = next(
                (
                    item
                    for item in pending_messages
                    if item.external_uid is None
                    and item.message_id == message_id
                ),
                None,
            )
            if pending_message is not None:
                pending_message.update_dm(
                    values={
                        "external_uid": external_uid,
                        "seen": b"\\Seen" in flags,
                        "flagged": b"\\Flagged" in flags,
                        "draft": b"\\Draft" in flags,
                        "source_name": models.MailSource.IMAP.value,
                        "source": {
                            "folder_path": folder.path,
                            "external_uid": external_uid,
                        },
                        "sync_status": models.SyncStatus.SYNCED.value,
                        "sync_error": None,
                    },
                )
                pending_message.update()
                workspace_events.create_groupware_event(
                    pending_message,
                    workspace_events.MAIL_MESSAGE_UPDATED_EVENT,
                )
                return pending_message
        body_html, body_text, attachments = _body_parts(parsed)
        text = body_text or ""
        message = models.MailMessage(
            uuid=sys_uuid.uuid4(),
            project_id=account.project_id,
            user_uuid=account.user_uuid,
            folder_uuid=folder.uuid,
            external_user_uuid=account.uuid,
            external_uid=external_uid,
            from_address=_decode_header(parsed.get("From")),
            to_addresses=_message_addresses(parsed, "To"),
            cc_addresses=_message_addresses(parsed, "Cc"),
            bcc_addresses=[],
            reply_to=_decode_header(parsed.get("Reply-To")) or None,
            subject=_decode_header(parsed.get("Subject")),
            snippet=" ".join(text.split())[:4096],
            body_html=body_html,
            body_text=body_text,
            message_id=message_id,
            references=parsed.get("References"),
            sent_at=_message_date(parsed),
            seen=b"\\Seen" in flags,
            flagged=b"\\Flagged" in flags,
            draft=b"\\Draft" in flags,
            source_name=models.MailSource.IMAP.value,
            source={"folder_path": folder.path, "external_uid": external_uid},
            sync_status=models.SyncStatus.SYNCED.value,
        )
        message.insert()
        for attachment in attachments:
            self._save_attachment(message, *attachment)
        workspace_events.create_groupware_event(
            message,
            workspace_events.MAIL_MESSAGE_CREATED_EVENT,
        )
        return message

    def _sync_inbound(self, account, imap):
        remote_folders = imap.list_folders()
        remote_paths = {item[0] for item in remote_folders}
        for path, delimiter, special_use, display_path in remote_folders:
            folder = self._upsert_folder(
                account,
                path,
                delimiter,
                special_use,
                display_path,
            )
            last_uid = int(folder.sync_cursor or 0)
            imported = imap.fetch_since(path, last_uid)
            for external_uid, flags, raw_message in imported:
                self._import_message(
                    account,
                    folder,
                    external_uid,
                    flags,
                    raw_message,
                )
                last_uid = max(last_uid, external_uid)
            flags_by_uid = imap.fetch_flags(path)
            local_messages = models.MailMessage.objects.get_all(
                filters={
                    "external_user_uuid": dm_filters.EQ(account.uuid),
                    "folder_uuid": dm_filters.EQ(folder.uuid),
                    "source_name": dm_filters.EQ(models.MailSource.IMAP.value),
                    "deleted": dm_filters.EQ(False),
                },
            )
            for message in local_messages:
                flags = flags_by_uid.get(message.external_uid)
                if flags is None:
                    message.update_dm(values={"deleted": True})
                    message.update()
                    workspace_events.create_groupware_deleted_event(
                        message.project_id,
                        message.user_uuid,
                        message.uuid,
                        workspace_events.MAIL_MESSAGE_DELETED_EVENT,
                    )
                    continue
                values = {
                    "seen": b"\\Seen" in flags,
                    "flagged": b"\\Flagged" in flags,
                    "draft": b"\\Draft" in flags,
                }
                if any(getattr(message, name) != value for name, value in values.items()):
                    message.update_dm(values=values)
                    message.update()
                    workspace_events.create_groupware_event(
                        message,
                        workspace_events.MAIL_MESSAGE_UPDATED_EVENT,
                    )
            counts = {
                "total_count": len(flags_by_uid),
                "unread_count": sum(
                    1 for flags in flags_by_uid.values() if b"\\Seen" not in flags
                ),
            }
            counts_changed = any(
                getattr(folder, name) != value for name, value in counts.items()
            )
            if str(last_uid) != folder.sync_cursor:
                counts["sync_cursor"] = str(last_uid)
            folder.update_dm(values=counts)
            folder.update()
            if counts_changed:
                workspace_events.create_groupware_event(
                    folder,
                    workspace_events.MAIL_FOLDER_UPDATED_EVENT,
                )

        local_folders = models.MailFolder.objects.get_all(
            filters={
                "external_user_uuid": dm_filters.EQ(account.uuid),
                "source_name": dm_filters.EQ(models.MailSource.IMAP.value),
                "deleted": dm_filters.EQ(False),
            },
        )
        for folder in local_folders:
            if folder.path not in remote_paths:
                folder.update_dm(values={"deleted": True})
                folder.update()
                workspace_events.create_groupware_deleted_event(
                    folder.project_id,
                    folder.user_uuid,
                    folder.uuid,
                    workspace_events.MAIL_FOLDER_DELETED_EVENT,
                )

    def _sync_pending_folders(self, account, imap):
        folders = models.MailFolder.objects.get_all(
            filters={
                "external_user_uuid": dm_filters.EQ(account.uuid),
                "sync_status": dm_filters.In(
                    [
                        models.SyncStatus.PENDING.value,
                        models.SyncStatus.FAILED.value,
                    ],
                ),
            },
            order_by={"updated_at": "asc"},
        )
        for folder in folders:
            folder.update_dm(
                values={"sync_status": models.SyncStatus.PROCESSING.value},
            )
            folder.update()
            source_path = folder.source.get("path")
            try:
                if folder.deleted:
                    imap.delete_folder(source_path or folder.path)
                    folder.delete()
                    continue
                if source_path is None:
                    imap.create_folder(folder.path)
                elif source_path != folder.path:
                    imap.rename_folder(source_path, folder.path)
                folder.update_dm(
                    values={
                        "source_name": models.MailSource.IMAP.value,
                        "source": {"path": folder.path},
                        "sync_status": models.SyncStatus.SYNCED.value,
                        "sync_error": None,
                    },
                )
                folder.update()
            except Exception as exc:
                folder.update_dm(
                    values={
                        "sync_status": models.SyncStatus.FAILED.value,
                        "sync_error": str(exc),
                    },
                )
                folder.update()

    @staticmethod
    def _attachments(message):
        return models.MailAttachment.objects.get_all(
            filters={"message_uuid": dm_filters.EQ(message.uuid)},
        )

    def _build_outbound_message(self, account, message):
        outgoing = email.message.EmailMessage()
        outgoing["From"] = account.account_settings.email
        outgoing["To"] = ", ".join(message.to_addresses)
        if message.cc_addresses:
            outgoing["Cc"] = ", ".join(message.cc_addresses)
        if message.bcc_addresses:
            outgoing["Bcc"] = ", ".join(message.bcc_addresses)
        outgoing["Subject"] = message.subject
        if message.message_id is not None:
            outgoing["Message-ID"] = message.message_id
        if message.reply_to is not None:
            outgoing["In-Reply-To"] = message.reply_to
        if message.references is not None:
            outgoing["References"] = message.references
        outgoing.set_content(message.body_text or "")
        if message.body_html is not None:
            outgoing.add_alternative(message.body_html, subtype="html")
        for attachment in self._attachments(message):
            data = file_storage.read_workspace_file(
                attachment.uuid,
                storage_type=attachment.storage_type,
                storage_object_id=attachment.storage_object_id,
            )
            maintype, subtype = attachment.content_type.split("/", 1)
            outgoing.add_attachment(
                data,
                maintype=maintype,
                subtype=subtype,
                filename=attachment.name,
            )
        return outgoing

    @staticmethod
    def _special_folder(account, special_use):
        return models.MailFolder.objects.get_one_or_none(
            filters={
                "project_id": dm_filters.EQ(account.project_id),
                "user_uuid": dm_filters.EQ(account.user_uuid),
                "external_user_uuid": dm_filters.EQ(account.uuid),
                "special_use": dm_filters.EQ(special_use),
            },
        )

    def _sync_pending_message(self, account, imap, smtp, message):
        message.update_dm(values={"sync_status": models.SyncStatus.PROCESSING.value})
        message.update()
        source_path = message.source.get("folder_path")
        try:
            if message.external_uid is not None:
                folder = models.MailFolder.objects.get_one(
                    filters={"uuid": dm_filters.EQ(message.folder_uuid)},
                )
                if source_path is not None and source_path != folder.path:
                    imap.move(source_path, folder.path, message.external_uid)
                    message.update_dm(
                        values={
                            "external_uid": None,
                            "source": {"folder_path": folder.path},
                        },
                    )
                else:
                    imap.update(
                        source_path or folder.path,
                        message.external_uid,
                        message.seen,
                        message.flagged,
                        message.deleted,
                    )
            else:
                if message.message_id is None:
                    email_domain = account.account_settings.email.rpartition("@")[2]
                    message.update_dm(
                        values={
                            "message_id": email.utils.make_msgid(
                                domain=email_domain or None,
                            ),
                        },
                    )
                    message.update()
                outgoing = self._build_outbound_message(account, message)
                if message.draft:
                    target = self._special_folder(account, "drafts")
                    if target is None:
                        raise ValueError("Drafts folder is not available")
                    imap.append(target.path, outgoing.as_bytes(), ("\\Draft",))
                else:
                    smtp.send(outgoing)
                    target = self._special_folder(account, "sent")
                    if target is not None:
                        imap.append(target.path, outgoing.as_bytes(), ("\\Seen",))
            message.update_dm(
                values={
                    "sync_status": models.SyncStatus.SYNCED.value,
                    "sync_error": None,
                },
            )
            message.update()
            workspace_events.create_groupware_event(
                message,
                workspace_events.MAIL_MESSAGE_UPDATED_EVENT,
            )
        except Exception as exc:
            message.update_dm(
                values={
                    "sync_status": models.SyncStatus.FAILED.value,
                    "sync_error": str(exc),
                },
            )
            message.update()
            workspace_events.create_groupware_event(
                message,
                workspace_events.MAIL_MESSAGE_UPDATED_EVENT,
            )

    def sync(self, account):
        settings = account.account_settings
        smtp = self.smtp_client_class(settings)
        with self.imap_client_class(settings) as imap:
            self._sync_pending_folders(account, imap)
            self._sync_inbound(account, imap)
            pending = models.MailMessage.objects.get_all(
                filters={
                    "external_user_uuid": dm_filters.EQ(account.uuid),
                    "sync_status": dm_filters.In(
                        [
                            models.SyncStatus.PENDING.value,
                            models.SyncStatus.FAILED.value,
                        ]
                    ),
                },
                order_by={"updated_at": "asc"},
            )
            for message in pending:
                self._sync_pending_message(account, imap, smtp, message)
