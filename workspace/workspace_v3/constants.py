# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Stable Workspace v3 identities shared by stores and projections."""

import uuid as sys_uuid

ALL_CHATS_FOLDER_UUID = sys_uuid.UUID("00000000-0000-0000-0000-000000000000")
DIRECT_FOLDER_UUID = sys_uuid.UUID("00000000-0000-0000-0000-000000000001")
STREAMS_FOLDER_UUID = sys_uuid.UUID("00000000-0000-0000-0000-000000000002")
SYSTEM_FOLDERS = (
    (ALL_CHATS_FOLDER_UUID, "all_chats", "All chats", "00"),
    (DIRECT_FOLDER_UUID, "direct", "Personal", "11"),
    (STREAMS_FOLDER_UUID, "streams", "Channels", "22"),
)
