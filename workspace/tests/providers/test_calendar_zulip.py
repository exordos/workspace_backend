import datetime
import uuid
from types import SimpleNamespace

import icalendar

from workspace_providers.calendar import codec as calendar_codec
from workspace_providers.calendar import provider as calendar_provider
from workspace_providers.common import models
from workspace_providers.common import reconciliation
from workspace_providers.zulip import codec as zulip_codec
from workspace_providers.zulip import provider as zulip_provider


def test_calendar_codec_roundtrip_preserves_typed_fields():
    event = {
        "uid": "provider-event",
        "summary": "Review",
        "description": "Architecture review",
        "location": "Room",
        "starts_at": "2026-07-15T10:00:00Z",
        "ends_at": "2026-07-15T11:00:00Z",
        "all_day": False,
        "recurrence": None,
        "attendees": [
            {
                "email": "reviewer@example.com",
                "name": "Reviewer",
                "status": "ACCEPTED",
                "role": "REQ-PARTICIPANT",
            }
        ],
        "alarms": [{"action": "DISPLAY", "trigger": "-PT15M"}],
        "recurrence_id": None,
    }
    encoded = calendar_codec.build_ics(event)
    assert isinstance(icalendar.Calendar.from_ical(encoded), icalendar.Calendar)
    decoded = calendar_codec.parse_ics(encoded)
    assert decoded["uid"] == event["uid"]
    assert decoded["summary"] == event["summary"]
    assert decoded["attendees"][0]["email"] == "reviewer@example.com"
    assert decoded["alarms"][0]["trigger"] == "-PT15M"


def test_zulip_codec_wraps_external_links_and_keeps_workspace_shape():
    payload = zulip_codec.normalize_message(
        {
            "id": 42,
            "sender_id": 7,
            "sender_full_name": "Cassi",
            "sender_email": "cassi@example.com",
            "stream_id": 3,
            "subject": "architecture",
            "content": "[outside](https://elsewhere.example/path)",
            "timestamp": 1784109600,
            "flags": ["read"],
        },
        "https://zulip.example.com",
    )
    assert payload["external_message_id"] == "42"
    assert payload["topic_external_id"] == "3:architecture"
    assert "urn:url:https://elsewhere.example/path" in payload["payload"]["content"]
    assert zulip_codec.urn("user", uuid.UUID(int=1)) == (
        "urn:user:00000000-0000-0000-0000-000000000001"
    )


class CommandRepository:
    def __init__(self, calendar_urn=None, calendar_href=None):
        self.calendar_urn = calendar_urn
        self.calendar_href = calendar_href
        self.mappings = {}

    def get_entity_map_by_urn(self, account_uuid, entity_kind, workspace_urn):
        if workspace_urn in self.mappings:
            return self.mappings[workspace_urn]
        if entity_kind == "calendar" and workspace_urn == self.calendar_urn:
            return {"external_key": self.calendar_href, "provider_payload": {}}
        return None

    def replace_entity_map(
        self,
        account_uuid,
        entity_kind,
        external_key,
        workspace_urn,
        provider_payload=None,
    ):
        self.mappings[workspace_urn] = {
            "external_key": external_key,
            "provider_payload": provider_payload or {},
        }


class FakeCalDav:
    calls = []

    def __init__(self, settings):
        self.settings = settings

    def calendar_home_url(self):
        return "https://caldav.example.com/home/"

    def create_calendar(self, *args):
        self.calls.append(("create_calendar", args))

    def delete_calendar(self, *args):
        self.calls.append(("delete_calendar", args))

    def put_event(self, *args):
        self.calls.append(("put_event", args))
        return SimpleNamespace(headers={"ETag": "etag"})


def command(account_uuid, domain, operation, payload):
    return models.ProviderCommand(
        uuid.uuid4(),
        domain,
        account_uuid,
        operation,
        f"urn:entity:{uuid.uuid4()}",
        payload,
    )


def test_calendar_commands_use_backend_operation_names_and_account_scope():
    account = models.ExternalAccount(
        uuid.uuid4(), models.ProviderKind.CALENDAR, {"credentials": {}}
    )
    calendar_urn = f"urn:calendar:{uuid.uuid4()}"
    repository = CommandRepository(
        calendar_urn, "https://caldav.example.com/home/existing/"
    )
    FakeCalDav.calls = []
    daemon = calendar_provider.CalendarProviderDaemon(
        provider_uuid=uuid.uuid4(),
        name="Calendar",
        service_client=SimpleNamespace(),
        repository=repository,
        client_class=FakeCalDav,
    )
    create_result = daemon.handle_command(
        command(
            account.uuid,
            models.ProviderDomain.CALENDAR,
            "event.create",
            {
                "calendar_urn": calendar_urn,
                "uid": "event-1",
                "summary": "Review",
                "description": None,
                "location": None,
                "starts_at": "2026-07-15T10:00:00Z",
                "ends_at": "2026-07-15T11:00:00Z",
                "all_day": False,
                "recurrence": None,
                "attendees": [],
                "alarms": [],
                "recurrence_id": None,
            },
        ),
        [account],
    )
    delete_command = command(
        account.uuid,
        models.ProviderDomain.CALENDAR,
        "calendar.delete",
        {"name": "Existing"},
    )
    repository.mappings[delete_command.entity_urn] = {
        "external_key": "https://caldav.example.com/home/existing/",
        "provider_payload": {},
    }
    delete_result = daemon.handle_command(delete_command, [account])
    assert [call[0] for call in FakeCalDav.calls] == [
        "put_event",
        "delete_calendar",
    ]
    assert create_result.provider_external_id.endswith("/event-1.ics")
    assert delete_result.status is models.DeliveryStatus.DELIVERED


class FakeZulip:
    calls = []

    def __init__(self, settings):
        self.settings = settings

    def send_message(self, message):
        self.calls.append(("send_message", message))
        return 42

    def add_reaction(self, message_id, emoji_name):
        self.calls.append(("add_reaction", message_id, emoji_name))


def test_zulip_commands_use_namespaced_operations():
    account = models.ExternalAccount(
        uuid.uuid4(), models.ProviderKind.ZULIP, {"credentials": {}}
    )
    FakeZulip.calls = []
    repository = CommandRepository()
    stream_urn = f"urn:messenger-stream:{uuid.uuid4()}"
    topic_urn = f"urn:messenger-topic:{uuid.uuid4()}"
    message_urn = f"urn:messenger-message:{uuid.uuid4()}"
    author_urn = f"urn:messenger-user:{uuid.uuid4()}"
    repository.mappings[stream_urn] = {
        "external_key": "3",
        "provider_payload": {},
    }
    repository.mappings[topic_urn] = {
        "external_key": "3:architecture",
        "provider_payload": {"name": "architecture"},
    }
    repository.mappings[message_urn] = {
        "external_key": "42",
        "provider_payload": {},
    }
    repository.mappings[author_urn] = {
        "external_key": "7",
        "provider_payload": {},
    }
    daemon = zulip_provider.ZulipProviderDaemon(
        provider_uuid=uuid.uuid4(),
        name="Zulip",
        service_client=SimpleNamespace(),
        repository=repository,
        client_class=FakeZulip,
    )
    sent = daemon.handle_command(
        command(
            account.uuid,
            models.ProviderDomain.MESSENGER,
            "message.create",
            {
                "stream_urn": stream_urn,
                "topic_urn": topic_urn,
                "payload": {"kind": "markdown", "content": "Hello"},
            },
        ),
        [account],
    )
    reacted = daemon.handle_command(
        command(
            account.uuid,
            models.ProviderDomain.MESSENGER,
            "reaction.create",
            {
                "message_urn": message_urn,
                "author_urn": author_urn,
                "emoji_name": "thumbs_up",
            },
        ),
        [account],
    )
    assert FakeZulip.calls == [
        (
            "send_message",
            {
                "type": "stream",
                "to": "3",
                "topic": "architecture",
                "content": "Hello",
            },
        ),
        ("add_reaction", 42, "thumbs_up"),
    ]
    assert sent.provider_external_id == "42"
    assert reacted.status is models.DeliveryStatus.DELIVERED


class InMemoryRepository:
    def __init__(self):
        self.maps = []
        self.collections = {}
        self.objects = {}
        self.partitions = {}
        self.queue = None
        self.clock = datetime.datetime(2026, 7, 15, tzinfo=datetime.timezone.utc)

    def save_account(self, *args):
        pass

    def now(self):
        value = self.clock
        self.clock += datetime.timedelta(seconds=1)
        return value

    def save_entity_map(
        self,
        account_uuid,
        entity_kind,
        external_key,
        workspace_urn,
        content_hash=None,
        provider_payload=None,
    ):
        self.maps = [
            row
            for row in self.maps
            if not (
                row["account_uuid"] == account_uuid
                and row["entity_kind"] == entity_kind
                and row["external_key"] == external_key
            )
        ]
        self.maps.append(
            {
                "account_uuid": account_uuid,
                "entity_kind": entity_kind,
                "external_key": external_key,
                "workspace_urn": workspace_urn,
                "provider_payload": provider_payload or {},
            }
        )

    def get_entity_map(self, account_uuid, entity_kind, external_key):
        return next(
            (
                row
                for row in self.maps
                if row["account_uuid"] == account_uuid
                and row["entity_kind"] == entity_kind
                and row["external_key"] == external_key
            ),
            None,
        )

    def get_entity_map_by_urn(self, account_uuid, entity_kind, workspace_urn):
        return next(
            (
                row
                for row in self.maps
                if row["account_uuid"] == account_uuid
                and row["entity_kind"] == entity_kind
                and row["workspace_urn"] == workspace_urn
            ),
            None,
        )

    def list_entity_maps(self, account_uuid, entity_kind):
        return [
            row
            for row in self.maps
            if row["account_uuid"] == account_uuid and row["entity_kind"] == entity_kind
        ]

    def delete_entity_map(self, account_uuid, entity_kind, external_key):
        self.maps = [
            row
            for row in self.maps
            if not (
                row["account_uuid"] == account_uuid
                and row["entity_kind"] == entity_kind
                and row["external_key"] == external_key
            )
        ]

    def replace_entity_map(
        self,
        account_uuid,
        entity_kind,
        external_key,
        workspace_urn,
        provider_payload=None,
    ):
        self.maps = [
            row
            for row in self.maps
            if not (
                row["account_uuid"] == account_uuid
                and row["entity_kind"] == entity_kind
                and row["workspace_urn"] == workspace_urn
            )
        ]
        self.save_entity_map(
            account_uuid,
            entity_kind,
            external_key,
            workspace_urn,
            provider_payload=provider_payload,
        )

    def load_partitions(self, account_uuid, entity_kind):
        value = self.partitions.get((account_uuid, entity_kind))
        return [] if value is None else [value]

    def save_partition(self, partition):
        self.partitions[(uuid.UUID(partition.account_uuid), partition.entity_kind)] = (
            partition
        )

    def get_calendar_collection_state(self, account_uuid, href):
        return self.collections.get((account_uuid, href))

    def list_calendar_collection_states(self, account_uuid):
        return [
            row
            for (row_account, _href), row in self.collections.items()
            if row_account == account_uuid
        ]

    def save_calendar_collection_state(
        self, account_uuid, href, ctag, sync_token, workspace_urn
    ):
        self.collections[(account_uuid, href)] = {
            "href": href,
            "ctag": ctag,
            "sync_token": sync_token,
            "workspace_urn": workspace_urn,
        }

    def delete_calendar_collection_state(self, account_uuid, href):
        self.collections.pop((account_uuid, href), None)

    def save_calendar_object_state(
        self,
        account_uuid,
        calendar_href,
        href,
        uid,
        recurrence_id,
        etag,
        workspace_urn,
    ):
        self.objects[(account_uuid, href)] = {
            "href": href,
            "calendar_href": calendar_href,
            "uid": uid,
            "etag": etag,
            "workspace_urn": workspace_urn,
        }

    def list_calendar_object_states(self, account_uuid, calendar_href):
        return [
            row
            for (row_account, _href), row in self.objects.items()
            if row_account == account_uuid and row["calendar_href"] == calendar_href
        ]

    def delete_calendar_object_state(self, account_uuid, href):
        self.objects.pop((account_uuid, href), None)

    def get_zulip_queue_state(self, account_uuid):
        return self.queue

    def save_zulip_queue_state(
        self, account_uuid, queue_id, last_event_id, last_message_id
    ):
        self.queue = {
            "queue_id": queue_id,
            "last_event_id": last_event_id,
            "last_message_id": last_message_id,
        }


class RecordingService:
    base_url = "https://workspace.example.com"

    def __init__(self):
        self.upserts = []
        self.deletes = []
        self.statuses = []
        self.message_flags = []
        self.uploaded_blobs = []
        self.blob_data = {}

    def _upsert(
        self,
        domain,
        resource,
        external_id,
        account_uuid,
        payload,
        entity_uuid=None,
    ):
        self.upserts.append((domain, resource, external_id, payload, entity_uuid))
        urn_type = {
            "users": "messenger-user",
            "streams": "messenger-stream",
            "topics": "messenger-topic",
            "messages": "messenger-message",
            "reactions": "messenger-reaction",
            "calendars": "calendar",
            "events": "calendar-event",
        }[resource]
        resolved_uuid = entity_uuid or uuid.uuid5(account_uuid, external_id)
        return models.EntityReference(f"urn:{urn_type}:{resolved_uuid}", None, {})

    def upsert_calendar(
        self,
        resource,
        external_id,
        account_uuid,
        payload,
        *,
        entity_uuid=None,
    ):
        return self._upsert(
            "calendar",
            resource,
            external_id,
            account_uuid,
            payload,
            entity_uuid,
        )

    def upsert_messenger(
        self,
        resource,
        external_id,
        account_uuid,
        payload,
        *,
        entity_uuid=None,
    ):
        return self._upsert(
            "messenger",
            resource,
            external_id,
            account_uuid,
            payload,
            entity_uuid,
        )

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

    def report_external_account_status(self, account_uuid, status):
        self.statuses.append((account_uuid, status))

    def sync_messenger_message_flags(
        self,
        message_uuid,
        account_uuid,
        *,
        read,
        starred,
    ):
        self.message_flags.append(
            (message_uuid, account_uuid, read, starred),
        )

    def upload_blob(
        self,
        account_uuid,
        name,
        content_type,
        data,
        content_hash,
    ):
        blob_uuid = uuid.uuid5(account_uuid, content_hash)
        urn = f"urn:file:{blob_uuid}"
        self.uploaded_blobs.append(
            (account_uuid, name, content_type, data, content_hash, urn),
        )
        self.blob_data[urn] = data
        return models.EntityReference(urn, None, {})

    def download_blob(self, urn):
        return self.blob_data[urn]


def event_ics(uid, summary):
    return calendar_codec.build_ics(
        {
            "uid": uid,
            "summary": summary,
            "description": None,
            "location": None,
            "starts_at": "2026-07-15T10:00:00Z",
            "ends_at": "2026-07-15T11:00:00Z",
            "all_day": False,
            "recurrence": None,
            "attendees": [],
            "alarms": [],
            "recurrence_id": None,
        }
    )


class IncrementalCalDav:
    calls = []

    def __init__(self, settings):
        self.settings = settings

    def calendars(self):
        return [
            SimpleNamespace(
                href="https://cal.example/main/",
                name="Main",
                color=None,
                ctag="ctag-2",
                sync_token="token-1",
            )
        ]

    def events(self, href):
        self.calls.append("full")
        return [
            SimpleNamespace(
                href=href + "one.ics", etag="e1", ics=event_ics("one", "One")
            ),
            SimpleNamespace(
                href=href + "two.ics", etag="e2", ics=event_ics("two", "Two")
            ),
        ]

    def event_changes(self, href, token):
        self.calls.append(("incremental", token))
        return SimpleNamespace(
            events=[
                SimpleNamespace(
                    href=href + "one.ics",
                    etag="e1-new",
                    ics=event_ics("one", "Changed"),
                )
            ],
            deleted_hrefs=[href + "two.ics"],
            sync_token="token-2",
        )


def test_calendar_uses_sync_token_for_incremental_updates_and_deletes():
    repository = InMemoryRepository()
    service = RecordingService()
    IncrementalCalDav.calls = []
    daemon = calendar_provider.CalendarProviderDaemon(
        provider_uuid=uuid.uuid4(),
        name="Calendar",
        service_client=service,
        repository=repository,
        client_class=IncrementalCalDav,
    )
    account = models.ExternalAccount(
        uuid.uuid4(), models.ProviderKind.CALENDAR, {"credentials": {}}
    )
    daemon.sync_account(account)
    daemon.sync_account(account)
    assert IncrementalCalDav.calls == ["full", ("incremental", "token-1")]
    assert any(call[2].endswith("two.ics") for call in service.deletes)
    assert (
        repository.collections[(account.uuid, "https://cal.example/main/")][
            "sync_token"
        ]
        == "token-2"
    )


class OutboundCalendarCalDav:
    event_deleted = False
    event_href = "https://cal.example/main/outbound.ics"

    def __init__(self, settings):
        self.settings = settings

    def put_event(self, href, ics, etag):
        assert href == self.event_href
        return SimpleNamespace(headers={"ETag": "outbound-etag"})

    def calendars(self):
        return [
            SimpleNamespace(
                href="https://cal.example/main/",
                name="Main",
                color=None,
                ctag="ctag-1",
                sync_token="token-1",
            )
        ]

    def events(self, href):
        if self.event_deleted:
            return []
        return [
            SimpleNamespace(
                href=self.event_href,
                etag="remote-etag",
                ics=event_ics("outbound", "Outbound"),
            )
        ]

    def event_changes(self, href, token):
        assert token == "token-1"
        return SimpleNamespace(
            events=[],
            deleted_hrefs=[self.event_href] if self.event_deleted else [],
            sync_token="token-2" if self.event_deleted else token,
        )


def test_calendar_outbound_event_keeps_uuid_on_sync_and_remote_delete():
    repository = InMemoryRepository()
    service = RecordingService()
    OutboundCalendarCalDav.event_deleted = False
    daemon = calendar_provider.CalendarProviderDaemon(
        provider_uuid=uuid.uuid4(),
        name="Calendar",
        service_client=service,
        repository=repository,
        client_class=OutboundCalendarCalDav,
    )
    account = models.ExternalAccount(
        uuid.uuid4(), models.ProviderKind.CALENDAR, {"credentials": {}}
    )
    calendar_uuid = uuid.uuid4()
    calendar_urn = f"urn:calendar:{calendar_uuid}"
    event_uuid = uuid.uuid4()
    event_urn = f"urn:calendar-event:{event_uuid}"
    repository.save_entity_map(
        account.uuid,
        "calendar",
        "https://cal.example/main/",
        calendar_urn,
    )
    create = models.ProviderCommand(
        uuid.uuid4(),
        models.ProviderDomain.CALENDAR,
        account.uuid,
        "event.create",
        event_urn,
        {
            "calendar_urn": calendar_urn,
            "uid": "outbound",
            "summary": "Outbound",
            "description": None,
            "location": None,
            "starts_at": "2026-07-15T10:00:00Z",
            "ends_at": "2026-07-15T11:00:00Z",
            "all_day": False,
            "recurrence": None,
            "attendees": [],
            "alarms": [],
            "recurrence_id": None,
        },
    )

    daemon.handle_command(create, [account])
    daemon.sync_account(account)

    event_upsert = next(call for call in service.upserts if call[1] == "events")
    assert event_upsert[2] == OutboundCalendarCalDav.event_href
    assert event_upsert[4] == event_uuid
    assert (
        repository.get_entity_map(
            account.uuid,
            "calendar_event",
            OutboundCalendarCalDav.event_href,
        )["workspace_urn"]
        == event_urn
    )

    OutboundCalendarCalDav.event_deleted = True
    daemon.sync_account(account)

    event_delete = next(call for call in service.deletes if call[1] == "events")
    assert event_delete[2] == OutboundCalendarCalDav.event_href
    assert event_delete[4] == event_uuid
    assert (
        repository.get_entity_map(
            account.uuid,
            "calendar_event",
            OutboundCalendarCalDav.event_href,
        )
        is None
    )


def zulip_message(message_id, content):
    return {
        "id": message_id,
        "sender_id": 7,
        "sender_full_name": "Sender User",
        "sender_email": "sender@example.com",
        "stream_id": 3,
        "recipient_id": 3,
        "subject": "architecture",
        "content": content,
        "timestamp": 1784109600,
        "flags": [],
        "reactions": [],
    }


def direct_zulip_message(message_id, content):
    message = zulip_message(message_id, content)
    message.update(
        {
            "type": "private",
            "stream_id": None,
            "recipient_id": 55,
            "subject": "",
            "display_recipient": [
                {
                    "id": 99,
                    "full_name": "Owner User",
                    "email": "owner@example.com",
                },
                {
                    "id": 7,
                    "full_name": "Sender User",
                    "email": "sender@example.com",
                },
            ],
        },
    )
    return message


def test_zulip_realm_mapping_is_shared_by_multiple_external_accounts():
    repository = InMemoryRepository()
    service = RecordingService()
    provider_uuid = uuid.uuid4()
    daemon = zulip_provider.ZulipProviderDaemon(
        provider_uuid=provider_uuid,
        name="Zulip",
        service_client=service,
        repository=repository,
        client_class=FakeZulip,
    )
    accounts = [
        models.ExternalAccount(
            uuid.uuid4(),
            models.ProviderKind.ZULIP,
            {
                "server_url": "https://zulip.example.com/",
                "credentials": {"login": "first@example.com"},
            },
        ),
        models.ExternalAccount(
            uuid.uuid4(),
            models.ProviderKind.ZULIP,
            {
                "server_url": "https://ZULIP.example.com",
                "credentials": {"login": "second@example.com"},
            },
        ),
    ]
    user_urns = [
        daemon._upsert_user(
            account,
            {
                "user_id": 7,
                "full_name": "Shared User",
                "email": "shared@example.com",
            },
        )
        for account in accounts
    ]
    assert user_urns[0] == user_urns[1]
    user_upserts = [call for call in service.upserts if call[1] == "users"]
    assert user_upserts[0][4] == user_upserts[1][4]
    assert daemon._realm_scope(accounts[0]) == daemon._realm_scope(accounts[1])


def test_zulip_direct_message_roundtrip_uses_private_workspace_stream():
    repository = InMemoryRepository()
    service = RecordingService()
    FakeZulip.calls = []
    daemon = zulip_provider.ZulipProviderDaemon(
        provider_uuid=uuid.uuid4(),
        name="Zulip",
        service_client=service,
        repository=repository,
        client_class=FakeZulip,
    )
    account = models.ExternalAccount(
        uuid.uuid4(),
        models.ProviderKind.ZULIP,
        {
            "server_url": "https://zulip.example.com",
            "credentials": {"login": "owner@example.com"},
        },
    )

    assert daemon._sync_message(account, direct_zulip_message(42, "Direct")) == 42
    stream_upsert = next(call for call in service.upserts if call[1] == "streams")
    topic_upsert = next(call for call in service.upserts if call[1] == "topics")
    assert stream_upsert[3]["private"] is True
    assert stream_upsert[3]["invite_only"] is True
    assert stream_upsert[3]["direct_user_urn"].startswith("urn:messenger-user:")
    stream_urn = f"urn:messenger-stream:{stream_upsert[4]}"
    topic_urn = f"urn:messenger-topic:{topic_upsert[4]}"
    created = daemon.handle_command(
        models.ProviderCommand(
            uuid.uuid4(),
            models.ProviderDomain.MESSENGER,
            account.uuid,
            "message.create",
            f"urn:messenger-message:{uuid.uuid4()}",
            {
                "stream_urn": stream_urn,
                "topic_urn": topic_urn,
                "payload": {"kind": "markdown", "content": "Reply"},
            },
        ),
        [account],
    )
    assert created.provider_external_id == "42"
    assert FakeZulip.calls[-1] == (
        "send_message",
        {"type": "direct", "to": "[7]", "content": "Reply"},
    )


class FileZulip(FakeZulip):
    def download_file(self, url):
        self.calls.append(("download_file", url))
        return b"remote file", "text/plain", "report.txt"

    def upload_file(self, name, content_type, data):
        self.calls.append(("upload_file", name, content_type, data))
        return f"/user_uploads/2/{name}"


def test_zulip_files_are_transferred_through_workspace_blob_urns():
    repository = InMemoryRepository()
    service = RecordingService()
    FileZulip.calls = []
    daemon = zulip_provider.ZulipProviderDaemon(
        provider_uuid=uuid.uuid4(),
        name="Zulip",
        service_client=service,
        repository=repository,
        client_class=FileZulip,
    )
    account = models.ExternalAccount(
        uuid.uuid4(),
        models.ProviderKind.ZULIP,
        {
            "server_url": "https://zulip.example.com",
            "credentials": {"login": "owner@example.com"},
        },
    )
    stream_urn = f"urn:messenger-stream:{uuid.uuid4()}"
    topic_urn = f"urn:messenger-topic:{uuid.uuid4()}"
    repository.save_entity_map(
        daemon._realm_scope(account),
        "stream",
        "3",
        stream_urn,
        provider_payload={"kind": "stream"},
    )
    repository.save_entity_map(
        daemon._realm_scope(account),
        "topic",
        "3:architecture",
        topic_urn,
        provider_payload={"name": "architecture"},
    )
    remote_message = zulip_message(
        42,
        "[report](/user_uploads/1/report.txt)",
    )
    daemon._sync_message(account, remote_message, FileZulip(account.settings))

    message_upsert = next(call for call in service.upserts if call[1] == "messages")
    imported_urn = service.uploaded_blobs[0][-1]
    assert message_upsert[3]["payload"]["content"] == f"[report]({imported_urn})"
    assert FileZulip.calls[0] == (
        "download_file",
        "https://zulip.example.com/user_uploads/1/report.txt",
    )

    workspace_urn = f"urn:file:{uuid.uuid4()}"
    service.blob_data[workspace_urn] = b"workspace file"
    daemon.handle_command(
        models.ProviderCommand(
            uuid.uuid4(),
            models.ProviderDomain.MESSENGER,
            account.uuid,
            "message.create",
            f"urn:messenger-message:{uuid.uuid4()}",
            {
                "stream_urn": stream_urn,
                "topic_urn": topic_urn,
                "payload": {
                    "kind": "markdown",
                    "content": f"[local]({workspace_urn})",
                },
            },
        ),
        [account],
    )
    assert FileZulip.calls[-2] == (
        "upload_file",
        "local",
        "application/octet-stream",
        b"workspace file",
    )
    assert FileZulip.calls[-1] == (
        "send_message",
        {
            "type": "stream",
            "to": "3",
            "topic": "architecture",
            "content": "[local](/user_uploads/2/local)",
        },
    )


class IncrementalZulip:
    calls = []
    event_round = 0

    def __init__(self, settings):
        self.settings = settings

    def current_user(self):
        self.calls.append("current_user")
        return {
            "user_id": 99,
            "full_name": "Owner User",
            "email": "owner@example.com",
        }

    def register_queue(self):
        self.calls.append("register")
        return {"queue_id": "queue", "last_event_id": 0}

    def streams(self):
        self.calls.append("streams")
        return [
            {
                "stream_id": 3,
                "name": "Engineering",
                "description": "Work",
                "invite_only": False,
                "is_archived": False,
            }
        ]

    def users(self):
        self.calls.append("users")
        return [
            {
                "user_id": 7,
                "full_name": "Sender User",
                "email": "sender@example.com",
            }
        ]

    def messages(self, anchor="newest", limit=100, **kwargs):
        self.calls.append(("messages", anchor))
        first = zulip_message(1, "Initial")
        first["reactions"] = [{"user_id": 7, "emoji_name": "thumbs_up"}]
        return [first, zulip_message(2, "Delete me")]

    def events(self, queue_id, last_event_id):
        self.calls.append(("events", last_event_id))
        type(self).event_round += 1
        if type(self).event_round == 1:
            return []
        return [
            {"id": 1, "type": "update_message", "message_id": 1},
            {"id": 2, "type": "delete_message", "message_id": 2},
        ]

    def message(self, message_id):
        self.calls.append(("message", message_id))
        return zulip_message(message_id, "Updated")


def test_zulip_queue_applies_incremental_update_and_delete_without_rescan():
    repository = InMemoryRepository()
    service = RecordingService()
    IncrementalZulip.calls = []
    IncrementalZulip.event_round = 0
    daemon = zulip_provider.ZulipProviderDaemon(
        provider_uuid=uuid.uuid4(),
        name="Zulip",
        service_client=service,
        repository=repository,
        client_class=IncrementalZulip,
    )
    account = models.ExternalAccount(
        uuid.uuid4(),
        models.ProviderKind.ZULIP,
        {"server_url": "https://zulip.example.com", "credentials": {}},
    )
    daemon.sync_account(account)
    message_scans = [
        call
        for call in IncrementalZulip.calls
        if isinstance(call, tuple) and call[0] == "messages"
    ]
    daemon.sync_account(account)
    assert [
        call
        for call in IncrementalZulip.calls
        if isinstance(call, tuple) and call[0] == "messages"
    ] == message_scans
    resources = [call[1] for call in service.upserts]
    assert resources.index("users") < resources.index("streams")
    message_updates = [
        call for call in service.upserts if call[1] == "messages" and call[2] == "1"
    ]
    assert message_updates[-1][3]["payload"]["content"] == "Updated"
    assert message_updates[-1][3]["created_at"].endswith(".000000Z")
    assert message_updates[-1][3]["author_urn"].startswith("urn:messenger-user:")
    assert any(call[1] == "messages" and call[2] == "2" for call in service.deletes)
    assert "reactions" in resources
    assert any(call[1] == "reactions" for call in service.deletes)
    assert repository.queue["last_event_id"] == 2


class PaginatedZulip:
    calls = []
    message_count = 0
    events_error = False

    def __init__(self, settings):
        self.settings = settings

    def current_user(self):
        return {
            "user_id": 99,
            "full_name": "Owner User",
            "email": "owner@example.com",
        }

    def register_queue(self):
        self.calls.append(("register",))
        return {"queue_id": "new-queue", "last_event_id": 0}

    def streams(self):
        return [
            {
                "stream_id": 3,
                "name": "Engineering",
                "description": "Work",
                "invite_only": False,
                "is_archived": False,
            }
        ]

    def users(self):
        return [
            {
                "user_id": 7,
                "full_name": "Sender User",
                "email": "sender@example.com",
            }
        ]

    def messages(
        self,
        anchor="newest",
        limit=100,
        before=None,
        after=None,
    ):
        self.calls.append(("messages", anchor, before, after))
        message_ids = list(range(1, self.message_count + 1))
        if anchor == "newest":
            selected = message_ids[-before:]
        elif after:
            selected = [value for value in message_ids if value >= int(anchor)][
                : after + 1
            ]
        else:
            selected = [value for value in message_ids if value < int(anchor)][
                -before:
            ] + [int(anchor)]
        return [zulip_message(value, f"Message {value}") for value in selected]

    def events(self, queue_id, last_event_id):
        self.calls.append(("events", queue_id, last_event_id))
        if self.events_error:
            raise RuntimeError("queue expired")
        return []


def paginated_zulip_daemon(repository, service):
    return zulip_provider.ZulipProviderDaemon(
        provider_uuid=uuid.uuid4(),
        name="Zulip",
        service_client=service,
        repository=repository,
        client_class=PaginatedZulip,
    )


def zulip_account():
    return models.ExternalAccount(
        uuid.uuid4(),
        models.ProviderKind.ZULIP,
        {"server_url": "https://zulip.example.com", "credentials": {}},
    )


def test_zulip_initial_sync_paginates_complete_history():
    repository = InMemoryRepository()
    service = RecordingService()
    PaginatedZulip.calls = []
    PaginatedZulip.message_count = 250
    PaginatedZulip.events_error = False
    daemon = paginated_zulip_daemon(repository, service)

    daemon.sync_account(zulip_account())

    message_ids = [int(call[2]) for call in service.upserts if call[1] == "messages"]
    assert message_ids == list(range(1, 251))
    assert [call[1] for call in PaginatedZulip.calls if call[0] == "messages"] == [
        "newest",
        151,
        51,
    ]
    assert repository.queue["last_message_id"] == 250


def test_zulip_expired_queue_paginates_every_message_after_cursor():
    repository = InMemoryRepository()
    service = RecordingService()
    account = zulip_account()
    repository.queue = {
        "queue_id": "expired-queue",
        "last_event_id": 42,
        "last_message_id": 10,
    }
    repository.partitions[(account.uuid, "zulip_message")] = (
        reconciliation.ReconciliationPartition(
            account_uuid=str(account.uuid),
            entity_kind="zulip_message",
            partition_key="recent",
            next_due_at=repository.clock + datetime.timedelta(days=1),
        )
    )
    PaginatedZulip.calls = []
    PaginatedZulip.message_count = 260
    PaginatedZulip.events_error = True
    daemon = paginated_zulip_daemon(repository, service)

    daemon.sync_account(account)

    message_ids = [int(call[2]) for call in service.upserts if call[1] == "messages"]
    assert message_ids == list(range(11, 261))
    assert [call[1] for call in PaginatedZulip.calls if call[0] == "messages"] == [
        10,
        110,
        210,
    ]
    assert repository.queue == {
        "queue_id": "new-queue",
        "last_event_id": 0,
        "last_message_id": 260,
    }


def test_zulip_reconciliation_paginates_dynamic_recent_depth():
    repository = InMemoryRepository()
    service = RecordingService()
    account = zulip_account()
    repository.queue = {
        "queue_id": "active-queue",
        "last_event_id": 42,
        "last_message_id": 350,
    }
    repository.partitions[(account.uuid, "zulip_message")] = (
        reconciliation.ReconciliationPartition(
            account_uuid=str(account.uuid),
            entity_kind="zulip_message",
            partition_key="recent",
            depth=3,
            next_due_at=repository.clock,
        )
    )
    PaginatedZulip.calls = []
    PaginatedZulip.message_count = 350
    PaginatedZulip.events_error = False
    daemon = paginated_zulip_daemon(repository, service)
    repository.save_entity_map(
        daemon._realm_scope(account),
        "stream",
        "3",
        f"urn:messenger-stream:{uuid.uuid4()}",
    )

    daemon.sync_account(account)

    message_ids = [int(call[2]) for call in service.upserts if call[1] == "messages"]
    assert message_ids == list(range(51, 351))
    assert [call[1] for call in PaginatedZulip.calls if call[0] == "messages"] == [
        "newest",
        251,
        151,
    ]


class OutboundZulip:
    event_round = 0

    def __init__(self, settings):
        self.settings = settings

    def send_message(self, message):
        return 42

    def current_user(self):
        return {
            "user_id": 99,
            "full_name": "Owner User",
            "email": "owner@example.com",
        }

    def register_queue(self):
        return {"queue_id": "queue", "last_event_id": 0}

    def streams(self):
        return [
            {
                "stream_id": 3,
                "name": "Engineering",
                "description": "Work",
                "invite_only": False,
                "is_archived": False,
            }
        ]

    def users(self):
        return [
            {
                "user_id": 7,
                "full_name": "Sender User",
                "email": "sender@example.com",
            }
        ]

    def messages(self, anchor="newest", limit=100, **kwargs):
        return [zulip_message(42, "Outbound")]

    def events(self, queue_id, last_event_id):
        type(self).event_round += 1
        if type(self).event_round == 1:
            return []
        return [{"id": 1, "type": "delete_message", "message_id": 42}]


def test_zulip_outbound_message_keeps_uuid_on_sync_and_remote_delete():
    repository = InMemoryRepository()
    service = RecordingService()
    OutboundZulip.event_round = 0
    daemon = zulip_provider.ZulipProviderDaemon(
        provider_uuid=uuid.uuid4(),
        name="Zulip",
        service_client=service,
        repository=repository,
        client_class=OutboundZulip,
    )
    account = models.ExternalAccount(
        uuid.uuid4(),
        models.ProviderKind.ZULIP,
        {"server_url": "https://zulip.example.com", "credentials": {}},
    )
    stream_urn = f"urn:messenger-stream:{uuid.uuid4()}"
    topic_urn = f"urn:messenger-topic:{uuid.uuid4()}"
    message_uuid = uuid.uuid4()
    message_urn = f"urn:messenger-message:{message_uuid}"
    realm_scope = daemon._realm_scope(account)
    repository.save_entity_map(realm_scope, "stream", "3", stream_urn)
    repository.save_entity_map(
        realm_scope,
        "topic",
        "3:architecture",
        topic_urn,
        provider_payload={"name": "architecture"},
    )
    create = models.ProviderCommand(
        uuid.uuid4(),
        models.ProviderDomain.MESSENGER,
        account.uuid,
        "message.create",
        message_urn,
        {
            "stream_urn": stream_urn,
            "topic_urn": topic_urn,
            "payload": {"kind": "markdown", "content": "Outbound"},
        },
    )

    daemon.handle_command(create, [account])
    daemon.sync_account(account)

    message_upsert = next(
        call for call in service.upserts if call[1] == "messages" and call[2] == "42"
    )
    assert message_upsert[4] == message_uuid
    assert (
        repository.get_entity_map(realm_scope, "message", "42")["workspace_urn"]
        == message_urn
    )

    daemon.sync_account(account)

    message_delete = next(
        call for call in service.deletes if call[1] == "messages" and call[2] == "42"
    )
    assert message_delete[4] == message_uuid
    assert repository.get_entity_map(realm_scope, "message", "42") is None
