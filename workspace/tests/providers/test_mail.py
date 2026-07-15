import datetime
import email.message
import uuid

from workspace_providers.common import models
from workspace_providers.mail import protocol
from workspace_providers.mail import provider


def test_mail_codec_parses_bodies_addresses_and_attachment():
    message = email.message.EmailMessage()
    message["From"] = "Sender <sender@example.com>"
    message["To"] = "Receiver <receiver@example.com>"
    message["Subject"] = "Provider E2E"
    message["Message-ID"] = "<provider@example.com>"
    message.set_content("plain body")
    message.add_alternative("<b>html body</b>", subtype="html")
    message.add_attachment(
        b"payload",
        maintype="application",
        subtype="octet-stream",
        filename="example.bin",
    )
    parsed = protocol.parse_message(message.as_bytes())
    assert parsed.subject == "Provider E2E"
    assert parsed.to_addresses == ["Receiver <receiver@example.com>"]
    assert parsed.body_text.strip() == "plain body"
    assert "html body" in parsed.body_html
    assert parsed.attachments[0].name == "example.bin"
    assert parsed.attachments[0].data == b"payload"


def test_mail_build_message_preserves_workspace_blob_content():
    outgoing = protocol.build_message(
        {
            "from_address": "sender@example.com",
            "to_addresses": ["receiver@example.com"],
            "subject": "Hello",
            "body_text": "Body",
        },
        [("file.txt", "text/plain", b"attachment")],
    )
    attachments = list(outgoing.iter_attachments())
    assert outgoing["Message-ID"]
    assert attachments[0].get_filename() == "file.txt"
    assert attachments[0].get_payload(decode=True) == b"attachment"


class FakeAppendConnection:
    def __init__(self, append_values, response=(None, [None])):
        self.append_values = append_values
        self.appenduid_response = response

    def append(self, path, flags, date_time, raw_message):
        return "OK", self.append_values

    def response(self, name):
        assert name == "APPENDUID"
        return self.appenduid_response


def test_imap_append_parses_appenduid_from_tagged_response():
    client = protocol.ImapClient({})
    client.connection = FakeAppendConnection(
        [b"[APPENDUID 38505 3955] APPEND completed"]
    )

    result = client.append("Drafts", b"message", ("\\Draft",))

    assert result == protocol.AppendUid(38505, 3955)


def test_imap_append_uses_imaplib_appenduid_response_fallback():
    client = protocol.ImapClient({})
    client.connection = FakeAppendConnection(
        [b"APPEND completed"],
        ("APPENDUID", [b"38505 3955"]),
    )

    result = client.append("Drafts", b"message")

    assert result == protocol.AppendUid(38505, 3955)


def test_imap_append_does_not_invent_uid_without_server_correlation():
    client = protocol.ImapClient({})
    client.connection = FakeAppendConnection([b"APPEND completed"])

    assert client.append("Drafts", b"message") is None


def test_mail_sent_folder_setting_overrides_special_use_discovery():
    class NeverListFolders:
        def list_folders(self):
            raise AssertionError("configured sent folder must not trigger IMAP LIST")

    assert (
        provider.MailProviderDaemon._sent_folder(
            {"sent_folder": "Archive/Sent"},
            NeverListFolders(),
        )
        == "Archive/Sent"
    )


class FakeRepository:
    def __init__(self):
        self.accounts = []
        self.folder_states = {}
        self.entities = []
        self.messages = {}
        self.partitions = []
        self.deleted_messages = []
        self.clock = datetime.datetime(2026, 7, 15, tzinfo=datetime.timezone.utc)

    def save_account(self, *args):
        self.accounts.append(args)

    def get_mail_folder_state(self, account_uuid, path):
        return self.folder_states.get((account_uuid, path))

    def save_mail_folder_state(self, account_uuid, path, **values):
        self.folder_states[(account_uuid, path)] = values

    def save_entity_map(self, *args, **kwargs):
        self.entities.append((args, kwargs))

    def save_mail_message_state(self, *args):
        account_uuid, folder_path, uid, message_id, workspace_urn, flags_hash = args
        self.messages[(account_uuid, folder_path, uid)] = {
            "account_uuid": account_uuid,
            "folder_path": folder_path,
            "uid": uid,
            "message_id": message_id,
            "workspace_urn": workspace_urn,
            "flags_hash": flags_hash,
        }

    def list_mail_message_states(self, account_uuid, folder_path, start_uid=1):
        return [
            row
            for (row_account, row_folder, uid), row in self.messages.items()
            if row_account == account_uuid
            and row_folder == folder_path
            and uid >= start_uid
        ]

    def delete_mail_message_state(self, account_uuid, folder_path, uid):
        self.deleted_messages.append((account_uuid, folder_path, uid))
        self.messages.pop((account_uuid, folder_path, uid), None)

    def load_partitions(self, account_uuid, entity_kind):
        return self.partitions

    def save_partition(self, partition):
        self.partitions = [partition]

    def now(self):
        value = self.clock
        self.clock += datetime.timedelta(seconds=1000)
        return value

    def get_entity_map_by_urn(self, account_uuid, entity_kind, workspace_urn):
        for args, kwargs in reversed(self.entities):
            if (
                args[0] == account_uuid
                and args[1] == entity_kind
                and args[3] == workspace_urn
            ):
                return {
                    "external_key": args[2],
                    "workspace_urn": args[3],
                    "provider_payload": kwargs.get("provider_payload", {}),
                }
        return None

    def get_entity_map(self, account_uuid, entity_kind, external_key):
        for args, kwargs in reversed(self.entities):
            if (
                args[0] == account_uuid
                and args[1] == entity_kind
                and args[2] == external_key
            ):
                return {
                    "external_key": args[2],
                    "workspace_urn": args[3],
                    "provider_payload": kwargs.get("provider_payload", {}),
                }
        return None

    def replace_entity_map(
        self,
        account_uuid,
        entity_kind,
        external_key,
        workspace_urn,
        provider_payload=None,
    ):
        self.entities = [
            item
            for item in self.entities
            if not (
                item[0][0] == account_uuid
                and item[0][1] == entity_kind
                and item[0][3] == workspace_urn
            )
        ]
        self.save_entity_map(
            account_uuid,
            entity_kind,
            external_key,
            workspace_urn,
            provider_payload=provider_payload,
        )


class FakeServiceClient:
    base_url = "https://workspace.example.com"

    def __init__(self):
        self.upserts = []
        self.uploads = []
        self.statuses = []
        self.deletes = []
        self.entities = {}

    def upsert_mail(
        self,
        resource,
        external_id,
        account_uuid,
        payload,
        *,
        entity_uuid=None,
    ):
        self.upserts.append((resource, external_id, account_uuid, payload, entity_uuid))
        resolved_uuid = entity_uuid or uuid.uuid5(account_uuid, external_id)
        urn = f"urn:{resource[:-1]}:{resolved_uuid}"
        self.entities[(resource, external_id)] = {
            "provider_external_id": external_id,
            "urn": urn,
            **payload,
        }
        return models.EntityReference(
            urn=urn,
            uuid=None,
            payload={},
        )

    def upload_blob(self, account_uuid, name, content_type, data, content_hash):
        self.uploads.append((account_uuid, name, content_type, data, content_hash))
        return models.EntityReference("urn:file:" + str(uuid.uuid4()), None, {})

    def report_external_account_status(self, account_uuid, status):
        self.statuses.append((account_uuid, status))

    def list_entities(self, domain, resource, account_uuid, filters, limit):
        external_id = filters["provider_external_id"]
        entity = self.entities.get((resource, external_id))
        return [] if entity is None else [entity]

    def delete_entity(
        self,
        domain,
        resource,
        external_id,
        account_uuid,
        *,
        entity_uuid=None,
    ):
        self.deletes.append((domain, resource, external_id, account_uuid, entity_uuid))
        self.entities.pop((resource, external_id), None)


class FakeImapClient:
    metadata = protocol.FolderMetadata(5, 2, 7)
    remote_flags = {1: frozenset({b"\\Seen"})}
    calls = []
    append_uid = protocol.AppendUid(5, 1)

    def __init__(self, settings):
        self.settings = settings

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def list_folders(self):
        return [protocol.FolderInfo("INBOX", "/", "inbox", "INBOX")]

    def select(self, path):
        return self.metadata

    def fetch_since(self, path, uid):
        if uid >= 1 or 1 not in self.remote_flags:
            return []
        return [(1, self.remote_flags[1], self._message())]

    @staticmethod
    def _message():
        message = email.message.EmailMessage()
        message["From"] = "sender@example.com"
        message["To"] = "receiver@example.com"
        message["Subject"] = "Inbound"
        message.set_content("Body")
        return message.as_bytes()

    def fetch_flags(self, path, start_uid):
        return {
            uid: flags for uid, flags in self.remote_flags.items() if uid >= start_uid
        }

    def fetch_uids(self, path, uids):
        return [
            (uid, self.remote_flags[uid], self._message())
            for uid in uids
            if uid in self.remote_flags
        ]

    def create_folder(self, path):
        self.calls.append(("create_folder", path))

    def rename_folder(self, old_path, new_path):
        self.calls.append(("rename_folder", old_path, new_path))

    def delete_folder(self, path):
        self.calls.append(("delete_folder", path))

    def update_flags(self, path, uid, seen, flagged, deleted):
        self.calls.append(("update_flags", path, uid, seen, flagged, deleted))

    def move(self, source_path, target_path, uid):
        self.calls.append(("move", source_path, target_path, uid))

    def append(self, path, raw_message, flags=()):
        self.calls.append(("append", path, flags))
        return self.append_uid


class FakeSmtpClient:
    calls = []

    def __init__(self, settings):
        self.settings = settings

    def send(self, message):
        self.calls.append(("send", str(message["Message-ID"])))


def test_mail_provider_incrementally_pushes_workspace_entities():
    FakeImapClient.metadata = protocol.FolderMetadata(5, 2, 7)
    FakeImapClient.remote_flags = {1: frozenset({b"\\Seen"})}
    repository = FakeRepository()
    service = FakeServiceClient()
    daemon = provider.MailProviderDaemon(
        provider_uuid=uuid.uuid4(),
        name="Mail",
        service_client=service,
        repository=repository,
        imap_client_class=FakeImapClient,
    )
    account = models.ExternalAccount(
        uuid.uuid4(),
        models.ProviderKind.MAIL,
        {"credentials": {"username": "user", "password": "secret"}},
    )
    daemon.sync_account(account)
    assert [call[0] for call in service.upserts] == ["folders", "messages"]
    message_payload = service.upserts[1][3]
    assert message_payload["folder_urn"].startswith("urn:folder:")
    assert message_payload["subject"] == "Inbound"
    assert message_payload["seen"] is True
    assert repository.folder_states[(account.uuid, "INBOX")]["uid_validity"] == 5
    assert service.statuses == [(account.uuid, "confirmed")]


def test_mail_reconciliation_updates_flags_deletes_and_resets_uidvalidity():
    FakeImapClient.metadata = protocol.FolderMetadata(5, 2, 7)
    FakeImapClient.remote_flags = {1: frozenset({b"\\Seen"})}
    repository = FakeRepository()
    service = FakeServiceClient()
    daemon = provider.MailProviderDaemon(
        provider_uuid=uuid.uuid4(),
        name="Mail",
        service_client=service,
        repository=repository,
        imap_client_class=FakeImapClient,
    )
    account = models.ExternalAccount(
        uuid.uuid4(), models.ProviderKind.MAIL, {"credentials": {}}
    )
    daemon.sync_account(account)

    FakeImapClient.remote_flags = {1: frozenset({b"\\Flagged"})}
    daemon.sync_account(account)
    message_updates = [call for call in service.upserts if call[0] == "messages"]
    assert message_updates[-1][3]["seen"] is False
    assert message_updates[-1][3]["flagged"] is True

    FakeImapClient.metadata = protocol.FolderMetadata(6, 2, 8)
    daemon.sync_account(account)
    assert any(call[2] == "5:INBOX:1" for call in service.deletes)
    assert service.upserts[-1][1] == "6:INBOX:1"
    assert repository.folder_states[(account.uuid, "INBOX")]["uid_validity"] == 6

    FakeImapClient.remote_flags = {}
    daemon.sync_account(account)
    assert any(call[2] == "6:INBOX:1" for call in service.deletes)
    assert repository.deleted_messages[-1] == (account.uuid, "INBOX", 1)


def test_mail_commands_route_all_backend_operation_names():
    repository = FakeRepository()
    service = FakeServiceClient()
    FakeImapClient.calls = []
    FakeImapClient.append_uid = protocol.AppendUid(5, 1)
    FakeSmtpClient.calls = []
    daemon = provider.MailProviderDaemon(
        provider_uuid=uuid.uuid4(),
        name="Mail",
        service_client=service,
        repository=repository,
        imap_client_class=FakeImapClient,
        smtp_client_class=FakeSmtpClient,
    )
    account = models.ExternalAccount(
        uuid.uuid4(), models.ProviderKind.MAIL, {"credentials": {}}
    )
    folder_urn = f"urn:mail-folder:{uuid.uuid4()}"
    repository.save_entity_map(account.uuid, "mail_folder", "Target", folder_urn)

    def execute(operation, payload, external_id=None):
        entity_urn = f"urn:entity:{uuid.uuid4()}"
        if external_id is not None:
            entity_kind = (
                "mail_folder" if operation.startswith("folder.") else "mail_message"
            )
            repository.save_entity_map(
                account.uuid, entity_kind, external_id, entity_urn
            )
        command = models.ProviderCommand(
            uuid.uuid4(),
            models.ProviderDomain.MAIL,
            account.uuid,
            operation,
            entity_urn,
            payload,
        )
        result = daemon.handle_command(command, [account])
        assert result.status is models.DeliveryStatus.DELIVERED
        return result

    execute("folder.create", {"path": "New"})
    execute(
        "folder.update",
        {"path": "Renamed"},
        "Old",
    )
    execute(
        "folder.delete",
        {"path": "Removed"},
        "Removed",
    )
    message_payload = {
        "folder_urn": folder_urn,
        "from_address": "sender@example.com",
        "to_addresses": ["receiver@example.com"],
        "subject": "Draft",
        "body_text": "Body",
        "draft": True,
    }
    execute("message.create", message_payload)
    execute(
        "message.update",
        {
            **message_payload,
            "seen": True,
            "flagged": True,
        },
        "5:INBOX:1",
    )
    execute("message.delete", {}, "5:INBOX:2")
    execute(
        "message.move",
        {"folder_urn": folder_urn},
        "5:INBOX:3",
    )
    sent = execute("message.send", message_payload)

    assert [call[0] for call in FakeImapClient.calls] == [
        "create_folder",
        "rename_folder",
        "delete_folder",
        "append",
        "update_flags",
        "update_flags",
        "move",
    ]
    assert FakeSmtpClient.calls[0][0] == "send"
    assert sent.provider_external_id is None


def test_mail_send_moves_draft_mapping_to_correlated_sent_uid():
    class SentSpecialUseImapClient(FakeImapClient):
        def list_folders(self):
            return [protocol.FolderInfo("Sent", "/", "sent", "Sent")]

    repository = FakeRepository()
    service = FakeServiceClient()
    FakeImapClient.calls = []
    FakeImapClient.append_uid = protocol.AppendUid(27, 42)
    FakeSmtpClient.calls = []
    daemon = provider.MailProviderDaemon(
        provider_uuid=uuid.uuid4(),
        name="Mail",
        service_client=service,
        repository=repository,
        imap_client_class=SentSpecialUseImapClient,
        smtp_client_class=FakeSmtpClient,
    )
    account = models.ExternalAccount(
        uuid.uuid4(),
        models.ProviderKind.MAIL,
        {"credentials": {}},
    )
    message_urn = f"urn:mail-message:{uuid.uuid4()}"
    repository.save_entity_map(
        account.uuid,
        "mail_message",
        "10:Drafts:7",
        message_urn,
        provider_payload={"message_id": "<draft@example.com>"},
    )
    repository.save_mail_message_state(
        account.uuid,
        "Drafts",
        7,
        "<draft@example.com>",
        message_urn,
        "hash",
    )
    command = models.ProviderCommand(
        uuid.uuid4(),
        models.ProviderDomain.MAIL,
        account.uuid,
        "message.send",
        message_urn,
        {
            "from_address": "sender@example.com",
            "to_addresses": ["receiver@example.com"],
            "subject": "Draft",
            "body_text": "Body",
        },
    )

    result = daemon.handle_command(command, [account])

    assert result == models.CommandResult(
        models.DeliveryStatus.DELIVERED,
        provider_external_id="27:Sent:42",
    )
    assert FakeSmtpClient.calls == [("send", "<draft@example.com>")]
    assert FakeImapClient.calls == [
        ("append", "Sent", ("\\Seen",)),
        ("update_flags", "Drafts", 7, False, False, True),
    ]
    mapping = repository.get_entity_map_by_urn(
        account.uuid,
        "mail_message",
        message_urn,
    )
    assert mapping["external_key"] == "27:Sent:42"
    assert mapping["provider_payload"]["message_id"] == "<draft@example.com>"
    assert (
        repository.get_entity_map(account.uuid, "mail_message", "10:Drafts:7") is None
    )
    assert (account.uuid, "Drafts", 7) not in repository.messages
    assert repository.messages[(account.uuid, "Sent", 42)]["workspace_urn"] == (
        message_urn
    )


def test_mail_sync_reuses_provider_mapping_workspace_uuid():
    FakeImapClient.metadata = protocol.FolderMetadata(5, 2, 7)
    FakeImapClient.remote_flags = {1: frozenset({b"\\Seen"})}
    repository = FakeRepository()
    service = FakeServiceClient()
    daemon = provider.MailProviderDaemon(
        provider_uuid=uuid.uuid4(),
        name="Mail",
        service_client=service,
        repository=repository,
        imap_client_class=FakeImapClient,
    )
    account = models.ExternalAccount(
        uuid.uuid4(), models.ProviderKind.MAIL, {"credentials": {}}
    )
    folder_uuid = uuid.uuid4()
    message_uuid = uuid.uuid4()
    repository.save_entity_map(
        account.uuid,
        "mail_folder",
        "INBOX",
        f"urn:mail-folder:{folder_uuid}",
    )
    repository.save_entity_map(
        account.uuid,
        "mail_message",
        "5:INBOX:1",
        f"urn:mail-message:{message_uuid}",
    )

    daemon.sync_account(account)

    folder_upsert, message_upsert = service.upserts
    assert folder_upsert[4] == folder_uuid
    assert message_upsert[4] == message_uuid


def test_mail_draft_create_maps_appenduid_and_update_does_not_append_again():
    repository = FakeRepository()
    service = FakeServiceClient()
    FakeImapClient.calls = []
    FakeImapClient.append_uid = protocol.AppendUid(27, 42)
    daemon = provider.MailProviderDaemon(
        provider_uuid=uuid.uuid4(),
        name="Mail",
        service_client=service,
        repository=repository,
        imap_client_class=FakeImapClient,
    )
    account = models.ExternalAccount(
        uuid.uuid4(), models.ProviderKind.MAIL, {"credentials": {}}
    )
    folder_urn = f"urn:mail-folder:{uuid.uuid4()}"
    message_urn = f"urn:mail-message:{uuid.uuid4()}"
    repository.save_entity_map(account.uuid, "mail_folder", "Drafts", folder_urn)
    payload = {
        "folder_urn": folder_urn,
        "from_address": "sender@example.com",
        "to_addresses": ["receiver@example.com"],
        "subject": "Draft",
        "body_text": "Body",
        "draft": True,
    }
    create = models.ProviderCommand(
        uuid.uuid4(),
        models.ProviderDomain.MAIL,
        account.uuid,
        "message.create",
        message_urn,
        payload,
    )
    update = models.ProviderCommand(
        uuid.uuid4(),
        models.ProviderDomain.MAIL,
        account.uuid,
        "message.update",
        message_urn,
        {**payload, "seen": True},
    )

    create_result = daemon.handle_command(create, [account])
    update_result = daemon.handle_command(update, [account])

    assert create_result == models.CommandResult(
        models.DeliveryStatus.DELIVERED,
        provider_external_id="27:Drafts:42",
    )
    assert update_result.provider_external_id == "27:Drafts:42"
    assert [call[0] for call in FakeImapClient.calls].count("append") == 1
    assert FakeImapClient.calls[-1] == (
        "update_flags",
        "Drafts",
        42,
        True,
        False,
        False,
    )
    assert repository.messages[(account.uuid, "Drafts", 42)]["workspace_urn"] == (
        message_urn
    )


def test_mail_draft_create_fails_closed_without_appenduid():
    repository = FakeRepository()
    service = FakeServiceClient()
    FakeImapClient.calls = []
    FakeImapClient.append_uid = None
    daemon = provider.MailProviderDaemon(
        provider_uuid=uuid.uuid4(),
        name="Mail",
        service_client=service,
        repository=repository,
        imap_client_class=FakeImapClient,
    )
    account = models.ExternalAccount(
        uuid.uuid4(), models.ProviderKind.MAIL, {"credentials": {}}
    )
    folder_urn = f"urn:mail-folder:{uuid.uuid4()}"
    message_urn = f"urn:mail-message:{uuid.uuid4()}"
    repository.save_entity_map(account.uuid, "mail_folder", "Drafts", folder_urn)
    command = models.ProviderCommand(
        uuid.uuid4(),
        models.ProviderDomain.MAIL,
        account.uuid,
        "message.create",
        message_urn,
        {
            "folder_urn": folder_urn,
            "from_address": "sender@example.com",
            "to_addresses": ["receiver@example.com"],
            "draft": True,
        },
    )

    result = daemon.handle_command(command, [account])

    assert result.status is models.DeliveryStatus.FAILED
    assert result.provider_external_id is None
    assert "without APPENDUID" in result.error
    assert (
        repository.get_entity_map_by_urn(account.uuid, "mail_message", message_urn)
        is None
    )
