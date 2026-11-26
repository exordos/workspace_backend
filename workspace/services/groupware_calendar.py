# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import dataclasses
import datetime
import http
import urllib.parse
import xml.etree.ElementTree as xml

import icalendar
import requests

from workspace.services import groupware_network


DAV = "{DAV:}"
CALDAV = "{urn:ietf:params:xml:ns:caldav}"


class CalendarError(Exception):
    pass


class CalendarHTTPError(CalendarError):
    def __init__(self, status: int) -> None:
        self.status = status
        super().__init__(f"CalDAV server returned HTTP {status}")


@dataclasses.dataclass(frozen=True)
class Calendar:
    path: str
    name: str
    can_write: bool
    timezone: datetime.tzinfo = datetime.timezone.utc


@dataclasses.dataclass(frozen=True)
class Event:
    path: str
    etag: str
    data: str = dataclasses.field(repr=False)


def validate_url(url: str) -> urllib.parse.SplitResult:
    if not url or any(char.isspace() or ord(char) < 32 or char == "\\" for char in url):
        raise ValueError("Invalid CalDAV address")
    endpoint = urllib.parse.urlsplit(url)
    if (
        endpoint.scheme != "https"
        or not endpoint.hostname
        or endpoint.username is not None
        or endpoint.query
        or endpoint.fragment
    ):
        raise ValueError("CalDAV requires an HTTPS address without credentials")
    groupware_network.Endpoint(endpoint.hostname, endpoint.port or 443)
    return endpoint


def _timestamp(value: datetime.datetime) -> str:
    if value.utcoffset() is None:
        raise ValueError("CalDAV requires timezone-aware timestamps")
    return value.astimezone(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _timezone(prop: xml.Element | None) -> datetime.tzinfo:
    if prop is not None and prop.text:
        try:
            zones = icalendar.Calendar.from_ical(prop.text).walk("VTIMEZONE")
            if zones and isinstance(zones[0], icalendar.Timezone):
                return zones[0].to_tz()
        except (KeyError, TypeError, ValueError):
            pass
    return datetime.timezone.utc


class Client:
    def __init__(
        self, session: requests.Session, url: str, login: str, password: str
    ) -> None:
        self._base = validate_url(url)
        self._origin = urllib.parse.urlunsplit(self._base._replace(path=""))
        self._session = session
        self._session.trust_env = False
        self._session.mount("https://", groupware_network.HTTPSAdapter())
        self._auth = (login, password)

    def _path(self, href: str, parent: str) -> str:
        try:
            url = validate_url(urllib.parse.urljoin(self._origin + parent, href))
        except ValueError as error:
            raise CalendarError("Invalid CalDAV resource address") from error
        if (url.hostname, url.port or 443) != (
            self._base.hostname,
            self._base.port or 443,
        ):
            raise CalendarError("Unexpected CalDAV resource address")
        if url.path.startswith("//"):
            raise CalendarError("Invalid CalDAV resource path")
        return url.path or "/"

    def _request(
        self,
        method: str,
        path: str,
        headers: dict[str, str],
        statuses: tuple[int, ...],
        data: bytes | None = None,
    ) -> requests.Response:
        path = self._path(path, "/")
        for _ in range(3):
            response = self._session.request(
                method,
                self._origin + path,
                auth=self._auth,
                data=data,
                headers=headers,
                timeout=groupware_network.TIMEOUT,
                allow_redirects=False,
            )
            if response.status_code in statuses:
                return response
            if (
                method not in {"PROPFIND", "REPORT"}
                or response.status_code not in {301, 302, 307, 308}
                or "Location" not in response.headers
            ):
                raise CalendarHTTPError(response.status_code)
            path = self._path(response.headers["Location"], path)
        raise CalendarError("Too many CalDAV redirects")

    def _multistatus(
        self, method: str, path: str, depth: str, body: xml.Element
    ) -> dict[str, dict[str, xml.Element]]:
        response = self._request(
            method,
            path,
            {"Content-Type": "application/xml; charset=utf-8", "Depth": depth},
            (http.HTTPStatus.MULTI_STATUS,),
            xml.tostring(body, encoding="utf-8", xml_declaration=True),
        )
        try:
            root = xml.fromstring(response.content)
        except xml.ParseError as error:
            raise CalendarError("Invalid CalDAV response") from error
        if root.tag != DAV + "multistatus":
            raise CalendarError("Expected a CalDAV multistatus response")

        resource_path = urllib.parse.urlsplit(response.url).path
        results: dict[str, dict[str, xml.Element]] = {}
        for resource in root.findall(DAV + "response"):
            href = resource.findtext(DAV + "href")
            if not href:
                raise CalendarError("Missing CalDAV resource address")
            properties = results.setdefault(self._path(href, resource_path), {})
            for item in resource.findall(DAV + "propstat"):
                status = item.findtext(DAV + "status", "").split()
                prop = item.find(DAV + "prop")
                if len(status) > 1 and status[1] == "200" and prop is not None:
                    properties.update((element.tag, element) for element in prop)
        return results

    def _propfind(
        self, path: str, depth: str, *properties: str
    ) -> dict[str, dict[str, xml.Element]]:
        body = xml.Element(DAV + "propfind")
        prop = xml.SubElement(body, DAV + "prop")
        for name in properties:
            xml.SubElement(prop, name)
        return self._multistatus("PROPFIND", path, depth, body)

    def _property_hrefs(self, path: str, name: str) -> list[str]:
        for resource, properties in self._propfind(path, "0", name).items():
            if name in properties:
                return [
                    self._path(node.text, resource)
                    for node in properties[name].findall(DAV + "href")
                    if node.text
                ]
        return []

    def calendars(self) -> list[Calendar]:
        start = self._base.path or "/"
        try:
            principals = self._property_hrefs(
                "/.well-known/caldav" if start == "/" else start,
                DAV + "current-user-principal",
            )
        except CalendarHTTPError as error:
            if start != "/" or error.status not in {404, 405}:
                raise
            principals = self._property_hrefs(start, DAV + "current-user-principal")
        if not principals:
            raise CalendarError("CalDAV did not return an authenticated principal")
        homes = self._property_hrefs(principals[0], CALDAV + "calendar-home-set")
        if not homes:
            raise CalendarError("CalDAV did not return a calendar home")
        calendars = {}
        for home in dict.fromkeys(homes):
            results = self._propfind(
                home,
                "1",
                DAV + "resourcetype",
                DAV + "displayname",
                DAV + "current-user-privilege-set",
                CALDAV + "supported-calendar-component-set",
                CALDAV + "calendar-timezone",
            )
            for path, props in results.items():
                resource_type = props.get(DAV + "resourcetype")
                if (
                    resource_type is None
                    or resource_type.find(CALDAV + "calendar") is None
                ):
                    continue
                components = props.get(CALDAV + "supported-calendar-component-set")
                if (
                    components is not None
                    and components.find(f"{CALDAV}comp[@name='VEVENT']") is None
                ):
                    continue
                privileges = props.get(DAV + "current-user-privilege-set")
                granted: set[str] = set()
                if privileges is not None:
                    for privilege in privileges.findall(DAV + "privilege"):
                        granted.update(item.tag for item in privilege)
                write = (
                    bool(granted & {DAV + "all", DAV + "write"})
                    or {DAV + "write-content", DAV + "bind", DAV + "unbind"} <= granted
                )
                name = props.get(DAV + "displayname")
                title = name.text if name is not None and name.text else "Calendar"
                zone = _timezone(props.get(CALDAV + "calendar-timezone"))
                calendars[path] = Calendar(path, title, write, zone)
        return list(calendars.values())

    def events(
        self, calendar: Calendar, start: datetime.datetime, end: datetime.datetime
    ) -> list[Event]:
        if end <= start:
            raise ValueError("Calendar time range must be increasing")
        body = xml.Element(CALDAV + "calendar-query")
        prop = xml.SubElement(body, DAV + "prop")
        xml.SubElement(prop, DAV + "getetag")
        xml.SubElement(prop, CALDAV + "calendar-data")
        event_filter = xml.SubElement(body, CALDAV + "filter")
        component = xml.SubElement(
            event_filter, CALDAV + "comp-filter", name="VCALENDAR"
        )
        component = xml.SubElement(component, CALDAV + "comp-filter", name="VEVENT")
        xml.SubElement(
            component,
            CALDAV + "time-range",
            start=_timestamp(start),
            end=_timestamp(end),
        )
        results = self._multistatus("REPORT", calendar.path, "1", body)
        events = []
        for path, props in results.items():
            if path.rstrip("/") == calendar.path.rstrip("/"):
                continue
            data = props.get(CALDAV + "calendar-data")
            etag = props.get(DAV + "getetag")
            if data is None or not data.text or etag is None or not etag.text:
                raise CalendarError("Incomplete CalDAV event response")
            events.append(Event(path, etag.text, data.text))
        return events

    def put(self, path: str, data: str, *, etag: str | None) -> str | None:
        headers = {"Content-Type": "text/calendar; charset=utf-8"}
        if etag is None:
            headers["If-None-Match"] = "*"
        else:
            headers["If-Match"] = etag
        response = self._request(
            "PUT",
            path,
            headers,
            (http.HTTPStatus.CREATED, http.HTTPStatus.NO_CONTENT, http.HTTPStatus.OK),
            data.encode("utf-8"),
        )
        return response.headers.get("ETag")

    def get(self, path: str) -> Event:
        response = self._request("GET", path, {}, (http.HTTPStatus.OK,))
        etag = response.headers.get("ETag")
        if not etag:
            raise CalendarError("CalDAV event is missing its ETag")
        return Event(path, etag, response.content.decode("utf-8"))

    def delete(self, path: str, *, etag: str) -> None:
        self._request(
            "DELETE",
            path,
            {"If-Match": etag},
            (http.HTTPStatus.OK, http.HTTPStatus.NO_CONTENT, http.HTTPStatus.NOT_FOUND),
        )
