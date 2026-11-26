# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import uuid as sys_uuid


CALENDARS = "/v1/calendar/calendars/"
EVENTS = "/v1/calendar/events/"


def test_calendar_event_crud_filters_and_move_are_owner_scoped(workspace_api):
    api = workspace_api
    first = api.post(CALENDARS, json={"name": "Personal"})
    second = api.post(CALENDARS, json={"name": "Other"})
    assert first.status_code == second.status_code == 201
    first_uuid = first.json()["uuid"]
    second_uuid = second.json()["uuid"]
    first_page = api.get(CALENDARS, params={"page_limit": 1})
    assert first_page.status_code == 200, first_page.text
    marker = first_page.headers["X-Pagination-Marker"]
    last_page = api.get(CALENDARS, params={"page_limit": 1, "page_marker": marker})
    assert last_page.status_code == 200, last_page.text
    assert {first_page.json()[0]["uuid"], last_page.json()[0]["uuid"]} == {
        first_uuid,
        second_uuid,
    }
    assert "X-Pagination-Marker" not in last_page.headers
    payload = {
        "kind": "event",
        "title": "Review",
        "starts_at": "2026-11-25T09:00:00Z",
        "ends_at": "2026-11-25T10:00:00Z",
    }
    response = api.post(EVENTS, json={"calendar_uuid": first_uuid, "payload": payload})
    assert response.status_code == 201, response.text
    event = response.json()
    assert event["payload"]["starts_at"] == "2026-11-25T09:00:00.000000Z"
    assert "starts_at" not in event
    url = EVENTS + event["uuid"]
    assert api.get(url, user=sys_uuid.uuid4()).status_code == 404
    assert api.get(EVENTS, params={"ends_after": payload["ends_at"]}).json() == []
    filtered = api.get(EVENTS, params={"starts_before": payload["ends_at"]})
    assert filtered.status_code == 200, filtered.text
    assert filtered.json() == [event]
    assert api.delete(CALENDARS + first_uuid).status_code == 400
    moved = api.post(url + "/actions/move/invoke", json={"calendar_uuid": second_uuid})
    assert moved.status_code == 200, moved.text
    assert moved.json()["calendar_uuid"] == second_uuid
    updated = api.put(url, json={"payload": {**payload, "title": "Updated"}})
    assert updated.status_code == 200, updated.text
    assert updated.json()["payload"]["title"] == "Updated"
    assert api.delete(url).status_code == 204
    assert api.get(url).status_code == 404
    assert api.delete(CALENDARS + first_uuid).status_code == 204
    changes = api.get("/v1/events/", params={"object_type": "calendar_event"})
    assert changes.status_code == 200, changes.text
    assert [item["payload"]["kind"] for item in changes.json()] == [
        "calendar.event.created",
        "calendar.event.updated",
        "calendar.event.updated",
        "calendar.event.deleted",
    ]


def test_unlinking_account_preserves_calendar_and_events(workspace_api):
    api = workspace_api
    accounts = "/v1/external_accounts/"
    permissions = (
        "workspace.external_account.create",
        "workspace.external_account.delete",
    )
    account = api.post(
        accounts, permissions=permissions, json={"provider": "google", "settings": {}}
    )
    assert account.status_code == 201, account.text
    assert account.json()["capabilities"] == ["mail", "calendar"]
    account_uuid = account.json()["uuid"]
    calendar = api.post(
        CALENDARS, json={"name": "Personal", "external_account_uuid": account_uuid}
    )
    assert calendar.status_code == 201, calendar.text
    assert calendar.json()["external_account_uuid"] == account_uuid
    calendar_uuid = calendar.json()["uuid"]
    event = api.post(
        EVENTS,
        json={
            "calendar_uuid": calendar_uuid,
            "payload": {
                "kind": "event",
                "title": "Review",
                "starts_at": "2026-11-25T09:00:00Z",
                "ends_at": "2026-11-25T10:00:00Z",
            },
        },
    )
    assert event.status_code == 201, event.text
    deleted = api.delete(accounts + account_uuid, permissions=permissions)
    assert deleted.status_code == 204, deleted.text
    kept = api.get(CALENDARS + calendar_uuid)
    assert kept.status_code == 200, kept.text
    assert "external_account_uuid" not in kept.json()
    assert api.get(EVENTS + event.json()["uuid"]).json() == event.json()
