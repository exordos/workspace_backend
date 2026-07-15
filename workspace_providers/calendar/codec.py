import datetime
from typing import Any

import icalendar
from icalendar.prop import vDDDTypes


def utc_datetime(value: datetime.date | datetime.datetime) -> datetime.datetime:
    if isinstance(value, datetime.datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=datetime.timezone.utc)
        return value.astimezone(datetime.timezone.utc)
    return datetime.datetime.combine(
        value,
        datetime.time.min,
        tzinfo=datetime.timezone.utc,
    )


def parse_ics(ics: str) -> dict[str, Any]:
    calendar = icalendar.Calendar.from_ical(ics)
    component = next(item for item in calendar.walk() if item.name == "VEVENT")
    starts = component.decoded("dtstart")
    ends = component.decoded("dtend")
    raw_attendees = component.get("attendee", [])
    if not isinstance(raw_attendees, list):
        raw_attendees = [raw_attendees]
    attendees = []
    for attendee in raw_attendees:
        attendees.append(
            {
                "email": str(attendee).removeprefix("mailto:").removeprefix("MAILTO:"),
                "name": attendee.params.get("CN"),
                "status": attendee.params.get("PARTSTAT"),
                "role": attendee.params.get("ROLE"),
            }
        )
    alarms = []
    for alarm in component.subcomponents:
        if alarm.name != "VALARM":
            continue
        trigger = alarm.get("trigger")
        alarms.append(
            {
                "action": str(alarm.get("action", "DISPLAY")),
                "trigger": trigger.to_ical().decode("utf-8") if trigger else None,
            }
        )
    recurrence = component.get("rrule")
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
        "starts_at": utc_datetime(starts).isoformat().replace("+00:00", "Z"),
        "ends_at": utc_datetime(ends).isoformat().replace("+00:00", "Z"),
        "all_day": isinstance(starts, datetime.date)
        and not isinstance(starts, datetime.datetime),
        "recurrence": (
            {"rrule": recurrence.to_ical().decode("utf-8")}
            if recurrence is not None
            else None
        ),
        "attendees": attendees,
        "alarms": alarms,
        "recurrence_id": (
            str(component.get("recurrence-id"))
            if component.get("recurrence-id") is not None
            else None
        ),
    }


def build_ics(event: dict[str, Any]) -> str:
    component = icalendar.Event()
    component.add("uid", event["uid"])
    component.add("summary", event.get("summary", ""))
    starts_at = datetime.datetime.fromisoformat(
        event["starts_at"].replace("Z", "+00:00")
    )
    ends_at = datetime.datetime.fromisoformat(event["ends_at"].replace("Z", "+00:00"))
    component.add("dtstart", starts_at.date() if event.get("all_day") else starts_at)
    component.add("dtend", ends_at.date() if event.get("all_day") else ends_at)
    for field in ("description", "location"):
        if event.get(field) is not None:
            component.add(field, event[field])
    recurrence = event.get("recurrence")
    if recurrence and recurrence.get("rrule"):
        component.add("rrule", recurrence["rrule"])
    for value in event.get("attendees", []):
        address = value["email"]
        if not address.lower().startswith("mailto:"):
            address = f"mailto:{address}"
        parameters = {
            key: value[name]
            for key, name in (("CN", "name"), ("PARTSTAT", "status"), ("ROLE", "role"))
            if value.get(name)
        }
        component.add("attendee", address, parameters=parameters)
    if event.get("recurrence_id") is not None:
        component.add("recurrence-id", event["recurrence_id"])
    for value in event.get("alarms", []):
        alarm = icalendar.Alarm()
        alarm.add("action", value.get("action", "DISPLAY"))
        if value.get("trigger") is not None:
            alarm.add("trigger", vDDDTypes.from_ical(value["trigger"]))
        component.add_component(alarm)
    calendar = icalendar.Calendar()
    calendar.add("prodid", "-//Workspace Provider//Calendar//EN")
    calendar.add("version", "2.0")
    calendar.add_component(component)
    return calendar.to_ical().decode("utf-8")
