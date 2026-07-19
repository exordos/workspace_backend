# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import contextlib
import uuid as sys_uuid
from typing import Any

from restalchemy.common import contexts
from restalchemy.dm import filters as dm_filters

from workspace.messenger_api import file_storage
from workspace.messenger_api.dm import external_models
from workspace.messenger_api.dm import helpers
from workspace.messenger_api.dm import models


class CanonicalFileRepository:
    """Canonical SQL projection and current-access resolver for bridge files."""

    @staticmethod
    def _current_session() -> contextlib.AbstractContextManager[Any]:
        return contextlib.nullcontext(contexts.Context().get_session())

    def commit_projection(
        self,
        sidecar: dict[str, Any],
        storage_info: file_storage.WorkspaceFileStorageInfo,
    ) -> Any:
        file_uuid = sys_uuid.UUID(sidecar["uuid"])
        project_id = sys_uuid.UUID(sidecar["project_id"])
        stream_uuid = sys_uuid.UUID(sidecar["stream_uuid"])
        owner_uuid = sys_uuid.UUID(sidecar["owner_uuid"])
        origin = sidecar["origin"]
        account_uuid = sys_uuid.UUID(origin["external_account_uuid"])
        chat_uuid = sys_uuid.UUID(origin["external_chat_uuid"])
        with self._current_session() as session:
            account = external_models.ExternalAccount.objects.get_one_or_none(
                filters={
                    "uuid": dm_filters.EQ(account_uuid),
                    "owner_user_uuid": dm_filters.EQ(owner_uuid),
                    "provider": dm_filters.EQ(origin["provider_kind"]),
                },
                session=session,
            )
            chat = external_models.ExternalChat.objects.get_one_or_none(
                filters={
                    "uuid": dm_filters.EQ(chat_uuid),
                    "external_account_uuid": dm_filters.EQ(account_uuid),
                    "project_id": dm_filters.EQ(project_id),
                    "projection_stream_uuid": dm_filters.EQ(stream_uuid),
                    "selected": dm_filters.EQ(True),
                },
                session=session,
            )
            if account is None or chat is None:
                raise ValueError("External file assignment is no longer canonical")

            existing = models.WorkspaceFile.objects.get_one_or_none(
                filters={"uuid": dm_filters.EQ(file_uuid)},
                session=session,
            )
            expected = {
                "project_id": project_id,
                "user_uuid": owner_uuid,
                "stream_uuid": stream_uuid,
                "external_account_uuid": account_uuid,
                "name": sidecar["name"],
                "description": sidecar["description"],
                "content_type": sidecar["content_type"],
                "size_bytes": sidecar["size_bytes"],
                "hash": sidecar["sha256"],
                "storage_type": storage_info.storage_type,
                "storage_id": storage_info.storage_id,
                "storage_object_id": storage_info.storage_object_id,
            }
            if existing is not None:
                if any(
                    getattr(existing, name) != value for name, value in expected.items()
                ):
                    raise ValueError(
                        "Canonical file UUID conflicts with finalized transfer"
                    )
                return existing
            return helpers.create_workspace_file(
                uuid=file_uuid,
                session=session,
                **expected,
            )

    def resolve(self, file_uuid: sys_uuid.UUID | str) -> dict[str, Any] | None:
        file_uuid = sys_uuid.UUID(str(file_uuid))
        with self._current_session() as session:
            canonical = models.WorkspaceFile.objects.get_one_or_none(
                filters={"uuid": dm_filters.EQ(file_uuid)},
                session=session,
            )
            if canonical is None:
                return None
            metadata = file_storage.read_workspace_file_metadata(
                file_uuid,
                storage_type=canonical.storage_type,
            )
            if (
                metadata.uuid != canonical.uuid
                or metadata.project_id != canonical.project_id
                or metadata.stream_uuid != canonical.stream_uuid
                or metadata.owner_uuid != canonical.user_uuid
                or metadata.name != canonical.name
                or metadata.description != canonical.description
                or metadata.content_type != canonical.content_type
                or metadata.size_bytes != canonical.size_bytes
                or metadata.sha256 != canonical.hash
            ):
                raise ValueError("Canonical file record and sidecar do not match")
            accesses = models.WorkspaceFileAccess.objects.get_all(
                filters={
                    "project_id": dm_filters.EQ(canonical.project_id),
                    "file_uuid": dm_filters.EQ(file_uuid),
                },
                session=session,
            )
            acl = {"mode": metadata.acl_mode}
            if metadata.stream_uuid is not None:
                acl["stream_uuid"] = str(metadata.stream_uuid)
            return {
                "uuid": str(canonical.uuid),
                "project_id": str(canonical.project_id),
                "stream_uuid": (
                    None
                    if canonical.stream_uuid is None
                    else str(canonical.stream_uuid)
                ),
                "owner_uuid": str(canonical.user_uuid),
                "name": canonical.name,
                "description": canonical.description,
                "content_type": canonical.content_type,
                "size_bytes": canonical.size_bytes,
                "sha256": canonical.hash,
                "acl": acl,
                "authorized_user_uuids": [str(access.user_uuid) for access in accesses],
                "storage_type": canonical.storage_type,
                "storage_id": canonical.storage_id,
                "storage_object_id": canonical.storage_object_id,
                "origin": metadata.origin,
            }
