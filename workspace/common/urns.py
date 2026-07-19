# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import uuid as sys_uuid


MAIL_FOLDER = "mail-folder"
MAIL_MESSAGE = "mail-message"
CALENDAR = "calendar"
CALENDAR_EVENT = "calendar-event"
MESSENGER_USER = "messenger-user"
MESSENGER_STREAM = "messenger-stream"
MESSENGER_TOPIC = "messenger-topic"
MESSENGER_MESSAGE = "messenger-message"
MESSENGER_REACTION = "messenger-reaction"
FILE = "file"


def build(entity_type: str, entity_uuid: sys_uuid.UUID | str) -> str:
    return f"urn:{entity_type}:{sys_uuid.UUID(str(entity_uuid))}"


def parse(value: str, expected_type: str | None = None) -> tuple[str, sys_uuid.UUID]:
    prefix, separator, remainder = value.partition(":")
    entity_type, separator2, raw_uuid = remainder.partition(":")
    if prefix != "urn" or not separator or not separator2:
        raise ValueError("Invalid Workspace URN")
    if expected_type is not None and entity_type != expected_type:
        raise ValueError("Unexpected Workspace URN type")
    return entity_type, sys_uuid.UUID(raw_uuid)
