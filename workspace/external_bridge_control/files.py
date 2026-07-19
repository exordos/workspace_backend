# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import base64
import datetime
import hashlib
import hmac
import json
import re
import typing
import unicodedata
import uuid as sys_uuid
from collections.abc import Callable
from typing import Any

from botocore import exceptions as botocore_exceptions

from workspace.common import file_storage_opts
from workspace.messenger_api import file_storage


URL_LIFETIME = datetime.timedelta(minutes=5)
ALLOCATION_LIFETIME = datetime.timedelta(minutes=15)
MAX_FILE_SIZE = 50 * 1024 * 1024
_CONTENT_TYPE_RE = re.compile(r"^[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+$")
_URN_RE = re.compile(
    r"^urn:(file|image|video):([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})(?:\?.*)?$"
)


class FileTransferError(RuntimeError):
    def __init__(
        self,
        error: str,
        message: str,
        status: int = 409,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.error = error
        self.message = message
        self.status = status
        self.retryable = retryable

    def as_dict(self) -> dict[str, str | int | bool]:
        return {
            "type": "BridgeFileApiError",
            "code": self.status,
            "error": self.error,
            "message": self.message,
            "retryable": self.retryable,
        }


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _timestamp(value: datetime.datetime) -> str:
    return value.astimezone(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))


def _urn_kind(content_type: str) -> str:
    if content_type.startswith("image/"):
        return "image"
    if content_type.startswith("video/"):
        return "video"
    return "file"


def _canonical_request(request: object) -> str:
    return hashlib.sha256(
        json.dumps(request, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).hexdigest()


class ExternalFileTransferManager:
    """Single-object bridge transfer grants over Workspace local or S3 storage."""

    def __init__(
        self,
        control_state: Any,
        public_base_url: str,
        signing_key: bytes,
        resolve_workspace_file: Callable[[sys_uuid.UUID], dict[str, Any] | None]
        | None = None,
        commit_file_projection: Callable[
            [dict[str, Any], file_storage.WorkspaceFileStorageInfo], None
        ]
        | None = None,
    ) -> None:
        self.control_state = control_state
        self.public_base_url = public_base_url.rstrip("/")
        self.signing_key = signing_key
        self.resolve_workspace_file = (
            resolve_workspace_file or self._resolve_workspace_file_sidecar
        )
        self.commit_file_projection = commit_file_projection or (lambda *_: None)

    def allocate_incoming(
        self,
        identity: Any,
        file_uuid: sys_uuid.UUID | str,
        request: dict[str, Any],
        now: datetime.datetime | None = None,
    ) -> tuple[dict[str, Any], bool]:
        now = now or _utcnow()
        file_uuid = sys_uuid.UUID(str(file_uuid))
        self._validate_request(request)
        assignment = self._authorize_assignment(identity, request)
        key = f"incoming:{file_uuid}"
        canonical = _canonical_request(request)
        existing = self.control_state.file_transfer_get(key)
        if existing is not None:
            if existing["bridge_instance_uuid"] != str(identity.bridge_instance_uuid):
                raise FileTransferError(
                    "bridge_identity_invalid",
                    "File allocation belongs to another bridge",
                    403,
                )
            if existing["canonical_request"] != canonical:
                raise FileTransferError(
                    "file_uuid_conflict",
                    "File UUID was reused with different metadata",
                )
            if existing["status"] == "finalized":
                return existing["response"], False
            generation = existing["allocation_generation"] + 1
            self._delete_object(file_uuid, existing["object_id"])
        else:
            generation = 1
        object_id = f"external-pending/{file_uuid}/{generation}"
        expires_at = now + URL_LIFETIME
        response = {
            "file_uuid": str(file_uuid),
            "operation_uuid": request["operation_uuid"],
            "status": "pending",
            "allocation_generation": generation,
            "upload": self._presigned_put(
                identity,
                file_uuid,
                generation,
                request,
                object_id,
                expires_at,
            ),
        }
        self.control_state.file_transfer_put(
            key,
            {
                "canonical_request": canonical,
                "bridge_instance_uuid": str(identity.bridge_instance_uuid),
                "request": request,
                "status": "pending",
                "phase": "pending",
                "allocation_generation": generation,
                "object_id": object_id,
                "expires_at": _timestamp(now + ALLOCATION_LIFETIME),
                "assignment": assignment,
                "response": response,
            },
        )
        return response, existing is None

    def finalize_incoming(
        self,
        identity: Any,
        file_uuid: sys_uuid.UUID | str,
        request: dict[str, Any],
        now: datetime.datetime | None = None,
    ) -> dict[str, Any]:
        now = now or _utcnow()
        file_uuid = sys_uuid.UUID(str(file_uuid))
        key = f"incoming:{file_uuid}"
        transfer = self.control_state.file_transfer_get(key)
        if transfer is None:
            raise FileTransferError(
                "resource_not_found", "Allocation was not found", 404
            )
        if request["operation_uuid"] != transfer["request"]["operation_uuid"]:
            raise FileTransferError(
                "idempotency_conflict", "Operation UUID does not match allocation"
            )
        if transfer["status"] == "finalized":
            expected = transfer["request"]
            if all(
                request[name] == expected[name]
                for name in ("size_bytes", "content_type", "sha256")
            ):
                return transfer["response"]
            raise FileTransferError(
                "idempotency_conflict", "Finalize replay does not match finalized file"
            )
        if transfer["status"] == "invalidated":
            raise FileTransferError(
                "allocation_not_pending", "Allocation generation was invalidated"
            )
        if request["allocation_generation"] != transfer["allocation_generation"]:
            raise FileTransferError(
                "allocation_generation_mismatch",
                "Allocation generation does not match",
            )
        if _parse_timestamp(transfer["expires_at"]) <= now:
            raise FileTransferError("allocation_expired", "Allocation has expired", 410)
        expected = transfer["request"]
        if any(
            request[name] != expected[name]
            for name in ("size_bytes", "content_type", "sha256")
        ):
            self._invalidate_transfer(key, file_uuid, transfer)
            raise FileTransferError(
                "file_integrity_mismatch",
                "Finalized metadata does not match allocation",
                422,
            )
        assignment = self._authorize_assignment(identity, expected)
        if assignment != transfer["assignment"]:
            raise FileTransferError(
                "assignment_changed", "Chat assignment changed before finalization"
            )
        chat = assignment["chat"]
        account = assignment["account"]
        stream_uuid = chat.get("projection_stream_uuid")
        if stream_uuid is None:
            raise FileTransferError(
                "operation_state_conflict",
                "Chat projection is not ready for file finalization",
            )
        if transfer["phase"] == "pending":
            data = self._read_object(file_uuid, transfer["object_id"])
            digest = hashlib.sha256(data).hexdigest()
            if len(data) != request["size_bytes"] or digest != request["sha256"]:
                self._invalidate_transfer(key, file_uuid, transfer)
                raise FileTransferError(
                    "file_integrity_mismatch",
                    "Uploaded object integrity check failed",
                    422,
                )
            transfer["phase"] = "verified"
            self.control_state.file_transfer_put(key, transfer)
        if transfer["phase"] == "verified":
            data = self._read_object(file_uuid, transfer["object_id"])
            storage_info = file_storage.save_workspace_file(file_uuid, data)
            transfer["storage_info"] = {
                "storage_type": storage_info.storage_type,
                "storage_id": storage_info.storage_id,
                "storage_object_id": storage_info.storage_object_id,
            }
            transfer["phase"] = "final_object_saved"
            self.control_state.file_transfer_put(key, transfer)
        storage_info = file_storage.WorkspaceFileStorageInfo(**transfer["storage_info"])
        if "sidecar" not in transfer:
            created_at = _timestamp(now)
            transfer["sidecar"] = {
                "schema_version": 2,
                "uuid": str(file_uuid),
                "project_id": chat["project_id"],
                "stream_uuid": stream_uuid,
                "owner_uuid": account["owner_user_uuid"],
                "name": expected["name"],
                "description": "",
                "content_type": expected["content_type"],
                "size_bytes": expected["size_bytes"],
                "sha256": expected["sha256"],
                "created_at": created_at,
                "acl": {"mode": "stream_members", "stream_uuid": stream_uuid},
                "origin": {
                    "kind": "external_provider",
                    "provider_kind": identity.provider_kind,
                    "external_account_uuid": expected["external_account_uuid"],
                    "external_chat_uuid": expected["external_chat_uuid"],
                    "operation_uuid": expected["operation_uuid"],
                },
            }
            self.control_state.file_transfer_put(key, transfer)
        sidecar = transfer["sidecar"]
        if transfer["phase"] == "final_object_saved":
            self._save_sidecar(file_uuid, sidecar)
            transfer["phase"] = "sidecar_saved"
            self.control_state.file_transfer_put(key, transfer)
        if transfer["phase"] == "sidecar_saved":
            self.commit_file_projection(sidecar, storage_info)
            transfer["phase"] = "projection_committed"
            self.control_state.file_transfer_put(key, transfer)
        if transfer["phase"] == "projection_committed":
            self._delete_object(file_uuid, transfer["object_id"])
            transfer["phase"] = "pending_deleted"
            self.control_state.file_transfer_put(key, transfer)
        file_urn = f"urn:{_urn_kind(expected['content_type'])}:{file_uuid}"
        response = {
            "file_uuid": str(file_uuid),
            "operation_uuid": expected["operation_uuid"],
            "status": "finalized",
            "allocation_generation": transfer["allocation_generation"],
            "file_urn": file_urn,
            "name": expected["name"],
            "size_bytes": expected["size_bytes"],
            "content_type": expected["content_type"],
            "sha256": expected["sha256"],
            "created_at": sidecar["created_at"],
        }
        transfer["status"] = "finalized"
        transfer["phase"] = "finalized"
        transfer["response"] = response
        self.control_state.file_transfer_put(key, transfer)
        return response

    def _invalidate_transfer(
        self,
        key: str,
        file_uuid: sys_uuid.UUID,
        transfer: dict[str, Any],
    ) -> None:
        transfer["status"] = "invalidated"
        transfer["phase"] = "invalidating"
        transfer.pop("response", None)
        self.control_state.file_transfer_put(key, transfer)
        self._delete_object(file_uuid, transfer["object_id"])
        transfer["phase"] = "invalidated"
        self.control_state.file_transfer_put(key, transfer)

    def authorize_outgoing(
        self,
        identity: Any,
        transfer_uuid: sys_uuid.UUID | str,
        request: dict[str, Any],
        now: datetime.datetime | None = None,
    ) -> dict[str, Any]:
        now = now or _utcnow()
        transfer_uuid = sys_uuid.UUID(str(transfer_uuid))
        assignment = self._authorize_assignment(identity, request)
        match = _URN_RE.fullmatch(request["file_urn"])
        if match is None:
            raise FileTransferError(
                "invalid_workspace_urn", "Workspace file URN is invalid", 422
            )
        kind, file_uuid_text = match.groups()
        file_uuid = sys_uuid.UUID(file_uuid_text)
        metadata = self.resolve_workspace_file(file_uuid)
        if metadata is None:
            raise FileTransferError(
                "resource_not_found", "Workspace file was not found", 404
            )
        if kind != _urn_kind(metadata["content_type"]):
            raise FileTransferError(
                "urn_kind_mismatch", "Workspace URN kind does not match file", 422
            )
        projection_stream_uuid = assignment["chat"].get("projection_stream_uuid")
        acl = metadata.get("acl")
        if (
            projection_stream_uuid is None
            or metadata.get("project_id") != assignment["chat"]["project_id"]
            or metadata.get("stream_uuid") != projection_stream_uuid
            or assignment["account"].get("owner_user_uuid")
            not in metadata.get("authorized_user_uuids", [])
            or not isinstance(acl, dict)
            or acl.get("mode") != "stream_members"
            or acl.get("stream_uuid") != projection_stream_uuid
        ):
            raise FileTransferError(
                "file_access_denied", "Workspace file access is denied", 403
            )
        key = f"outgoing:{transfer_uuid}"
        canonical = _canonical_request(request)
        existing = self.control_state.file_transfer_get(key)
        if existing is not None:
            if existing["bridge_instance_uuid"] != str(identity.bridge_instance_uuid):
                raise FileTransferError(
                    "bridge_identity_invalid",
                    "File transfer belongs to another bridge",
                    403,
                )
            if existing["canonical_request"] != canonical:
                raise FileTransferError(
                    "idempotency_conflict",
                    "Transfer UUID was reused with different input",
                )
        generation = 1 if existing is None else existing["authorization_generation"]
        download = self._presigned_get(
            identity,
            transfer_uuid,
            file_uuid,
            metadata,
            now + URL_LIFETIME,
        )
        response = {
            "transfer_uuid": str(transfer_uuid),
            "operation_uuid": request["operation_uuid"],
            "status": "ready",
            "authorization_generation": generation,
            "file_uuid": str(file_uuid),
            "file_urn": request["file_urn"],
            "name": metadata["name"],
            "size_bytes": metadata["size_bytes"],
            "content_type": metadata["content_type"],
            "sha256": metadata["sha256"],
            "download": download,
        }
        self.control_state.file_transfer_put(
            key,
            {
                "canonical_request": canonical,
                "bridge_instance_uuid": str(identity.bridge_instance_uuid),
                "authorization_generation": generation,
                "response": response,
            },
        )
        return response

    def put_presigned_object(
        self,
        identity: Any,
        token: str,
        headers: dict[str, str | None],
        data: bytes,
        now: datetime.datetime | None = None,
    ) -> None:
        payload = self._verify_token(identity, token, "PUT", now or _utcnow())
        expected = payload["headers"]
        if any(headers.get(name) != value for name, value in expected.items()):
            raise FileTransferError(
                "invalid_request", "Signed upload headers do not match", 400
            )
        if len(data) != int(expected["Content-Length"]):
            raise FileTransferError(
                "file_integrity_mismatch", "Upload length does not match", 422
            )
        file_storage.save_workspace_file(
            sys_uuid.UUID(payload["file_uuid"]),
            data,
            storage_object_id=payload["object_id"],
        )

    def get_presigned_object(
        self,
        identity: Any,
        token: str,
        now: datetime.datetime | None = None,
    ) -> bytes:
        payload = self._verify_token(identity, token, "GET", now or _utcnow())
        return self._read_object(
            sys_uuid.UUID(payload["file_uuid"]), payload["object_id"]
        )

    def _authorize_assignment(
        self, identity: Any, request: dict[str, Any]
    ) -> dict[str, Any]:
        assignment = self.control_state.assignment(
            identity,
            request["external_account_uuid"],
            request["external_chat_uuid"],
        )
        if assignment is None:
            raise FileTransferError(
                "account_not_assigned",
                "External account and chat are not assigned",
                403,
            )
        return assignment

    @staticmethod
    def _validate_request(request: dict[str, Any]) -> None:
        name = request["name"]
        if (
            unicodedata.normalize("NFC", name) != name
            or not name
            or len(name.encode("utf-8")) > 255
            or any(ord(character) < 32 or character in "/\\" for character in name)
        ):
            raise FileTransferError("invalid_request", "File name is invalid", 400)
        size = request["size_bytes"]
        if not 0 <= size <= MAX_FILE_SIZE:
            raise FileTransferError(
                "file_too_large", "File exceeds the allowed size", 413
            )
        content_type = request["content_type"]
        if _CONTENT_TYPE_RE.fullmatch(content_type) is None:
            raise FileTransferError(
                "unsupported_content_type", "Content type is invalid", 415
            )
        if re.fullmatch(r"[0-9a-f]{64}", request["sha256"]) is None:
            raise FileTransferError("invalid_request", "File digest is invalid", 400)
        sys_uuid.UUID(request["operation_uuid"])
        sys_uuid.UUID(request["external_account_uuid"])
        sys_uuid.UUID(request["external_chat_uuid"])

    def _presigned_put(
        self,
        identity: Any,
        file_uuid: sys_uuid.UUID,
        generation: int,
        request: dict[str, Any],
        object_id: str,
        expires_at: datetime.datetime,
    ) -> dict[str, Any]:
        checksum = base64.b64encode(bytes.fromhex(request["sha256"])).decode("ascii")
        headers = {
            "Content-Type": request["content_type"],
            "Content-Length": str(request["size_bytes"]),
            "x-amz-checksum-sha256": checksum,
        }
        storage = file_storage.get_workspace_file_storage()
        if storage.storage_type == file_storage_opts.STORAGE_TYPE_S3:
            s3_storage = typing.cast(file_storage.S3WorkspaceFileStorage, storage)
            url = s3_storage.client.generate_presigned_url(
                "put_object",
                Params={
                    "Bucket": s3_storage.bucket_name,
                    "Key": object_id,
                    "ContentType": request["content_type"],
                    "ChecksumSHA256": checksum,
                },
                ExpiresIn=300,
                HttpMethod="PUT",
            )
        else:
            token = self._sign_token(
                identity,
                {
                    "method": "PUT",
                    "file_uuid": str(file_uuid),
                    "generation": generation,
                    "object_id": object_id,
                    "headers": headers,
                    "expires_at": _timestamp(expires_at),
                },
            )
            url = f"{self.public_base_url}/v1/file-objects/{file_uuid}?token={token}"
        return {
            "method": "PUT",
            "url": url,
            "headers": headers,
            "expires_at": _timestamp(expires_at),
            "expires_in_seconds": 300,
        }

    def _presigned_get(
        self,
        identity: Any,
        transfer_uuid: sys_uuid.UUID,
        file_uuid: sys_uuid.UUID,
        metadata: dict[str, Any],
        expires_at: datetime.datetime,
    ) -> dict[str, Any]:
        object_id = metadata.get(
            "storage_object_id"
        ) or file_storage.get_workspace_file_object_id(file_uuid)
        storage = file_storage.get_workspace_file_storage(
            storage_type=metadata.get("storage_type")
        )
        if storage.storage_type == file_storage_opts.STORAGE_TYPE_S3:
            s3_storage = typing.cast(file_storage.S3WorkspaceFileStorage, storage)
            url = s3_storage.client.generate_presigned_url(
                "get_object",
                Params={"Bucket": s3_storage.bucket_name, "Key": object_id},
                ExpiresIn=300,
                HttpMethod="GET",
            )
        else:
            token = self._sign_token(
                identity,
                {
                    "method": "GET",
                    "transfer_uuid": str(transfer_uuid),
                    "file_uuid": str(file_uuid),
                    "object_id": object_id,
                    "headers": {},
                    "expires_at": _timestamp(expires_at),
                },
            )
            url = f"{self.public_base_url}/v1/file-objects/{file_uuid}?token={token}"
        return {
            "method": "GET",
            "url": url,
            "headers": {},
            "expires_at": _timestamp(expires_at),
            "expires_in_seconds": 300,
        }

    def _sign_token(self, identity: Any, payload: dict[str, Any]) -> str:
        scoped = {
            **payload,
            "bridge_instance_uuid": str(identity.bridge_instance_uuid),
            "identity_generation": identity.identity_generation,
        }
        content = json.dumps(scoped, separators=(",", ":"), sort_keys=True).encode(
            "utf-8"
        )
        signature = hmac.new(self.signing_key, content, hashlib.sha256).digest()
        return (
            base64.urlsafe_b64encode(content + signature).rstrip(b"=").decode("ascii")
        )

    def _verify_token(
        self,
        identity: Any,
        token: str,
        method: str,
        now: datetime.datetime,
    ) -> dict[str, Any]:
        try:
            decoded = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
            content, signature = decoded[:-32], decoded[-32:]
            expected = hmac.new(self.signing_key, content, hashlib.sha256).digest()
            if not hmac.compare_digest(signature, expected):
                raise ValueError
            payload = json.loads(content)
        except (ValueError, json.JSONDecodeError) as error:
            raise FileTransferError(
                "invalid_request", "Signed URL is invalid", 400
            ) from error
        if (
            payload["method"] != method
            or payload["bridge_instance_uuid"] != str(identity.bridge_instance_uuid)
            or payload["identity_generation"] != identity.identity_generation
        ):
            raise FileTransferError(
                "bridge_identity_invalid", "Signed URL scope is invalid", 403
            )
        if _parse_timestamp(payload["expires_at"]) <= now:
            raise FileTransferError("allocation_expired", "Signed URL has expired", 410)
        return payload

    @staticmethod
    def _read_object(file_uuid: sys_uuid.UUID, object_id: str) -> bytes:
        try:
            return file_storage.read_workspace_file(
                file_uuid, storage_object_id=object_id
            )
        except FileNotFoundError as error:
            raise FileTransferError(
                "upload_missing", "Uploaded object is missing", 409
            ) from error

    @staticmethod
    def _delete_object(file_uuid: sys_uuid.UUID, object_id: str) -> None:
        file_storage.delete_workspace_file(file_uuid, storage_object_id=object_id)

    @staticmethod
    def _save_sidecar(file_uuid: sys_uuid.UUID, sidecar: dict[str, Any]) -> None:
        metadata = file_storage.WorkspaceFileMetadata(
            uuid=sys_uuid.UUID(sidecar["uuid"]),
            project_id=sys_uuid.UUID(sidecar["project_id"]),
            stream_uuid=sys_uuid.UUID(sidecar["stream_uuid"]),
            owner_uuid=sys_uuid.UUID(sidecar["owner_uuid"]),
            name=sidecar["name"],
            description=sidecar["description"],
            content_type=sidecar["content_type"],
            size_bytes=sidecar["size_bytes"],
            sha256=sidecar["sha256"],
            created_at=_parse_timestamp(sidecar["created_at"]),
            acl_mode=sidecar["acl"]["mode"],
            origin=sidecar["origin"],
        )
        file_storage.save_workspace_file_metadata(metadata)

    @staticmethod
    def _resolve_workspace_file_sidecar(
        file_uuid: sys_uuid.UUID,
    ) -> dict[str, Any] | None:
        storage = file_storage.get_workspace_file_storage()
        object_id = file_storage.get_workspace_file_metadata_object_id(file_uuid)
        try:
            if storage.storage_type == file_storage_opts.STORAGE_TYPE_FILE:
                content = file_storage._get_local_file_path(object_id).read_bytes()
            else:
                s3_storage = typing.cast(file_storage.S3WorkspaceFileStorage, storage)
                response = s3_storage.client.get_object(
                    Bucket=s3_storage.bucket_name, Key=object_id
                )
                content = response["Body"].read()
        except FileNotFoundError:
            return None
        except botocore_exceptions.ClientError as error:
            if error.response["Error"]["Code"] in {"NoSuchKey", "404"}:
                return None
            raise
        metadata = json.loads(content)
        metadata["storage_type"] = storage.storage_type
        metadata["storage_object_id"] = file_storage.get_workspace_file_object_id(
            file_uuid
        )
        return metadata
