import dataclasses
import urllib.parse
import xml.etree.ElementTree as xml_etree
from xml.sax import saxutils
from typing import Any

import requests


DAV = "DAV:"
CALDAV = "urn:ietf:params:xml:ns:caldav"
APPLE = "http://apple.com/ns/ical/"
CS = "http://calendarserver.org/ns/"
NAMESPACES = {"d": DAV, "c": CALDAV, "a": APPLE, "cs": CS}


@dataclasses.dataclass(frozen=True)
class RemoteCalendar:
    href: str
    name: str
    color: str | None
    ctag: str | None
    sync_token: str | None


@dataclasses.dataclass(frozen=True)
class RemoteEvent:
    href: str
    etag: str | None
    ics: str


@dataclasses.dataclass(frozen=True)
class CalendarChanges:
    events: list[RemoteEvent]
    deleted_hrefs: list[str]
    sync_token: str | None


class CalDavClient:
    def __init__(self, settings: dict[str, Any], timeout: float = 30.0):
        self.settings = settings
        self.timeout = timeout
        credentials = settings["credentials"]
        self.auth = (credentials["username"], credentials["password"])
        self.server_url = settings["server_url"].rstrip("/") + "/"
        self._home_url = None

    def request(self, method: str, url: str, body=None, headers=None):
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

    def calendar_home_url(self) -> str:
        if self._home_url is not None:
            return self._home_url
        body = """<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
        <d:prop><c:calendar-home-set/></d:prop></d:propfind>"""
        discovery = urllib.parse.urljoin(self.server_url, "/.well-known/caldav")
        response = self.request(
            "PROPFIND",
            discovery,
            body,
            {"Depth": "0", "Content-Type": "application/xml"},
        )
        root = xml_etree.fromstring(response.content)
        href = root.findtext(".//c:calendar-home-set/d:href", namespaces=NAMESPACES)
        self._home_url = (
            urllib.parse.urljoin(response.url, href) if href else self.server_url
        )
        return self._home_url

    def calendars(self) -> list[RemoteCalendar]:
        body = """<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"
        xmlns:a="http://apple.com/ns/ical/" xmlns:cs="http://calendarserver.org/ns/">
        <d:prop><d:displayname/><d:resourcetype/><a:calendar-color/>
        <cs:getctag/><d:sync-token/></d:prop></d:propfind>"""
        response = self.request(
            "PROPFIND",
            self.calendar_home_url(),
            body,
            {"Depth": "1", "Content-Type": "application/xml"},
        )
        root = xml_etree.fromstring(response.content)
        result = []
        for item in root.findall("d:response", NAMESPACES):
            prop = item.find("d:propstat/d:prop", NAMESPACES)
            if (
                prop is None
                or prop.find("d:resourcetype/c:calendar", NAMESPACES) is None
            ):
                continue
            href = item.findtext("d:href", namespaces=NAMESPACES)
            result.append(
                RemoteCalendar(
                    urllib.parse.urljoin(response.url, href),
                    prop.findtext("d:displayname", "Calendar", NAMESPACES),
                    prop.findtext("a:calendar-color", namespaces=NAMESPACES),
                    prop.findtext("cs:getctag", namespaces=NAMESPACES),
                    prop.findtext("d:sync-token", namespaces=NAMESPACES),
                )
            )
        return result

    def events(self, calendar_url: str) -> list[RemoteEvent]:
        body = """<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
        <d:prop><d:getetag/><c:calendar-data/></d:prop>
        <c:filter><c:comp-filter name="VCALENDAR"><c:comp-filter name="VEVENT"/>
        </c:comp-filter></c:filter></c:calendar-query>"""
        response = self.request(
            "REPORT",
            calendar_url,
            body,
            {"Depth": "1", "Content-Type": "application/xml"},
        )
        root = xml_etree.fromstring(response.content)
        result = []
        for item in root.findall("d:response", NAMESPACES):
            prop = item.find("d:propstat/d:prop", NAMESPACES)
            ics = (
                prop.findtext("c:calendar-data", namespaces=NAMESPACES)
                if prop
                else None
            )
            if not ics:
                continue
            href = item.findtext("d:href", namespaces=NAMESPACES)
            result.append(
                RemoteEvent(
                    urllib.parse.urljoin(calendar_url, href),
                    prop.findtext("d:getetag", namespaces=NAMESPACES),
                    ics,
                )
            )
        return result

    def event_changes(
        self,
        calendar_url: str,
        sync_token: str,
    ) -> CalendarChanges:
        body = f"""<d:sync-collection xmlns:d="DAV:"
        xmlns:c="urn:ietf:params:xml:ns:caldav">
        <d:sync-token>{saxutils.escape(sync_token)}</d:sync-token>
        <d:sync-level>1</d:sync-level>
        <d:prop><d:getetag/><c:calendar-data/></d:prop>
        </d:sync-collection>"""
        response = self.request(
            "REPORT",
            calendar_url,
            body,
            {"Depth": "1", "Content-Type": "application/xml"},
        )
        root = xml_etree.fromstring(response.content)
        events = []
        deleted_hrefs = []
        for item in root.findall("d:response", NAMESPACES):
            href = item.findtext("d:href", namespaces=NAMESPACES)
            if href is None:
                continue
            absolute_href = urllib.parse.urljoin(calendar_url, href)
            status = item.findtext("d:status", namespaces=NAMESPACES) or ""
            if " 404 " in status:
                deleted_hrefs.append(absolute_href)
                continue
            prop = item.find("d:propstat/d:prop", NAMESPACES)
            ics = (
                prop.findtext("c:calendar-data", namespaces=NAMESPACES)
                if prop is not None
                else None
            )
            if ics:
                events.append(
                    RemoteEvent(
                        absolute_href,
                        prop.findtext("d:getetag", namespaces=NAMESPACES),
                        ics,
                    )
                )
        return CalendarChanges(
            events,
            deleted_hrefs,
            root.findtext("d:sync-token", namespaces=NAMESPACES),
        )

    def put_event(self, url: str, ics: str, etag: str | None = None):
        headers = {"Content-Type": "text/calendar; charset=utf-8"}
        headers["If-Match" if etag else "If-None-Match"] = etag or "*"
        return self.request("PUT", url, ics.encode(), headers)

    def create_calendar(
        self,
        url: str,
        name: str,
        color: str | None = None,
    ):
        color_property = (
            f"<a:calendar-color>{saxutils.escape(color)}</a:calendar-color>"
            if color
            else ""
        )
        body = f"""<c:mkcalendar xmlns:d="DAV:"
        xmlns:c="urn:ietf:params:xml:ns:caldav"
        xmlns:a="http://apple.com/ns/ical/">
        <d:set><d:prop><d:displayname>{saxutils.escape(name)}</d:displayname>{color_property}
        </d:prop></d:set></c:mkcalendar>"""
        return self.request(
            "MKCALENDAR", url, body.encode(), {"Content-Type": "application/xml"}
        )

    def update_calendar(
        self,
        url: str,
        name: str,
        color: str | None = None,
    ):
        color_property = (
            f"<a:calendar-color>{saxutils.escape(color)}</a:calendar-color>"
            if color
            else ""
        )
        body = f"""<d:propertyupdate xmlns:d="DAV:"
        xmlns:a="http://apple.com/ns/ical/">
        <d:set><d:prop><d:displayname>{saxutils.escape(name)}</d:displayname>{color_property}
        </d:prop></d:set></d:propertyupdate>"""
        return self.request(
            "PROPPATCH", url, body.encode(), {"Content-Type": "application/xml"}
        )

    def delete_calendar(self, url: str) -> None:
        self.request("DELETE", url)

    def delete_event(self, url: str, etag: str | None = None) -> None:
        headers = {"If-Match": etag} if etag else None
        self.request("DELETE", url, headers=headers)
