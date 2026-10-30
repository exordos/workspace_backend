# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import typing

from gcl_sdk.events.dm import models as event_models
from restalchemy.dm import properties
from restalchemy.dm import types


class WorkspacePushEventPayload(event_models.AbstractEventPayload):
    KIND = "WorkspacePush"

    body = properties.property(types.Dict(), required=True)

    def to_simple_dict(self) -> dict[str, typing.Any]:
        return self.body["event_params"]


class WorkspaceTopicSignal(event_models.AbstractEventPayload):
    """A timestamped request to prepare the current user/topic notification."""

    KIND = "WorkspaceTopicSignal"
    body = properties.property(types.Dict(), required=True)

    def to_simple_dict(self) -> dict[str, typing.Any]:
        return self.body
