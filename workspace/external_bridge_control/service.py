# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import dataclasses
import json
import re
import urllib.parse
import uuid as sys_uuid
from collections.abc import Callable
from typing import Any, cast

from workspace.external_bridge_control import files
from workspace.external_bridge_control import pki
from workspace.external_bridge_control import provider_service
from workspace.external_bridge_control import state


MAX_JSON_BODY = 1024 * 1024


@dataclasses.dataclass(frozen=True)
class Response:
    status: int
    body: bytes
    content_type: str
    headers: dict[str, str]

    @classmethod
    def json(
        cls,
        status: int,
        payload: object,
        headers: dict[str, str] | None = None,
    ) -> "Response":
        return cls(
            status=status,
            body=json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8"),
            content_type=(
                "application/problem+json" if status >= 400 else "application/json"
            ),
            headers={"Cache-Control": "no-store", **(headers or {})},
        )


class PrivateBridgeService:
    """Transport-neutral implementation of the private control and file APIs."""

    def __init__(
        self,
        control_pki: pki.PersistentControlPki,
        control_state: Any,
        file_manager: files.ExternalFileTransferManager,
        enrollment_persist: Callable[[pki.BridgeIdentity, dict[str, str]], None]
        | None = None,
        provider_data_service: provider_service.ProviderDataService | None = None,
    ) -> None:
        self.control_pki = control_pki
        self.control_state = control_state
        self.file_manager = file_manager
        self.enrollment_persist = enrollment_persist or (lambda *_: None)
        self.provider_data_service = provider_data_service

    def handle(
        self,
        method: str,
        target: str,
        headers: dict[str, str],
        body: bytes,
        certificate_der: bytes,
        request_session: Any = None,
    ) -> Response:
        request_uuid = str(sys_uuid.uuid4())
        try:
            parsed = urllib.parse.urlsplit(target)
            query = (
                urllib.parse.parse_qs(
                    parsed.query, keep_blank_values=True, strict_parsing=True
                )
                if parsed.query
                else {}
            )
            is_object_upload = parsed.path.startswith("/v1/file-objects/")
            payload = (
                self._json_body(body)
                if method in {"POST", "PUT"} and not is_object_upload
                else None
            )
            if method == "POST" and parsed.path == "/v1/enrollments":
                token = headers.get("X-Workspace-Enrollment-Token")
                if token is None:
                    return self._problem(401, "enrollment_token_required", request_uuid)
                issuance = self.control_pki.enroll(
                    token,
                    cast(dict[str, Any], payload),
                    before_commit=self.enrollment_persist,
                )
                return Response.json(201, issuance)

            identity = self.control_pki.authenticate_certificate(certificate_der)
            self.control_state.authorize_identity(identity)
            return self._handle_authenticated(
                method,
                parsed.path,
                query,
                headers,
                payload,
                body,
                identity,
                request_uuid,
                request_session,
            )
        except pki.IdentityError:
            return self._problem(401, "bridge_identity_invalid", request_uuid)
        except pki.EnrollmentAuthenticationError as error:
            return self._problem(
                401, "enrollment_token_invalid", request_uuid, str(error)
            )
        except pki.EnrollmentValidationError as error:
            return self._problem(
                422, "enrollment_request_invalid", request_uuid, str(error)
            )
        except pki.EnrollmentError as error:
            return self._problem(409, "enrollment_conflict", request_uuid, str(error))
        except state.CursorExpiredError as error:
            return Response.json(
                410,
                {
                    "type": "ControlCursorExpiredError",
                    "status": 410,
                    "error": "control_cursor_pruned",
                    "message": "The desired-state cursor requires a full snapshot",
                    "request_uuid": request_uuid,
                    "reason": error.reason,
                    "snapshot_generation": error.snapshot_generation,
                },
            )
        except state.SnapshotExpiredError as error:
            return Response.json(
                410,
                {
                    "type": "ControlSnapshotExpiredError",
                    "status": 410,
                    "error": "control_snapshot_expired",
                    "message": "The desired-state snapshot cannot be continued",
                    "request_uuid": request_uuid,
                    "reason": error.reason,
                },
            )
        except state.StateConflictError as error:
            return self._problem(409, "state_conflict", request_uuid, str(error))
        except state.BridgeForbiddenError as error:
            return self._problem(
                403, "bridge_identity_forbidden", request_uuid, str(error)
            )
        except files.FileTransferError as error:
            return Response.json(error.status, error.as_dict())
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return self._problem(400, "invalid_request", request_uuid)

    def _handle_authenticated(
        self,
        method: str,
        path: str,
        query: dict[str, list[str]],
        headers: dict[str, str],
        payload: Any,
        raw_body: bytes,
        identity: pki.BridgeIdentity,
        request_uuid: str,
        request_session: Any,
    ) -> Response:
        if provider_service.ProviderDataService.matches(path):
            if self.provider_data_service is None or request_session is None:
                raise provider_service.ProviderIngressUnavailableError(
                    "Provider API request transaction is not configured"
                )
            response = self.provider_data_service.handle(
                request_session,
                identity,
                method,
                path,
                query,
                payload,
            )
            if response is None:
                return self._problem(404, "resource_not_found", request_uuid)
            return Response.json(200, response)
        if method == "POST" and path == "/v1/certificate-renewals":
            return Response.json(200, self.control_pki.renew(identity, payload))
        if method == "GET" and path == "/v1/desired-state/changes":
            cursor = self._single(query, "cursor")
            resource_types = self._resource_types(query)
            limit = int(self._single(query, "page_limit", "200"))
            return Response.json(
                200,
                self.control_state.changes(
                    identity, cursor, resource_types, limit=limit
                ),
            )
        if method == "POST" and path == "/v1/desired-state/snapshots":
            snapshot, created = self.control_state.create_snapshot(
                identity,
                payload["request_uuid"],
                payload.get("resource_types"),
            )
            return Response.json(201 if created else 200, snapshot)
        match = re.fullmatch(
            r"/v1/desired-state/snapshots/([A-Za-z0-9_-]+)/pages", path
        )
        if method == "GET" and match is not None:
            return Response.json(
                200,
                self.control_state.snapshot_page(
                    identity,
                    match.group(1),
                    page_cursor=self._single(query, "page_cursor", None),
                    limit=int(self._single(query, "page_limit", "200")),
                ),
            )
        if method == "PUT" and path == "/v1/bridge-instances/self/heartbeat":
            response = self.control_state.heartbeat(identity, payload)
            response["ca_migration"] = self.control_pki.ca_migration(identity)
            return Response.json(200, response)
        if method == "POST" and path == "/v1/observed-state/reports":
            return Response.json(
                200,
                self.control_state.observed_reports(identity, payload["reports"]),
            )
        match = re.fullmatch(r"/v1/file-transfers/incoming/([0-9a-f-]{36})", path)
        if method == "PUT" and match is not None:
            response, created = self.file_manager.allocate_incoming(
                identity, match.group(1), payload
            )
            return Response.json(201 if created else 200, response)
        match = re.fullmatch(
            r"/v1/file-transfers/incoming/([0-9a-f-]{36})/actions/finalize",
            path,
        )
        if method == "POST" and match is not None:
            return Response.json(
                200,
                self.file_manager.finalize_incoming(identity, match.group(1), payload),
            )
        match = re.fullmatch(r"/v1/file-transfers/outgoing/([0-9a-f-]{36})", path)
        if method == "PUT" and match is not None:
            return Response.json(
                200,
                self.file_manager.authorize_outgoing(identity, match.group(1), payload),
            )
        match = re.fullmatch(r"/v1/file-objects/([0-9a-f-]{36})", path)
        if match is not None and method == "PUT":
            token = self._single(query, "token")
            self.file_manager.put_presigned_object(
                identity,
                token,
                {
                    "Content-Type": headers.get("Content-Type"),
                    "Content-Length": headers.get("Content-Length"),
                    "x-amz-checksum-sha256": headers.get("x-amz-checksum-sha256"),
                },
                raw_body,
            )
            return Response(status=204, body=b"", content_type="", headers={})
        if match is not None and method == "GET":
            token = self._single(query, "token")
            data = self.file_manager.get_presigned_object(identity, token)
            return Response(
                status=200,
                body=data,
                content_type="application/octet-stream",
                headers={"Cache-Control": "no-store"},
            )
        return self._problem(404, "resource_not_found", request_uuid)

    @staticmethod
    def _json_body(body: bytes) -> Any:
        if len(body) > MAX_JSON_BODY:
            raise ValueError("Request body is too large")
        return json.loads(body)

    @staticmethod
    def _single(
        query: dict[str, list[str]],
        name: str,
        default: Any = ...,
    ) -> Any:
        values = query.get(name)
        if values is None:
            if default is ...:
                raise ValueError(f"Missing query parameter: {name}")
            return default
        if len(values) != 1:
            raise ValueError(f"Repeated query parameter: {name}")
        return values[0]

    @classmethod
    def _resource_types(
        cls,
        query: dict[str, list[str]],
    ) -> list[str] | None:
        raw = cls._single(query, "resource_types", None)
        if raw is None:
            return None
        values = raw.split(",")
        if len(values) != len(set(values)):
            raise ValueError("Repeated resource type")
        return values

    @staticmethod
    def _problem(
        status: int,
        error: str,
        request_uuid: str,
        message: str | None = None,
    ) -> Response:
        return Response.json(
            status,
            {
                "type": "ControlProblem",
                "status": status,
                "error": error,
                "message": message or "The private control request was rejected",
                "request_uuid": request_uuid,
            },
        )
