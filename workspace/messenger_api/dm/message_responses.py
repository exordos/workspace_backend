# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

"""Message input and access-scoped response models."""

from restalchemy.dm import properties, types

from workspace.messenger_api.dm import base, message_payloads, polls


COUNT = types.Integer(min_value=0)
POLL_STATE = types.AllowNone(
    polls.Fields(
        {
            "revision": types.Integer(min_value=1),
            "is_closed": types.Boolean(),
            "voter_count": COUNT,
            "questions": polls.Array(
                polls.Fields(
                    {
                        "question_uuid": types.UUID(),
                        "voter_count": COUNT,
                        "option_counts": types.TypedDict(COUNT),
                    }
                )
            ),
            "my_votes": polls.Array(
                polls.Fields(
                    {
                        "question_uuid": types.UUID(),
                        "option_uuids": polls.Array(types.UUID()),
                    }
                )
            ),
        }
    )
)


class WorkspaceUserMessageWrite(base.WorkspaceUserMessageBase):
    payload = properties.property(
        message_payloads.MESSAGE_WRITE_PAYLOAD_TYPE, required=True
    )


class WorkspaceUserMessage(base.WorkspaceUserMessageBase):
    poll_state = properties.property(POLL_STATE, default=None, read_only=True)
