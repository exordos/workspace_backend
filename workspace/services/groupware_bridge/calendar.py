# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import datetime
import urllib.parse
import uuid as sys_uuid
import xml.etree.ElementTree as xml_etree
from xml.sax import saxutils

import icalendar
import requests
from icalendar.prop import vDDDTypes
from restalchemy.dm import filters as dm_filters

from workspace.groupware.dm import models
from workspace.messenger_api import events as workspace_events


DAV = "DAV:"
CALDAV = "urn:ietf:params:xml:ns:caldav"
APPLE = "http://apple.com/ns/ical/"
CS = "http://calendarserver.org/ns/"
NAMESPACES = {"d": DAV, "c": CALDAV, "a": APPLE, "cs": CS}


def _utc_datetime(value):
    if isinstance(value, datetime.datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=datetime.timezone.utc)
        return value.astimezone(datetime.timezone.utc)
    return datetime.datetime.combine(
        value,
        datetime.time.min,
        tzinfo=datetime.timezone.utc,
    )


def _calendar_component(event):
    component = icalendar.Event()
    component.add("uid", event.uid)
    component.add("summary", event.summary)
    if getattr(event, "all_day", False):
        component.add("dtstart", event.starts_at.date())
        component.add("dtend", event.ends_at.date())
    else:
        component.add("dtstart", event.starts_at)
        component.add("dtend", event.ends_at)
    if event.description is not None:
        component.add("description", event.description)
    if event.location is not None:
        component.add("location", event.location)
    if event.recurrence is not None and "rrule" in event.recurrence:
        component.add("rrule", event.recurrence["rrule"])
    for attendee in event.attendees:
        address = attendee["email"]
        if not address.lower().startswith("mailto:"):
            address = f"mailto:{address}"
        parameters = {}
        if attendee.get("name"):
            parameters["CN"] = attendee["name"]
        if attendee.get("status"):
            parameters["PARTSTAT"] = attendee["status"]
        if attendee.get("role"):
            parameters["ROLE"] = attendee["role"]
        component.add("attendee", address, parameters=parameters)
    if getattr(event, "recurrence_id", None) is not None:
        component.add("recurrence-id", event.recurrence_id)
    for alarm_value in getattr(event, "alarms", []):
        alarm = icalendar.Alarm()
        alarm.add("action", alarm_value.get("action", "DISPLAY"))
        trigger = alarm_value.get("trigger")
        if trigger is not None:
            if isinstance(trigger, str):
                trigger = vDDDTypes.from_ical(trigger)
            alarm.add("trigger", trigger)
        component.add_component(alarm)
    return component


def build_ics(event):
    calendar = icalendar.Calendar()
    calendar.add("prodid", "-//Genesis Workspace//Calendar//EN")
    calendar.add("version", "2.0")
    calendar.add_component(_calendar_component(event))
    return calendar.to_ical().decode("utf-8")


def parse_ics(ics):
    calendar = icalendar.Calendar.from_ical(ics)
    component = next(item for item in calendar.walk() if item.name == "VEVENT")
    starts = component.decoded("dtstart")
    ends = component.decoded("dtend")
    attendees = []
    raw_attendees = component.get("attendee", [])
    if not isinstance(raw_attendees, list):
        raw_attendees = [raw_attendees]
    for attendee in raw_attendees:
        value = str(attendee)
        attendees.append(
            {
                "email": value.removeprefix("mailto:").removeprefix("MAILTO:"),
                "name": attendee.params.get("CN"),
                "status": attendee.params.get("PARTSTAT"),
            }
        )
    recurrence = None
    if component.get("rrule") is not None:
        recurrence = {
            "rrule": component.get("rrule").to_ical().decode("utf-8"),
        }
    alarms = []
    for alarm in component.subcomponents:
        if alarm.name != "VALARM":
            continue
        alarms.append(
            {
                "action": str(alarm.get("action", "DISPLAY")),
                "trigger": alarm.get("trigger").to_ical().decode("utf-8"),
            }
        )
    return {
        "uid": str(component.get("uid")),
        "summary": str(component.get("summary", "")),
        "description": (
            str(component.get("description"))
            if component.get("description") is not None
            else None
        ),
        "location": (
            str(component.get("location"))
            if component.get("location") is not None
            else None
        ),
        "starts_at": _utc_datetime(starts),
        "ends_at": _utc_datetime(ends),
        "all_day": isinstance(starts, datetime.date)
        and not isinstance(starts, datetime.datetime),
        "recurrence": recurrence,
        "attendees": attendees,
        "alarms": alarms,
        "recurrence_id": (
            str(component.get("recurrence-id"))
            if component.get("recurrence-id") is not None
            else None
        ),
    }


class CalDavClient:
    def __init__(self, account, timeout=30):
        self.account = account
        self.timeout = timeout
        self._calendar_home_url = None
        credentials = account.account_settings.credentials
        self.auth = (credentials.username, credentials.password)

    def _request(self, method, url, body=None, headers=None):
        response = requests.request(
            method,
            url,
            data=body,
            headers=headers,
            auth=self.auth,
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response

    @staticmethod
    def _property_href(root, property_name):
        property_node = root.find(f".//{property_name}", NAMESPACES)
        if property_node is None:
            return None
        return property_node.findtext("d:href", namespaces=NAMESPACES)

    def _discovery_url(self):
        configured = self.account.server_url.rstrip("/") + "/"
        parsed = urllib.parse.urlsplit(configured)
        if parsed.path in ("", "/"):
            return urllib.parse.urlunsplit(
                (parsed.scheme, parsed.netloc, "/.well-known/caldav", "", ""),
            )
        return configured

    def calendar_home_url(self):
        if self._calendar_home_url is not None:
            return self._calendar_home_url
        body = """<?xml version="1.0" encoding="utf-8" ?>
        <d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
          <d:prop><d:current-user-principal/><c:calendar-home-set/></d:prop>
        </d:propfind>"""
        response = self._request(
            "PROPFIND",
            self._discovery_url(),
            body=body,
            headers={"Depth": "0", "Content-Type": "application/xml"},
        )
        root = xml_etree.fromstring(response.content)
        home_href = self._property_href(root, "c:calendar-home-set")
        if home_href is None:
            principal_href = self._property_href(root, "d:current-user-principal")
            if principal_href is not None:
                principal_url = urllib.parse.urljoin(response.url, principal_href)
                response = self._request(
                    "PROPFIND",
                    principal_url,
                    body=body,
                    headers={"Depth": "0", "Content-Type": "application/xml"},
                )
                root = xml_etree.fromstring(response.content)
                home_href = self._property_href(root, "c:calendar-home-set")
        self._calendar_home_url = (
            urllib.parse.urljoin(response.url, home_href)
            if home_href is not None
            else self.account.server_url
        )
        return self._calendar_home_url

    def calendars(self):
        body = """<?xml version="1.0" encoding="utf-8" ?>
        <d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"
          xmlns:a="http://apple.com/ns/ical/" xmlns:cs="http://calendarserver.org/ns/">
          <d:prop><d:displayname/><d:resourcetype/><a:calendar-color/>
          <cs:getctag/><d:sync-token/></d:prop></d:propfind>"""
        response = self._request(
            "PROPFIND",
            self.calendar_home_url(),
            body=body,
            headers={"Depth": "1", "Content-Type": "application/xml"},
        )
        root = xml_etree.fromstring(response.content)
        calendars = []
        for item in root.findall("d:response", NAMESPACES):
            prop = item.find("d:propstat/d:prop", NAMESPACES)
            if (
                prop is None
                or prop.find("d:resourcetype/c:calendar", NAMESPACES) is None
            ):
                continue
            href = item.findtext("d:href", namespaces=NAMESPACES)
            calendars.append(
                {
                    "href": urllib.parse.urljoin(self.account.server_url, href),
                    "name": prop.findtext(
                        "d:displayname", default="Calendar", namespaces=NAMESPACES
                    ),
                    "color": prop.findtext("a:calendar-color", namespaces=NAMESPACES),
                    "ctag": prop.findtext("cs:getctag", namespaces=NAMESPACES),
                    "sync_token": prop.findtext("d:sync-token", namespaces=NAMESPACES),
                }
            )
        return calendars

    def events(self, calendar_url):
        body = """<?xml version="1.0" encoding="utf-8" ?>
        <c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
          <d:prop><d:getetag/><c:calendar-data/></d:prop>
          <c:filter><c:comp-filter name="VCALENDAR"><c:comp-filter name="VEVENT"/>
          </c:comp-filter></c:filter></c:calendar-query>"""
        response = self._request(
            "REPORT",
            calendar_url,
            body=body,
            headers={"Depth": "1", "Content-Type": "application/xml"},
        )
        root = xml_etree.fromstring(response.content)
        result = []
        for item in root.findall("d:response", NAMESPACES):
            prop = item.find("d:propstat/d:prop", NAMESPACES)
            if prop is None:
                continue
            ics = prop.findtext("c:calendar-data", namespaces=NAMESPACES)
            if not ics:
                continue
            href = item.findtext("d:href", namespaces=NAMESPACES)
            result.append(
                {
                    "href": urllib.parse.urljoin(calendar_url, href),
                    "etag": prop.findtext("d:getetag", namespaces=NAMESPACES),
                    "ics": ics,
                }
            )
        return result

    def put(self, url, ics, etag=None):
        headers = {"Content-Type": "text/calendar; charset=utf-8"}
        headers["If-Match" if etag else "If-None-Match"] = etag or "*"
        return self._request("PUT", url, body=ics.encode("utf-8"), headers=headers)

    def delete(self, url, etag=None):
        headers = {"If-Match": etag} if etag else None
        return self._request("DELETE", url, headers=headers)

    def create_calendar(self, name, color, slug):
        url = urllib.parse.urljoin(
            self.calendar_home_url().rstrip("/") + "/",
            urllib.parse.quote(slug, safe="") + "/",
        )
        safe_name = saxutils.escape(name)
        color_property = (
            f"<a:calendar-color>{saxutils.escape(color)}</a:calendar-color>"
            if color
            else ""
        )
        body = f"""<?xml version="1.0" encoding="utf-8" ?>
        <c:mkcalendar xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"
          xmlns:a="http://apple.com/ns/ical/">
          <d:set><d:prop><d:displayname>{safe_name}</d:displayname>
          <c:supported-calendar-component-set><c:comp name="VEVENT"/>
          </c:supported-calendar-component-set>{color_property}</d:prop></d:set>
        </c:mkcalendar>"""
        self._request(
            "MKCALENDAR",
            url,
            body=body.encode("utf-8"),
            headers={"Content-Type": "application/xml; charset=utf-8"},
        )
        return url

    def update_calendar(self, url, name, color):
        safe_name = saxutils.escape(name)
        color_property = (
            f"<a:calendar-color>{saxutils.escape(color)}</a:calendar-color>"
            if color
            else ""
        )
        body = f"""<?xml version="1.0" encoding="utf-8" ?>
        <d:propertyupdate xmlns:d="DAV:" xmlns:a="http://apple.com/ns/ical/">
          <d:set><d:prop><d:displayname>{safe_name}</d:displayname>
          {color_property}</d:prop></d:set>
        </d:propertyupdate>"""
        self._request(
            "PROPPATCH",
            url,
            body=body.encode("utf-8"),
            headers={"Content-Type": "application/xml; charset=utf-8"},
        )


class CalendarSynchronizer:
    def __init__(self, client_class=CalDavClient):
        self.client_class = client_class

    @staticmethod
    def _upsert_calendar(account, remote):
        calendar = models.Calendar.objects.get_one_or_none(
            filters={
                "external_user_uuid": dm_filters.EQ(account.uuid),
                "source_name": dm_filters.EQ(models.CalendarSource.CALDAV.value),
                "source": dm_filters.EQ({"href": remote["href"]}),
            },
        )
        values = {
            "name": remote["name"],
            "color": remote["color"],
            "ctag": remote["ctag"],
            "sync_token": remote["sync_token"],
        }
        if calendar is None:
            calendar = models.Calendar(
                uuid=sys_uuid.uuid4(),
                project_id=account.project_id,
                user_uuid=account.user_uuid,
                external_user_uuid=account.uuid,
                source_name=models.CalendarSource.CALDAV.value,
                source={"href": remote["href"]},
                **values,
            )
            calendar.insert()
            workspace_events.create_groupware_event(
                calendar,
                workspace_events.CALENDAR_CREATED_EVENT,
            )
            return calendar
        changed = any(
            getattr(calendar, name) != value
            for name, value in values.items()
        )
        calendar.update_dm(values=values)
        calendar.update()
        if changed:
            workspace_events.create_groupware_event(
                calendar,
                workspace_events.CALENDAR_UPDATED_EVENT,
            )
        return calendar

    @staticmethod
    def _upsert_event(account, calendar, remote):
        values = parse_ics(remote["ics"])
        matching_events = models.CalendarEvent.objects.get_all(
            filters={
                "calendar_uuid": dm_filters.EQ(calendar.uuid),
                "uid": dm_filters.EQ(values["uid"]),
            },
        )
        event = next(
            (
                candidate
                for candidate in matching_events
                if candidate.recurrence_id == values["recurrence_id"]
            ),
            None,
        )
        values.update(
            {
                "external_user_uuid": account.uuid,
                "ics": remote["ics"],
                "etag": remote["etag"],
                "source_name": models.CalendarSource.CALDAV.value,
                "source": {"href": remote["href"]},
                "sync_status": models.SyncStatus.SYNCED.value,
                "sync_error": None,
                "deleted": False,
            }
        )
        if event is None:
            event = models.CalendarEvent(
                uuid=sys_uuid.uuid4(),
                project_id=account.project_id,
                user_uuid=account.user_uuid,
                calendar_uuid=calendar.uuid,
                **values,
            )
            event.insert()
            workspace_events.create_groupware_event(
                event,
                workspace_events.CALENDAR_EVENT_CREATED_EVENT,
            )
            return event
        if event.sync_status == models.SyncStatus.PENDING.value:
            return event
        if event.etag == remote["etag"] and not event.deleted:
            return event
        event.update_dm(values=values)
        event.update()
        workspace_events.create_groupware_event(
            event,
            workspace_events.CALENDAR_EVENT_UPDATED_EVENT,
        )
        return event

    @staticmethod
    def _event_url(calendar, event):
        return urllib.parse.urljoin(
            calendar.source["href"].rstrip("/") + "/",
            urllib.parse.quote(event.uid, safe="") + ".ics",
        )

    def _sync_outbound(self, account, client):
        events = models.CalendarEvent.objects.get_all(
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
        for event in events:
            event.update_dm(values={"sync_status": models.SyncStatus.PROCESSING.value})
            event.update()
            try:
                calendar = models.Calendar.objects.get_one(
                    filters={"uuid": dm_filters.EQ(event.calendar_uuid)},
                )
                url = event.source.get("href") or self._event_url(calendar, event)
                if event.deleted:
                    client.delete(url, event.etag)
                else:
                    ics = event.ics or build_ics(event)
                    response = client.put(url, ics, event.etag)
                    event.update_dm(
                        values={
                            "ics": ics,
                            "etag": response.headers.get("ETag"),
                            "source": {"href": url},
                        },
                    )
                event.update_dm(
                    values={
                        "sync_status": models.SyncStatus.SYNCED.value,
                        "sync_error": None,
                    },
                )
                event.update()
                workspace_events.create_groupware_event(
                    event,
                    workspace_events.CALENDAR_EVENT_UPDATED_EVENT,
                )
            except Exception as exc:
                event.update_dm(
                    values={
                        "sync_status": models.SyncStatus.FAILED.value,
                        "sync_error": str(exc),
                    },
                )
                event.update()
                workspace_events.create_groupware_event(
                    event,
                    workspace_events.CALENDAR_EVENT_UPDATED_EVENT,
                )

    def _sync_outbound_calendars(self, account, client):
        calendars = models.Calendar.objects.get_all(
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
        for local_calendar in calendars:
            local_calendar.update_dm(
                values={"sync_status": models.SyncStatus.PROCESSING.value},
            )
            local_calendar.update()
            href = local_calendar.source.get("href")
            try:
                if local_calendar.deleted:
                    if href is not None:
                        client.delete(href)
                    local_calendar.delete()
                    continue
                if href is None:
                    href = client.create_calendar(
                        local_calendar.name,
                        local_calendar.color,
                        str(local_calendar.uuid),
                    )
                else:
                    client.update_calendar(
                        href,
                        local_calendar.name,
                        local_calendar.color,
                    )
                local_calendar.update_dm(
                    values={
                        "source_name": models.CalendarSource.CALDAV.value,
                        "source": {"href": href},
                        "sync_status": models.SyncStatus.SYNCED.value,
                        "sync_error": None,
                    },
                )
                local_calendar.update()
                workspace_events.create_groupware_event(
                    local_calendar,
                    workspace_events.CALENDAR_UPDATED_EVENT,
                )
            except Exception as exc:
                local_calendar.update_dm(
                    values={
                        "sync_status": models.SyncStatus.FAILED.value,
                        "sync_error": str(exc),
                    },
                )
                local_calendar.update()
                workspace_events.create_groupware_event(
                    local_calendar,
                    workspace_events.CALENDAR_UPDATED_EVENT,
                )
    def sync(self, account):
        client = self.client_class(account)
        self._sync_outbound_calendars(account, client)
        remote_calendars = client.calendars()
        remote_calendar_hrefs = {item["href"] for item in remote_calendars}
        for remote_calendar in remote_calendars:
            calendar = self._upsert_calendar(account, remote_calendar)
            remote_events = client.events(remote_calendar["href"])
            remote_event_hrefs = {item["href"] for item in remote_events}
            for remote_event in remote_events:
                self._upsert_event(account, calendar, remote_event)
            local_events = models.CalendarEvent.objects.get_all(
                filters={
                    "calendar_uuid": dm_filters.EQ(calendar.uuid),
                    "external_user_uuid": dm_filters.EQ(account.uuid),
                    "source_name": dm_filters.EQ(
                        models.CalendarSource.CALDAV.value,
                    ),
                    "sync_status": dm_filters.EQ(
                        models.SyncStatus.SYNCED.value,
                    ),
                    "deleted": dm_filters.EQ(False),
                },
            )
            for event in local_events:
                if event.source.get("href") not in remote_event_hrefs:
                    event.update_dm(values={"deleted": True})
                    event.update()
                    workspace_events.create_groupware_deleted_event(
                        event.project_id,
                        event.user_uuid,
                        event.uuid,
                        workspace_events.CALENDAR_EVENT_DELETED_EVENT,
                    )
        local_calendars = models.Calendar.objects.get_all(
            filters={
                "external_user_uuid": dm_filters.EQ(account.uuid),
                "source_name": dm_filters.EQ(models.CalendarSource.CALDAV.value),
                "sync_status": dm_filters.EQ(models.SyncStatus.SYNCED.value),
                "deleted": dm_filters.EQ(False),
            },
        )
        for local_calendar in local_calendars:
            if local_calendar.source.get("href") not in remote_calendar_hrefs:
                local_calendar.update_dm(values={"deleted": True})
                local_calendar.update()
                workspace_events.create_groupware_deleted_event(
                    local_calendar.project_id,
                    local_calendar.user_uuid,
                    local_calendar.uuid,
                    workspace_events.CALENDAR_DELETED_EVENT,
                )
        self._sync_outbound(account, client)
