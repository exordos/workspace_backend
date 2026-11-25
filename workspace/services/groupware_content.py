# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import collections.abc
import dataclasses
import datetime
import email.headerregistry
import email.message
import email.parser
import email.policy
import email.utils
import typing
import uuid

import icalendar

from workspace.calendar_api import models as calendar_models
from workspace.mail_api.dm import payloads as mail_payloads


@dataclasses.dataclass(frozen=True)
class Attachment:
    name: str
    content_type: str
    body: bytes = dataclasses.field(repr=False)


@dataclasses.dataclass(frozen=True)
class IncomingMail:
    from_address: str
    to_addresses: list[str]
    cc_addresses: list[str]
    subject: str
    message_id: str
    sent_at: datetime.datetime | None
    payload: mail_payloads.MultipartPayload = dataclasses.field(repr=False)
    attachments: list[Attachment] = dataclasses.field(repr=False)


def _addresses(message: email.message.EmailMessage, field: str) -> list[str]:
    return [
        address.addr_spec
        for header in message.get_all(field, [])
        for address in getattr(header, "addresses", ())
        if address.username and address.domain and len(address.addr_spec) <= 2048
    ]


def _body_text(part: email.message.MIMEPart) -> str:
    data = typing.cast(bytes, part.get_payload(decode=True)) or b""
    try:
        return data.decode(part.get_content_charset() or "utf-8", errors="replace")
    except LookupError:
        return data.decode("utf-8", errors="replace")


def parse_mail(data: bytes) -> IncomingMail:
    parsed = typing.cast(
        email.message.EmailMessage,
        email.parser.BytesParser(policy=email.policy.default).parsebytes(data),
    )
    parts: list[mail_payloads.TextPart | mail_payloads.HtmlPart] = []
    plain = parsed.get_body(preferencelist=("plain",))
    html = parsed.get_body(preferencelist=("html",))
    if plain is not None:
        parts.append(mail_payloads.TextPart(content=_body_text(plain)))
    if html is not None:
        parts.append(mail_payloads.HtmlPart(content=_body_text(html)))
    attachments = []
    for part in parsed.walk():
        content = part.get_payload(decode=True)
        if not isinstance(content, bytes):
            continue
        name = part.get_filename()
        if name is not None or part.is_attachment():
            attachments.append(
                Attachment(name or "attachment", part.get_content_type(), content)
            )
    date = parsed["Date"]
    sent_at = (
        date.datetime if isinstance(date, email.headerregistry.DateHeader) else None
    )
    if sent_at is not None:
        sent_at = (
            sent_at.replace(tzinfo=datetime.timezone.utc)
            if sent_at.utcoffset() is None
            else sent_at.astimezone(datetime.timezone.utc)
        )
    return IncomingMail(
        from_address=next(iter(_addresses(parsed, "From")), ""),
        to_addresses=_addresses(parsed, "To"),
        cc_addresses=_addresses(parsed, "Cc"),
        subject=str(parsed.get("Subject", ""))[:2048],
        message_id=str(parsed.get("Message-ID", "")),
        sent_at=sent_at,
        payload=mail_payloads.MultipartPayload(parts=parts),
        attachments=attachments,
    )


def compose_mail(
    message_uuid: uuid.UUID,
    sender: str,
    to: list[str],
    cc: list[str],
    subject: str,
    payload: mail_payloads.MultipartPayload,
    attachments: collections.abc.Iterable[Attachment],
) -> email.message.EmailMessage:
    _, separator, domain = sender.rpartition("@")
    if not separator or not domain:
        raise ValueError("Invalid outgoing mail address")
    outgoing = email.message.EmailMessage(policy=email.policy.SMTP)
    outgoing["Message-ID"] = f"<{message_uuid}@{domain}>"
    outgoing["Date"] = email.utils.formatdate(usegmt=True)
    outgoing["From"] = sender
    if to:
        outgoing["To"] = ", ".join(to)
    if cc:
        outgoing["Cc"] = ", ".join(cc)
    outgoing["Subject"] = subject
    plain = "\n".join(part.content for part in payload.parts if part.KIND == "text")
    html = "\n".join(part.content for part in payload.parts if part.KIND == "html")
    if plain or not html:
        outgoing.set_content(plain)
        if html:
            outgoing.add_alternative(html, subtype="html")
    else:
        outgoing.set_content(html, subtype="html")
    for attachment in attachments:
        media, subtype = attachment.content_type.split("/", 1)
        outgoing.add_attachment(
            attachment.body, media, subtype, filename=attachment.name
        )
    return outgoing


def _utc(
    value: datetime.datetime | datetime.date, zone: datetime.tzinfo
) -> datetime.datetime:
    if not isinstance(value, datetime.datetime):
        value = datetime.datetime.combine(value, datetime.time(), tzinfo=zone)
    elif value.tzinfo is None:
        value = value.replace(tzinfo=zone)
    return value.astimezone(datetime.timezone.utc)


def parse_event(
    data: str, calendar_zone: datetime.tzinfo
) -> calendar_models.EventPayload | None:
    events = icalendar.Calendar.from_ical(data).walk("VEVENT")
    if len(events) != 1:
        return None
    event = events[0]
    if event.keys() & {"RRULE", "RDATE", "EXRULE", "EXDATE", "RECURRENCE-ID"}:
        return None
    if event.get("STATUS") == "CANCELLED" or "DTSTART" not in event:
        return None
    raw_start = event.decoded("DTSTART")
    if "DTEND" in event:
        raw_end = event.decoded("DTEND")
    elif "DURATION" in event:
        raw_end = raw_start + event.decoded("DURATION")
    elif not isinstance(raw_start, datetime.datetime):
        raw_end = raw_start + datetime.timedelta(days=1)
    else:
        return None
    start = _utc(raw_start, calendar_zone)
    end = _utc(raw_end, calendar_zone)
    if start >= end:
        return None
    return calendar_models.EventPayload(
        title=str(event.get("SUMMARY", ""))[:1024] or "Untitled event",
        description=str(event["DESCRIPTION"]) if "DESCRIPTION" in event else None,
        location=str(event["LOCATION"])[:2048] if "LOCATION" in event else None,
        starts_at=start,
        ends_at=end,
    )


def compose_event(
    event_uuid: uuid.UUID,
    payload: calendar_models.EventPayload,
    original: str | None = None,
) -> str:
    event: icalendar.Component
    if original is None:
        document = icalendar.Calendar()
        document.add("VERSION", "2.0")
        document.add("PRODID", "-//Exordos//Workspace//EN")
        event = icalendar.Event()
        event.add("UID", f"workspace-{event_uuid}")
        document.add_component(event)
    else:
        document, event = editable_event(original)
        sequence = int(event.pop("SEQUENCE", 0))
        event.add("SEQUENCE", sequence + 1)
    for name, value in (
        ("DTSTAMP", datetime.datetime.now(datetime.timezone.utc)),
        ("DTSTART", payload.starts_at),
        ("DTEND", payload.ends_at),
        ("SUMMARY", payload.title),
        ("DESCRIPTION", payload.description),
        ("LOCATION", payload.location),
    ):
        event.pop(name, None)
        if value is not None:
            event.add(name, value)
    event.pop("DURATION", None)
    return document.to_ical().decode("utf-8")


def editable_event(data: str) -> tuple[icalendar.Calendar, icalendar.Component]:
    document = icalendar.Calendar.from_ical(data)
    (event,) = document.walk("VEVENT")
    if "ATTENDEE" in event or event.keys() & {
        "RRULE",
        "RDATE",
        "EXRULE",
        "EXDATE",
        "RECURRENCE-ID",
    }:
        raise ValueError("Workspace edits only single events without attendees")
    return document, event
