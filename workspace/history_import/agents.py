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

"""A resumable worker: storage and conversion never borrow a SQL connection."""

import typing

import errno
import json
import logging
import time
import types
import uuid as sys_uuid

from botocore import exceptions as botocore_exceptions
from psycopg import errors as pg_errors
from restalchemy.common import exceptions as ra_exceptions

from workspace.history_import import contract
from workspace.history_import import preparation
from workspace.history_import import repository
from workspace.history_import import writer
from workspace.messenger_api import file_storage


LOG = logging.getLogger(__name__)
WRITE_BUDGET_SECONDS = 0.2


class WriteBudgetExceeded(Exception):
    pass


def transient_storage_error(error: BaseException | None) -> bool:
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(
            error,
            (
                TimeoutError,
                ConnectionError,
                botocore_exceptions.ConnectionError,
                botocore_exceptions.HTTPClientError,
                botocore_exceptions.IncompleteReadError,
            ),
        ):
            return True
        if isinstance(error, OSError):
            return error.errno in {
                errno.EAGAIN,
                errno.EINTR,
                errno.ECONNABORTED,
                errno.ECONNREFUSED,
                errno.ECONNRESET,
                errno.EHOSTUNREACH,
                errno.ENETDOWN,
                errno.ENETRESET,
                errno.ENETUNREACH,
                errno.ETIMEDOUT,
                errno.EPIPE,
            }
        if isinstance(error, botocore_exceptions.ClientError):
            response = error.response
            status = response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
            if status in {408, 429} or 500 <= status <= 599:
                return True
            if response.get("Error", {}).get("Code") in {
                "RequestTimeout",
                "SlowDown",
                "ServiceUnavailable",
                "InternalError",
            }:
                return True
        error = error.__cause__ or error.__context__
    return False


def database_error_code(error: BaseException | None) -> str | None:
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if getattr(error, "sqlstate", None) in {"40P01", "40001", "55P03", "57014"}:
            return str(getattr(error, "sqlstate"))
        if getattr(error, "code", None) in {"40P01", "40001", "55P03", "57014"}:
            return str(getattr(error, "code"))
        error = error.__cause__ or error.__context__
    return None


def database_connection_failure(error: BaseException | None) -> bool:
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        code = getattr(error, "sqlstate", None) or getattr(error, "code", None)
        if isinstance(code, str) and (
            code.startswith("08") or code in {"57P01", "57P02", "57P03"}
        ):
            return True
        if isinstance(error, pg_errors.ConnectionTimeout):
            return True
        if isinstance(error, pg_errors.OperationalError) and not error.sqlstate:
            # libpq can report a lost connection without a server SQLSTATE.
            # Unknown DSN/authentication/configuration failures must still count.
            message = str(error).lower()
            if message == "the connection is closed" or any(
                marker in message
                for marker in (
                    "server closed the connection unexpectedly",
                    "ssl connection has been closed unexpectedly",
                    "connection reset by peer",
                    "ssl syscall error: eof detected",
                )
            ):
                return True
        error = error.__cause__ or error.__context__
    return False


class HistoryImportWorker:
    def __init__(
        self,
        session_factory: typing.Callable,
        *,
        reaction_user_limit: int = 4,
        clock: typing.Callable[[], float] = time.monotonic,
    ) -> None:
        self.session_factory = session_factory
        self.reaction_user_limit = reaction_user_limit
        self.clock = clock
        self.worker_uuid = sys_uuid.uuid4()
        self.cached_uuid: sys_uuid.UUID | None = None
        self.cached_batch: contract.Batch | None = None
        self.cached_file_accounts: dict[str, list[str]] | None = None
        self.cached_mappings: preparation.MappingStore | None = None
        self.cached_mapping_key: str | None = None
        self.metadata_cache: set[str] = set()
        self.identity_key: str | None = None
        self.identity_cursor = 0
        self.identities: dict[int, str] = {}
        self.preferred_uuid: sys_uuid.UUID | None = None

    def run_once(self) -> bool:
        with self.session_factory() as session:
            writer.configure_transaction(session)
            pressure = session.execute(
                """SELECT count(*) AS count FROM (
                       SELECT 1 FROM messenger_projection_tasks
                       WHERE status IN ('pending', 'failed') LIMIT 2001
                   ) AS backlog""",
                (),
            ).fetchone()["count"]
            if pressure > 2000:
                return False
            if self.preferred_uuid is not None:
                active = session.execute(
                    "SELECT 1 FROM m_external_history_imports_v1 WHERE uuid = %s AND status IN ('pending', 'running')",
                    (self.preferred_uuid,),
                ).fetchone()
                if active is None:
                    self.preferred_uuid = None
            job = repository.claim(session, self.worker_uuid, self.preferred_uuid)
        if job is None:
            return False
        identity = types.SimpleNamespace(
            bridge_instance_uuid=job["bridge_uuid"],
            provider_kind="zulip",
            identity_generation=job["identity_generation"],
        )
        try:
            self.process(identity, job)
        except (writer.PreparationChanged, WriteBudgetExceeded) as error:
            if isinstance(error, writer.PreparationChanged):
                self.identity_key = None
                self.cached_mapping_key = None
            self.retry(
                job, type(error).__name__, shrink=isinstance(error, WriteBudgetExceeded)
            )
        except contract.ImportError as error:
            self.preferred_uuid = None
            superseded = error.code in {
                "history_sources_changed",
                "history_generation_superseded",
                "history_bridge_authority_changed",
                "history_observer_not_assigned",
            }
            with self.session_factory() as session:
                writer.configure_transaction(session)
                repository.release(
                    session,
                    job,
                    error=error.code,
                    superseded=superseded,
                    permanent=error.status == 422,
                    count_error=error.code
                    not in {
                        "history_import_lease_expired",
                        "history_provider_paused",
                    },
                    delay=2,
                )
        except (
            ra_exceptions.PropertyException,
            ra_exceptions.TypeError,
            ra_exceptions.ValidationErrorException,
        ):
            with self.session_factory() as session:
                writer.configure_transaction(session)
                repository.release(
                    session, job, error="history_invalid_domain_value", permanent=True
                )
        except Exception as error:
            # Log types, never file paths, message bodies or credentials.
            LOG.warning("History worker retry: error_type=%s", type(error).__name__)
            self.retry(
                job,
                "history_database_contention"
                if database_error_code(error)
                else "history_database_unavailable"
                if database_connection_failure(error)
                else "history_storage_unavailable"
                if transient_storage_error(error)
                else "history_processing_unavailable",
                shrink=database_error_code(error) == "57014",
            )
        return True

    def retry(self, job: dict, code: str, *, shrink: bool = False) -> None:
        self.preferred_uuid = None
        size = max(1, job["part_size"] // 2) if shrink else job["part_size"]
        with self.session_factory() as session:
            writer.configure_transaction(session)
            repository.release(
                session,
                job,
                error=code,
                count_error=code
                not in {
                    "history_database_contention",
                    "history_database_unavailable",
                    "history_storage_unavailable",
                    "WriteBudgetExceeded",
                },
                delay=0.5
                if code == "history_database_contention"
                else min(30, 2 ** min(job["attempts"], 5)),
                part_size=size,
            )

    def write(
        self, identity: typing.Any, job: dict, operation: typing.Callable
    ) -> typing.Any:
        started = self.clock()
        with self.session_factory() as session:
            result = operation(session)
            # statement_timeout bounds each SQL operation; this additional
            # whole-transaction budget rolls back an oversized prepared part.
            if self.clock() - started > WRITE_BUDGET_SECONDS:
                raise WriteBudgetExceeded()
        duration = self.clock() - started
        with self.session_factory() as session:
            writer.configure_transaction(session)
            session.execute(
                """UPDATE m_external_history_imports_v1
                   SET write_seconds = write_seconds + %s,
                       max_write_seconds = GREATEST(max_write_seconds, %s),
                       part_size = CASE WHEN %s < 0.1 THEN LEAST(100, part_size + 5) ELSE part_size END
                   WHERE uuid = %s AND lease_token = %s""",
                (duration, duration, duration, job["uuid"], job["lease_token"]),
            )
            repository.release(session, job, delay=0.1)
        LOG.info("History part committed: duration_seconds=%.4f", duration)
        return result

    def load_batch(self, job: dict) -> contract.Batch:
        if self.cached_uuid != job["uuid"]:
            storage = job["storage"]
            raw = file_storage.read_workspace_file(
                job["uuid"],
                storage_type=storage["storage_type"],
                storage_object_id=storage["storage_object_id"],
            )
            if len(raw) > contract.MAX_BODY_BYTES:
                raise contract.ImportError("history_body_too_large")
            batch = contract.Batch.parse(json.loads(raw))
            if batch.job_uuid(job["bridge_uuid"], job["identity_generation"]) != job[
                "uuid"
            ] or batch.sources != tuple(job["sources"]):
                raise contract.ImportError("history_stored_batch_mismatch")
            self.cached_uuid, self.cached_batch, self.cached_file_accounts = (
                job["uuid"],
                batch,
                None,
            )
            self.cached_mappings, self.cached_mapping_key = None, None
            self.metadata_cache.clear()
        assert self.cached_batch is not None
        return self.cached_batch

    def required_files(
        self,
        batch: contract.Batch,
        sources: list[dict],
        job: dict,
        messages: list[dict],
    ) -> list[dict]:
        requests = preparation.file_requests(
            batch, sources, job["uuid"], job["created_at"], messages=messages
        )
        if requests and self.cached_file_accounts is None:
            # A shared path must try observers from later parts too. Resolve
            # existence once in bounded ID-only pages, never reading old payloads.
            eligible = [
                message
                for message in batch.body["messages"]
                if preparation.eligible_accesses(message, sources, job["created_at"])
            ]
            missing: list[dict] = []
            for offset in range(0, len(eligible), 100):
                page = eligible[offset : offset + 100]
                with self.session_factory() as session:
                    writer.configure_transaction(session)
                    existing = preparation.existing_message_ids(session, batch, page)
                missing.extend(
                    message for message in page if message["id"] not in existing
                )
            self.cached_file_accounts = {
                request["source_path"]: request["account_uuids"]
                for request in preparation.file_requests(
                    batch, sources, job["uuid"], job["created_at"], messages=missing
                )
            }
        for request in requests:
            request["account_uuids"] = sorted(
                set(request["account_uuids"])
                | set((self.cached_file_accounts or {}).get(request["source_path"], []))
            )
        return requests

    def process(self, identity: typing.Any, job: dict) -> None:
        with self.session_factory() as session:
            writer.configure_transaction(session)
            sources = repository.guard(session, identity, job, lease=True)
        batch = self.load_batch(job)
        repository.validate_observers(batch, sources)
        if not job["files_discovered"]:

            def discover(session: typing.Any) -> None:
                writer.configure_transaction(session)
                repository.guard(session, identity, job, lease=True)
                session.execute(
                    """UPDATE m_external_history_imports_v1
                       SET files_discovered = true, consecutive_errors = 0, updated_at = now()
                       WHERE uuid = %s AND lease_token = %s""",
                    (job["uuid"], job["lease_token"]),
                )

            self.write(identity, job, discover)
            return
        # Retain affinity through this bounded batch, without retaining a SQL
        # connection or lease between parts. Otherwise alternating directories
        # can repeatedly discard each other's partially resolved identity map.
        self.preferred_uuid = job["uuid"]
        identity_key = contract.digest(
            {
                "realm": str(batch.provider_realm_uuid),
                "users": batch.body["users"],
                "sources": list(batch.sources),
                "identity_generation": job["identity_generation"],
            }
        )
        if self.identity_key != identity_key:
            self.identity_key = identity_key
            self.identity_cursor = 0
            self.identities = {}
        if self.identity_cursor < len(batch.body["users"]):
            # Resolve a bounded directory page in its own short read transaction.
            # Reuse the completed map for later message parts and batch ranges.
            page = contract.Batch(
                batch.project_uuid,
                batch.provider_realm_uuid,
                batch.generation,
                batch.sources,
                {
                    "users": batch.body["users"][
                        self.identity_cursor : self.identity_cursor + 100
                    ]
                },
            )
            with self.session_factory() as session:
                writer.configure_transaction(session)
                resolved = preparation.load_identities(session, page)
                repository.release(session, job, delay=0.1)
            self.identities.update(resolved)
            self.identity_cursor += len(page.body["users"])
            return
        identities = self.identities
        if job["directory_cursor"] < len(batch.body["users"]):
            page = contract.Batch(
                batch.project_uuid,
                batch.provider_realm_uuid,
                batch.generation,
                batch.sources,
                {
                    "users": batch.body["users"][
                        job["directory_cursor"] : job["directory_cursor"]
                        + job["part_size"]
                    ]
                },
            )
            records = preparation.directory_records(page, identities)
            encoded = contract.encode(records).decode()
            self.write(
                identity,
                job,
                lambda session: writer.write_directory(
                    session, identity, job, records, encoded
                ),
            )
            return
        messages = next(
            contract.message_parts(
                [
                    message
                    for message in batch.body["messages"]
                    if message["id"] > job["next_message_id"]
                ],
                job["part_size"],
            ),
            [],
        )
        if not messages:
            self.write(
                identity,
                job,
                lambda session: writer.flush_notifications(session, identity, job),
            )
            return
        eligible_messages = [
            message
            for message in messages
            if preparation.eligible_accesses(message, sources, job["created_at"])
        ]
        with self.session_factory() as session:
            writer.configure_transaction(session)
            references = preparation.referenced_message_ids(eligible_messages)
            references.update(message["id"] for message in messages)
            existing = preparation.existing_messages(
                session, batch, [{"id": value} for value in references]
            )
        absent_messages = [
            message for message in eligible_messages if message["id"] not in existing
        ]
        requests = self.required_files(batch, sources, job, absent_messages)
        file_uuids = [sys_uuid.UUID(request["uuid"]) for request in requests]
        files: dict[str, dict] = {}
        for offset in range(0, len(file_uuids), 100):
            with self.session_factory() as session:
                writer.configure_transaction(session)
                files.update(
                    (row["source_path"], dict(row))
                    for row in session.execute(
                        "SELECT * FROM m_external_history_files_v1 WHERE job_uuid = %s AND uuid = ANY(%s::uuid[])",
                        (job["uuid"], file_uuids[offset : offset + 100]),
                    ).fetchall()
                )
        # Only new canonical messages need source attachments. Old manifests or
        # realtime arrivals must not hold preserved messages behind a global gate.
        missing = [
            request
            for request in requests
            if request["source_path"] not in files
            or files[request["source_path"]]["status"] == "missing"
            or (
                files[request["source_path"]]["status"] == "unavailable"
                and not set(request["account_uuids"]).issubset(
                    files[request["source_path"]]["account_uuids"]
                )
            )
        ]
        if missing:
            encoded = contract.encode(missing[: job["part_size"]]).decode()
            self.write(
                identity,
                job,
                lambda session: writer.write_missing_files(
                    session,
                    identity,
                    job,
                    encoded,
                    [message["id"] for message in absent_messages],
                ),
            )
            return
        mapping_key = contract.digest(
            {"sources": list(batch.sources), "identities": identities}
        )
        if self.cached_mapping_key != mapping_key:
            self.cached_mappings = preparation.mappings_for(
                batch, sources, identities, {}, job["created_at"]
            )
            self.cached_mapping_key = mapping_key
        assert self.cached_mappings is not None
        mapping_store = self.cached_mappings
        # Resolve only this part's existing messages and quote targets. Do not
        # re-read all 5000 messages after each 100-message commit.
        for source_id, prior in existing.items():
            key = ("message", str(source_id))
            if prior["placement_uuid"] is None or prior["deleted_at"] is not None:
                mapping_store.mappings.pop(key, None)
            else:
                mapping_store.mappings[key] = {
                    "workspace_uuid": str(
                        prior["legacy_public_uuid"] or prior["placement_uuid"]
                    ),
                    "provider_id": str(source_id),
                    "metadata": {"author_uuid": str(prior["author_uuid"])},
                }
        part = preparation.prepare_part(
            batch,
            job,
            messages,
            sources,
            identities,
            existing,
            files,
            mapping_store,
            reaction_user_limit=self.reaction_user_limit,
            metadata_cache=self.metadata_cache,
        )
        self.write(
            identity,
            job,
            lambda session: writer.write_part(session, identity, job, part),
        )
