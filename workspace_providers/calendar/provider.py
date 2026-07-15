import urllib.parse
import uuid

from workspace_providers.calendar import codec
from workspace_providers.calendar import protocol
from workspace_providers.common import daemon
from workspace_providers.common import models
from workspace_providers.common import reconciliation


class CalendarProviderDaemon(daemon.ProviderDaemon):
    provider_kind = models.ProviderKind.CALENDAR
    provider_domain = models.ProviderDomain.CALENDAR

    def __init__(
        self,
        *args,
        client_class=protocol.CalDavClient,
        scheduler: reconciliation.DynamicReconciliationScheduler | None = None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.client_class = client_class
        self.scheduler = scheduler or reconciliation.DynamicReconciliationScheduler()

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

    def _upsert_event(self, account, remote_calendar, calendar_urn, remote_event):
        payload = codec.parse_ics(remote_event.ics)
        payload["calendar_urn"] = calendar_urn
        event = self.client.upsert_calendar(
            "events",
            remote_event.href,
            account.uuid,
            payload,
            entity_uuid=self._mapped_entity_uuid(
                account.uuid,
                "calendar_event",
                remote_event.href,
            ),
        )
        self.repository.save_entity_map(
            account.uuid,
            "calendar_event",
            remote_event.href,
            event.urn,
            provider_payload={"href": remote_event.href, "etag": remote_event.etag},
        )
        self.repository.save_calendar_object_state(
            account.uuid,
            remote_calendar.href,
            remote_event.href,
            payload["uid"],
            payload["recurrence_id"],
            remote_event.etag,
            event.urn,
        )

    def _delete_event(self, account, href):
        self.client.delete_entity(
            models.ProviderDomain.CALENDAR,
            "events",
            href,
            account.uuid,
            entity_uuid=self._mapped_entity_uuid(
                account.uuid,
                "calendar_event",
                href,
            ),
        )
        self.repository.delete_calendar_object_state(account.uuid, href)
        self.repository.delete_entity_map(account.uuid, "calendar_event", href)

    def _full_reconcile(self, account, caldav, remote_calendar, calendar_urn):
        remote_events = caldav.events(remote_calendar.href)
        remote_by_href = {event.href: event for event in remote_events}
        local = {
            row["href"]: row
            for row in self.repository.list_calendar_object_states(
                account.uuid, remote_calendar.href
            )
        }
        mismatches = 0
        for href, remote_event in remote_by_href.items():
            row = local.get(href)
            if row is None or row["etag"] != remote_event.etag:
                self._upsert_event(account, remote_calendar, calendar_urn, remote_event)
                mismatches += 1
        for href in set(local) - set(remote_by_href):
            self._delete_event(account, href)
            mismatches += 1
        return mismatches, len(remote_events)

    def _incremental_sync(
        self, account, caldav, remote_calendar, calendar_urn, sync_token
    ):
        changes = caldav.event_changes(remote_calendar.href, sync_token)
        for remote_event in changes.events:
            self._upsert_event(account, remote_calendar, calendar_urn, remote_event)
        known_hrefs = {
            row["href"]
            for row in self.repository.list_calendar_object_states(
                account.uuid, remote_calendar.href
            )
        }
        for href in changes.deleted_hrefs:
            if href in known_hrefs:
                self._delete_event(account, href)
        return changes, len(changes.events) + len(changes.deleted_hrefs)

    def sync_account(self, account: models.ExternalAccount) -> None:
        self.repository.save_account(account.uuid, account.settings, "syncing")
        caldav = self.client_class(account.settings)
        remote_calendars = caldav.calendars()
        remote_hrefs = {item.href for item in remote_calendars}
        for remote_calendar in remote_calendars:
            calendar = self.client.upsert_calendar(
                "calendars",
                remote_calendar.href,
                account.uuid,
                {"name": remote_calendar.name, "color": remote_calendar.color},
                entity_uuid=self._mapped_entity_uuid(
                    account.uuid,
                    "calendar",
                    remote_calendar.href,
                ),
            )
            self.repository.save_entity_map(
                account.uuid,
                "calendar",
                remote_calendar.href,
                calendar.urn,
                provider_payload={"href": remote_calendar.href},
            )
            stored = self.repository.get_calendar_collection_state(
                account.uuid, remote_calendar.href
            )
            now = self.repository.now()
            partitions = self.repository.load_partitions(account.uuid, "calendar_event")
            partition = next(
                (
                    item
                    for item in partitions
                    if item.partition_key == remote_calendar.href
                ),
                reconciliation.ReconciliationPartition(
                    account_uuid=str(account.uuid),
                    entity_kind="calendar_event",
                    partition_key=remote_calendar.href,
                    next_due_at=now,
                ),
            )
            reconciliation_due = bool(
                self.scheduler.select([partition], now, budget=partition.estimated_cost)
            )
            mismatches = 0
            cost = 0
            next_token = remote_calendar.sync_token
            incremental_succeeded = False
            incremental_failed = False
            if stored is not None and stored["sync_token"]:
                try:
                    changes, cost = self._incremental_sync(
                        account,
                        caldav,
                        remote_calendar,
                        calendar.urn,
                        stored["sync_token"],
                    )
                except Exception:
                    incremental_failed = True
                    next_token = None
                else:
                    incremental_succeeded = True
                    mismatches = cost
                    next_token = changes.sync_token or next_token
            needs_full = (
                stored is None
                or incremental_failed
                or reconciliation_due
                or (
                    not incremental_succeeded and stored["ctag"] != remote_calendar.ctag
                )
            )
            if needs_full:
                mismatches, cost = self._full_reconcile(
                    account, caldav, remote_calendar, calendar.urn
                )
            self.repository.save_calendar_collection_state(
                account.uuid,
                remote_calendar.href,
                remote_calendar.ctag,
                next_token,
                calendar.urn,
            )
            if reconciliation_due or needs_full:
                self.repository.save_partition(
                    self.scheduler.complete(
                        partition,
                        now,
                        mismatches=mismatches,
                        actual_cost=max(0.01, cost / 100),
                        cursor=next_token,
                    )
                )
        for stored in self.repository.list_calendar_collection_states(account.uuid):
            if stored["href"] in remote_hrefs:
                continue
            for event in self.repository.list_calendar_object_states(
                account.uuid, stored["href"]
            ):
                self._delete_event(account, event["href"])
            self.client.delete_entity(
                models.ProviderDomain.CALENDAR,
                "calendars",
                stored["href"],
                account.uuid,
                entity_uuid=self._mapped_entity_uuid(
                    account.uuid,
                    "calendar",
                    stored["href"],
                ),
            )
            self.repository.delete_calendar_collection_state(
                account.uuid, stored["href"]
            )
            self.repository.delete_entity_map(account.uuid, "calendar", stored["href"])
        self.repository.save_account(account.uuid, account.settings, "active")
        self.client.report_external_account_status(account.uuid, "confirmed")

    @staticmethod
    def _account_for_command(command, accounts):
        return next(
            account
            for account in accounts
            if account.uuid == command.external_account_uuid
        )

    def _calendar_href(self, account_uuid, calendar_urn: str) -> str:
        row = self.repository.get_entity_map_by_urn(
            account_uuid, "calendar", calendar_urn
        )
        if row is None:
            raise ValueError(f"Unknown calendar: {calendar_urn}")
        return row["external_key"]

    def _command_mapping(self, command, entity_kind):
        return self.repository.get_entity_map_by_urn(
            command.external_account_uuid, entity_kind, command.entity_urn
        )

    @staticmethod
    def _event_href(calendar_href: str, uid: str) -> str:
        return urllib.parse.urljoin(
            calendar_href.rstrip("/") + "/",
            urllib.parse.quote(uid, safe="") + ".ics",
        )

    def handle_command(self, command, accounts):
        account = self._account_for_command(command, accounts)
        caldav = self.client_class(account.settings)
        payload = command.payload
        if command.operation.startswith("calendar."):
            mapping = self._command_mapping(command, "calendar")
            href = None if mapping is None else mapping["external_key"]
            if command.operation == "calendar.create":
                href = urllib.parse.urljoin(
                    caldav.calendar_home_url().rstrip("/") + "/",
                    command.entity_urn.rsplit(":", 1)[-1] + "/",
                )
                caldav.create_calendar(href, payload["name"], payload.get("color"))
            elif command.operation == "calendar.update":
                if href is None:
                    raise ValueError("Calendar update lacks provider_external_id")
                caldav.update_calendar(href, payload["name"], payload.get("color"))
            elif command.operation == "calendar.delete":
                if href is None:
                    raise ValueError("Calendar delete lacks provider_external_id")
                caldav.delete_calendar(href)
            else:
                raise ValueError(f"Unsupported calendar operation: {command.operation}")
            if command.operation != "calendar.delete":
                self.repository.replace_entity_map(
                    account.uuid,
                    "calendar",
                    href,
                    command.entity_urn,
                    {"href": href},
                )
            return models.CommandResult(
                models.DeliveryStatus.DELIVERED,
                provider_external_id=href,
            )
        if command.operation.startswith("event."):
            mapping = self._command_mapping(command, "calendar_event")
            old_href = None if mapping is None else mapping["external_key"]
            etag = None if mapping is None else mapping["provider_payload"].get("etag")
            if command.operation == "event.delete":
                if old_href is None:
                    raise ValueError("Event delete lacks provider_external_id")
                caldav.delete_event(old_href, etag)
                return models.CommandResult(models.DeliveryStatus.DELIVERED)
            calendar_href = self._calendar_href(account.uuid, payload["calendar_urn"])
            href = old_href or self._event_href(calendar_href, payload["uid"])
            if command.operation == "event.move":
                target_href = self._event_href(calendar_href, payload["uid"])
                if old_href is None:
                    raise ValueError("Event move lacks provider_external_id")
                target_etag = etag if target_href == old_href else None
                response = caldav.put_event(
                    target_href, codec.build_ics(payload), target_etag
                )
                if target_href != old_href:
                    caldav.delete_event(old_href, etag)
                new_etag = response.headers.get("ETag")
                self.repository.replace_entity_map(
                    account.uuid,
                    "calendar_event",
                    target_href,
                    command.entity_urn,
                    {"href": target_href, "etag": new_etag},
                )
                return models.CommandResult(
                    models.DeliveryStatus.DELIVERED,
                    provider_external_id=target_href,
                )
            elif command.operation not in ("event.create", "event.update"):
                raise ValueError(f"Unsupported calendar operation: {command.operation}")
            response = caldav.put_event(href, codec.build_ics(payload), etag)
            self.repository.replace_entity_map(
                account.uuid,
                "calendar_event",
                href,
                command.entity_urn,
                {"href": href, "etag": response.headers.get("ETag")},
            )
            return models.CommandResult(
                models.DeliveryStatus.DELIVERED,
                provider_external_id=href,
            )
        raise ValueError(f"Unsupported calendar operation: {command.operation}")
