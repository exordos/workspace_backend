#    Copyright 2026 Genesis Corporation.
#
#    All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""Private history ingress with explicit short SQL scopes around storage I/O."""

import typing

import dataclasses
import hashlib
import json
import logging
import urllib.parse
import uuid as sys_uuid

from workspace.external_bridge_control import pki
from workspace.external_bridge_control import service
from workspace.external_bridge_control import state
from workspace.history_import import contract
from workspace.history_import import references
from workspace.history_import import repository
from workspace.history_import import writer
from workspace.messenger_api import file_storage
from workspace.messenger_api.dm import models


LOG = logging.getLogger(__name__)


def require_file_limit(session: typing.Any, size: int) -> None:
    policy = session.execute(
        "SELECT limits FROM m_external_provider_policies_v1 WHERE provider='zulip' FOR SHARE",
        (),
    ).fetchone()
    limit = policy["limits"].get("max_file_bytes", 0)
    if (
        type(limit) is not int
        or limit <= 0
        or size > min(limit, contract.MAX_FILE_BYTES)
    ):
        raise contract.ImportError("history_file_too_large", 413)


class HistoryImportService:
    """Orchestrate private HTTP phases, passing each short SQL session to helpers.

    This boundary intentionally has no enclosing request transaction: storage
    must finish before SQL registration, and authority is checked in each phase.
    """

    def __init__(
        self,
        control_pki: typing.Any,
        control_state: typing.Any,
        session_factory: typing.Callable,
    ) -> None:
        self.control_pki = control_pki
        self.control_state = control_state
        self.session_factory = session_factory

    def authorize(self, session: typing.Any, identity: typing.Any) -> None:
        writer.configure_transaction(session)
        self.control_state.authorize_identity(identity)
        if identity.provider_kind != "zulip":
            raise contract.ImportError("history_provider_unsupported", 403)

    def handle(
        self,
        method: str,
        target: str,
        headers: dict,
        body: bytes,
        certificate_der: bytes,
    ) -> service.Response:
        try:
            identity = self.control_pki.authenticate_certificate(certificate_der)
            path = urllib.parse.urlsplit(target)
            if path.query or path.fragment:
                raise contract.ImportError("invalid_history_target", 400)
            with self.session_factory() as session:
                self.authorize(session, identity)
            parts = path.path.removeprefix(contract.PATH).strip("/").split("/")
            if path.path == contract.PATH:
                if method == "GET":
                    return service.Response.json(200, {"schema_version": 1})
                if method == "POST":
                    return self.accept(identity, body)
            job_uuid = sys_uuid.UUID(parts[0])
            if len(parts) == 1 and method == "GET":
                with self.session_factory() as session:
                    self.authorize(session, identity)
                    job = repository.get_job(session, identity, job_uuid)
                    if job["status"] in {"complete", "failed", "superseded"}:
                        job = repository.get_job(session, identity, job_uuid, lock=True)
                    else:
                        repository.guard(session, identity, job)
                    return service.Response.json(
                        200, repository.public_status(session, job)
                    )
            if len(parts) == 5 and parts[1] == "mappings" and method == "GET":
                with self.session_factory() as session:
                    self.authorize(session, identity)
                    job = repository.get_job(session, identity, job_uuid)
                    result = references.page(
                        session,
                        identity,
                        job,
                        sys_uuid.UUID(parts[2]),
                        parts[3],
                        parts[4],
                    )
                    return service.Response.json(200, result)
            if len(parts) in {3, 4} and parts[1] == "files":
                unavailable = (
                    len(parts) == 4 and parts[3] == "unavailable" and method == "POST"
                )
                if unavailable or (len(parts) == 3 and method == "PUT"):
                    return self.upload(
                        identity,
                        job_uuid,
                        sys_uuid.UUID(parts[2]),
                        headers,
                        body,
                        unavailable,
                    )
            raise contract.ImportError("history_endpoint_not_found", 404)
        except contract.ImportError as error:
            return service.Response.json(error.status, {"error": error.code})
        except pki.IdentityError:
            return service.Response.json(401, {"error": "bridge_identity_invalid"})
        except state.BridgeForbiddenError:
            return service.Response.json(403, {"error": "bridge_identity_forbidden"})
        except (ValueError, KeyError, TypeError, OverflowError):
            return service.Response.json(400, {"error": "invalid_history_request"})
        except Exception as error:
            # No request text, storage credentials or provider paths in logs.
            LOG.warning(
                "History ingress unavailable: error_type=%s", type(error).__name__
            )
            return service.Response.json(503, {"error": "history_import_unavailable"})

    def accept(self, identity: typing.Any, body: bytes) -> service.Response:
        if len(body) > contract.MAX_BODY_BYTES:
            raise contract.ImportError("history_body_too_large", 413)
        envelope = json.loads(body)
        batch = contract.Batch.parse(envelope)
        encoded = contract.encode(envelope)
        with self.session_factory() as session:
            self.authorize(session, identity)
            sources = repository.authorize_sources(session, identity, batch)
            repository.validate_observers(batch, sources)
            existing = repository.existing_job(session, identity, batch)
            if existing is not None:
                existing = repository.get_job(
                    session, identity, existing["uuid"], lock=True
                )
                return service.Response.json(
                    200, repository.public_status(session, existing)
                )
            repository.check_capacity(session, identity)
        job_uuid = batch.job_uuid(
            identity.bridge_instance_uuid, identity.identity_generation
        )
        storage = file_storage.save_workspace_file(
            job_uuid,
            encoded,
            storage_object_id=f"history-imports/v1/{identity.bridge_instance_uuid}/{job_uuid}/batch.json",
        )
        with self.session_factory() as session:
            self.authorize(session, identity)
            job = repository.register(session, identity, batch, storage)
            response = repository.public_status(session, job)
        return service.Response.json(202, response)

    def upload(
        self,
        identity: typing.Any,
        job_uuid: sys_uuid.UUID,
        request_uuid: sys_uuid.UUID,
        headers: dict,
        body: bytes,
        unavailable: bool,
    ) -> service.Response:
        if len(body) > contract.MAX_FILE_BYTES:
            raise contract.ImportError("history_file_too_large", 413)
        with self.session_factory() as session:
            self.authorize(session, identity)
            job = repository.get_job(session, identity, job_uuid)
            repository.guard(session, identity, job)
            if not unavailable:
                require_file_limit(session, len(body))
            request = session.execute(
                "SELECT * FROM m_external_history_files_v1 WHERE job_uuid = %s AND uuid = %s",
                (job_uuid, request_uuid),
            ).fetchone()
            if request is None:
                raise contract.ImportError("history_file_not_requested", 404)
            if request["status"] != "missing":
                return service.Response.json(200, {"status": request["status"]})
        name, content_type, sha256, storage = None, None, None, None
        if not unavailable:
            headers = {key.lower(): value for key, value in headers.items()}
            name = contract.text(urllib.parse.unquote(headers["x-file-name"]), 255)
            content_type = contract.text(headers["content-type"], 255)
            if (
                not models.WorkspaceFile.properties.properties["name"]
                .get_property_type()
                .validate(name)
            ):
                raise contract.ImportError("invalid_history_file_name", 400)
            if (
                not models.WorkspaceFile.properties.properties["content_type"]
                .get_property_type()
                .validate(content_type)
            ):
                raise contract.ImportError("invalid_history_content_type", 400)
            sha256 = hashlib.sha256(body).hexdigest()
            stored = file_storage.save_workspace_file(
                request_uuid,
                body,
                storage_object_id=f"external-content/sha256/{sha256[:2]}/{sha256}",
            )
            storage = json.dumps(dataclasses.asdict(stored))
        with self.session_factory() as session:
            self.authorize(session, identity)
            repository.guard(session, identity, job)
            if not unavailable:
                require_file_limit(session, len(body))
            updated_file = session.execute(
                """UPDATE m_external_history_files_v1 SET status = %s, name = %s,
                           content_type = %s, sha256 = %s, size_bytes = %s, storage = %s::jsonb,
                           updated_at = now()
                   WHERE job_uuid = %s AND uuid = %s AND status = 'missing' RETURNING uuid""",
                (
                    "unavailable" if unavailable else "ready",
                    name,
                    content_type,
                    sha256,
                    len(body) if not unavailable else None,
                    storage,
                    job_uuid,
                    request_uuid,
                ),
            ).fetchone()
            if updated_file is not None:
                session.execute(
                    """UPDATE m_external_history_imports_v1 SET consecutive_errors=0
                       WHERE uuid=%s AND %s=ANY(active_file_uuids)
                         AND status IN ('pending', 'running', 'waiting_files')""",
                    (job_uuid, request_uuid),
                )
            session.execute(
                """UPDATE m_external_history_imports_v1 SET status = 'pending', available_at = now(), updated_at = now()
                   WHERE uuid = %s AND status = 'waiting_files' AND files_discovered
                     AND NOT EXISTS (SELECT 1 FROM m_external_history_files_v1
                         WHERE job_uuid = %s AND uuid = ANY(m_external_history_imports_v1.active_file_uuids)
                           AND status = 'missing')""",
                (job_uuid, job_uuid),
            )
        return service.Response.json(
            200, {"status": "unavailable" if unavailable else "ready"}
        )
