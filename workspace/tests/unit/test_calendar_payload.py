# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import pytest
from restalchemy.common import exceptions

from workspace.calendar_api import models


def test_event_payload_round_trips_utc_instants():
    payload = models.EVENT_PAYLOAD_TYPE.from_simple_type(
        {
            "kind": "event",
            "title": "Review",
            "starts_at": "2026-11-25T09:00:00Z",
            "ends_at": "2026-11-25T10:00:00Z",
        }
    )
    models.event_times(payload)
    data = models.EVENT_PAYLOAD_TYPE.to_simple_type(payload)
    assert data["starts_at"] == "2026-11-25T09:00:00.000000Z"
    assert data["ends_at"] == "2026-11-25T10:00:00.000000Z"


def test_event_payload_rejects_empty_intervals():
    with pytest.raises(exceptions.ValidationErrorException):
        models.EVENT_PAYLOAD_TYPE.from_simple_type(
            {
                "kind": "event",
                "title": "Review",
                "starts_at": "2026-11-25T09:00:00Z",
                "ends_at": "2026-11-25T09:00:00Z",
            }
        )


@pytest.mark.parametrize("starts_at", ["2026-11-25T12:00:00+03:00", "2026-11-25"])
def test_event_payload_rejects_non_utc_and_date_only(starts_at):
    with pytest.raises(exceptions.ParseError):
        models.EVENT_PAYLOAD_TYPE.from_simple_type(
            {
                "kind": "event",
                "title": "Review",
                "starts_at": starts_at,
                "ends_at": "2026-11-25T10:00:00Z",
            }
        )
