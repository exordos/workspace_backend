# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Concrete isolated-test fixture adapter for PostgreSQL/S3/Provider API."""

import datetime
import json
import uuid as sys_uuid

from restalchemy.dm import filters as dm_filters

from workspace.external_bridge_control import pki
from workspace.external_bridge_control import provider_data
from workspace.external_bridge_control import provider_event_apply
from workspace.external_bridge_control import sql_state
from workspace.messenger_api import application_services
from workspace.messenger_api import events as messenger_events
from workspace.messenger_api import file_storage
from workspace.messenger_api.dm import external_models
from workspace.messenger_api.dm import helpers
from workspace.messenger_api.dm import message_payloads
from workspace.messenger_api.dm import models
from workspace.tests.scale import fixture
from workspace.tests.scale import concrete_inventory


_RUN_TABLE = "test_workspace_fixture_runs_v1"
_UNIT_TABLE = "test_workspace_fixture_units_v1"
_RESOURCE_TABLE = "test_workspace_fixture_resources_v1"
_OBSERVATION_TABLE = "test_workspace_fixture_observations_v1"
_CLEANUP_KIND_ORDER = (
    "reaction",
    "file",
    "event",
    "message",
    "stream",
    "external_chat",
    "external_account",
    "user",
)


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _timestamp(value):
    return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))


def _optional_storage_read(reader):
    try:
        return reader()
    except FileNotFoundError:
        return None
    except Exception as error:
        response = getattr(error, "response", {})
        code = str(response.get("Error", {}).get("Code", ""))
        status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if code in {"404", "NoSuchKey", "NotFound"} or status == 404:
            return None
        raise


class ConcreteFixtureAdapter:
    """Apply deterministic fixture records through production service seams."""

    def __init__(self, credentials):
        self._credentials = credentials
        self._logical_to_iam = {
            str(row["logical_user_uuid"]): sys_uuid.UUID(row["iam_user_uuid"])
            for row in credentials["workspace_identity_mappings"]
        }
        self._iam_to_logical = {
            str(value): key for key, value in self._logical_to_iam.items()
        }
        self._account_credentials = {
            row["credential_ref"]: row
            for row in credentials["external_account_credentials"]
        }
        self._inventory_contract = None

    def bind_inventory_contract(self, manifest, units):
        """Bind validated planner input used only to scope and order reads."""
        self._inventory_contract = (manifest, units)

    @staticmethod
    def _ensure_tables(session):
        session.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {_RUN_TABLE} (
                run_uuid UUID PRIMARY KEY,
                project_uuid UUID NOT NULL,
                contract JSONB NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """,
            (),
        )
        session.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {_UNIT_TABLE} (
                run_uuid UUID NOT NULL REFERENCES {_RUN_TABLE}(run_uuid)
                    ON DELETE CASCADE,
                unit_uuid UUID NOT NULL,
                records_sha256 TEXT NOT NULL,
                completed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (run_uuid, unit_uuid)
            )
            """,
            (),
        )
        session.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {_RESOURCE_TABLE} (
                run_uuid UUID NOT NULL REFERENCES {_RUN_TABLE}(run_uuid)
                    ON DELETE CASCADE,
                record_kind TEXT NOT NULL,
                logical_uuid UUID NOT NULL,
                actual_uuid UUID NOT NULL,
                cleanup JSONB NOT NULL,
                PRIMARY KEY (run_uuid, record_kind, logical_uuid)
            )
            """,
            (),
        )
        session.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {_OBSERVATION_TABLE} (
                run_uuid UUID NOT NULL REFERENCES {_RUN_TABLE}(run_uuid)
                    ON DELETE CASCADE,
                operation_uuid UUID NOT NULL,
                observation JSONB NOT NULL,
                PRIMARY KEY (run_uuid, operation_uuid)
            )
            """,
            (),
        )

    @staticmethod
    def _run_contract(run):
        return {
            name: run[name]
            for name in (
                "schema_version",
                "run_id",
                "test_project_id",
                "profile_id",
                "profile_sha256",
                "manifest_sha256",
                "expected_ledger_sha256",
                "application_plan_sha256",
            )
        }

    def prepare(self, session, run):
        self._ensure_tables(session)
        contract = self._run_contract(run)
        other = session.execute(
            f"SELECT run_uuid FROM {_RUN_TABLE} "
            "WHERE project_uuid = %s AND run_uuid != %s LIMIT 1",
            (run["test_project_id"], run["run_id"]),
        ).fetchone()
        if other is not None:
            raise ValueError("fixture project contains resources from another run")
        existing = session.execute(
            f"SELECT contract FROM {_RUN_TABLE} WHERE run_uuid = %s",
            (run["run_id"],),
        ).fetchone()
        if existing is None:
            session.execute(
                f"INSERT INTO {_RUN_TABLE} (run_uuid, project_uuid, contract) "
                "VALUES (%s, %s, %s::jsonb)",
                (run["run_id"], run["test_project_id"], _json(contract)),
            )
        elif existing["contract"] != contract:
            raise ValueError("fixture run contract changed during resume")
        rows = session.execute(
            f"SELECT unit_uuid FROM {_UNIT_TABLE} "
            "WHERE run_uuid = %s ORDER BY unit_uuid",
            (run["run_id"],),
        ).fetchall()
        return {"completed_unit_ids": [str(row["unit_uuid"]) for row in rows]}

    def _map_user(self, value):
        try:
            return self._logical_to_iam[str(value)]
        except KeyError as error:
            raise ValueError(
                "fixture record references an unmapped logical user"
            ) from error

    @staticmethod
    def _project(run):
        return sys_uuid.UUID(run["test_project_id"])

    @staticmethod
    def _resource_exists(session, run, record):
        return session.execute(
            f"SELECT actual_uuid FROM {_RESOURCE_TABLE} "
            "WHERE run_uuid = %s AND record_kind = %s AND logical_uuid = %s",
            (run["run_id"], record["record_kind"], record["record_key"]),
        ).fetchone()

    @staticmethod
    def _record_resource(session, run, record, actual_uuid, cleanup=None):
        session.execute(
            f"""
            INSERT INTO {_RESOURCE_TABLE} (
                run_uuid, record_kind, logical_uuid, actual_uuid, cleanup
            ) VALUES (%s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (run_uuid, record_kind, logical_uuid) DO NOTHING
            """,
            (
                run["run_id"],
                record["record_kind"],
                record["record_key"],
                actual_uuid,
                _json(cleanup or {}),
            ),
        )

    def _identity_for_account(self, session, account_uuid):
        account = external_models.ExternalAccount.objects.get_one(
            filters={"uuid": dm_filters.EQ(account_uuid)},
            session=session,
        )
        credential = application_services.external_credential(account, session)
        associated = credential.envelope["associated_data"]
        return pki.BridgeIdentity(
            realm_uuid=sys_uuid.UUID(associated["realm_uuid"]),
            provider_kind=associated["provider_kind"],
            bridge_instance_uuid=sys_uuid.UUID(associated["bridge_instance_uuid"]),
            identity_generation=associated["identity_generation"],
            uri_san="urn:workspace:fixture-bridge",
        )

    def _apply_user(self, session, run, record):
        del run
        actual_uuid = self._map_user(record["record_key"])
        user = models.WorkspaceUser.objects.get_one_or_none(
            filters={"uuid": dm_filters.EQ(actual_uuid)},
            session=session,
        )
        if user is None:
            raise ValueError("mapped IAM user does not exist in Workspace")
        return actual_uuid, {}

    def _apply_external_account(self, session, run, record):
        values = record["values"]
        private = self._account_credentials.get(values["credential_ref"])
        if private is None:
            raise ValueError("fixture external account credential is missing")
        settings = values["settings"]
        account = application_services.ExternalAccountApplicationService.create(
            session,
            application_services.ExternalAccountActor(
                self._map_user(values["owner_user_uuid"]),
                self._project(run),
            ),
            {
                "uuid": values["uuid"],
                "settings": {
                    "kind": values["provider"],
                    "server_url": private["server_url"],
                    "email": private["email"],
                    "api_key": private["api_key"],
                    "selection_mode": settings["selection_mode"],
                    "history_depth": settings["history_depth"],
                    "default_project_id": self._project(run),
                },
            },
        )
        return account.uuid, {"credential_ref": values["credential_ref"]}

    def _apply_external_chat(self, session, run, record):
        values = record["values"]
        account_uuid = sys_uuid.UUID(values["external_account_uuid"])
        identity = self._identity_for_account(session, account_uuid)
        catalog = dict(values["catalog_report_spec"])
        catalog.update(
            {
                "owner_user_uuid": str(self._map_user(catalog["owner_user_uuid"])),
                "project_id": str(self._project(run)),
            }
        )
        report = {
            "report_uuid": str(
                sys_uuid.uuid5(sys_uuid.UUID(values["uuid"]), "fixture-catalog")
            ),
            "resource_type": "external_chat_catalog",
            "resource_uuid": values["uuid"],
            "observed_generation": 1,
            "status": "ready",
            "progress": {
                "phase": "discovery",
                "completed": 1,
                "total": 1,
                "last_progress_at": "2026-07-18T00:00:00+00:00",
            },
            "safe_error": None,
            "observed_at": "2026-07-18T00:00:00+00:00",
            "catalog": catalog,
        }
        state = sql_state.SQLControlState(identity.realm_uuid, b"fixture-only-key")
        result = state.reconcile_observed_reports(session, identity, [report])
        if result["results"][0]["status"] not in {"applied", "duplicate"}:
            raise ValueError("fixture external chat catalog was rejected")
        return sys_uuid.UUID(values["uuid"]), {}

    def _apply_stream(self, session, run, record):
        values = record["values"]
        owner_uuid = self._map_user(values["owner_user_uuid"])
        members = [self._map_user(value) for value in values["members"]]
        binding_uuids = {
            str(self._map_user(binding["user_uuid"])): binding["uuid"]
            for binding in values["bindings"]
        }
        default = next(
            topic
            for topic in values["topics"]
            if topic["uuid"] == values["default_topic_uuid"]
        )
        stream_values = {
            "uuid": values["uuid"],
            "name": values["name"],
            "description": "",
            "source_name": "native",
            "source": models.NativeSource(),
            "private": values["private"],
            "invite_only": values["invite_only"],
            "canonical_default_topic_uuid": default["uuid"],
            "default_topic_name": default["name"],
            "canonical_binding_uuids": binding_uuids,
        }
        identity = None
        if values["provider_synced"]:
            identity = self._identity_for_account(
                session,
                sys_uuid.UUID(values["external_account_uuid"]),
            )
            stream_values.update(
                {
                    "external_account_uuid": values["external_account_uuid"],
                    "provider_uuid": identity.bridge_instance_uuid,
                    "provider_external_id": values["provider_chat_key"],
                    "provider_metadata": {
                        "kind": "zulip",
                        "external_chat_uuid": values["external_chat_uuid"],
                    },
                }
            )
        if values["kind"] == "direct_dm":
            stream_values["direct_user_uuid"] = next(
                value for value in members if value != owner_uuid
            )
        if values["kind"] == "group_dm":
            stream = helpers.create_workspace_private_group_stream(
                self._project(run),
                owner_uuid,
                session=session,
                **stream_values,
            )
        else:
            stream = helpers.get_or_create_workspace_user_stream(
                self._project(run),
                owner_uuid,
                session=session,
                **stream_values,
            )
        roles = {}
        for binding in values["bindings"]:
            actual_user = self._map_user(binding["user_uuid"])
            if actual_user == owner_uuid:
                continue
            roles.setdefault(binding["role"], []).append(actual_user)
        if roles:
            helpers.get_or_create_workspace_stream_bindings(
                self._project(run),
                stream.uuid,
                owner_uuid,
                roles,
                session=session,
                binding_uuids=binding_uuids,
            )
        for topic in values["topics"]:
            if topic["uuid"] == values["default_topic_uuid"]:
                continue
            helpers.create_workspace_user_stream_topic(
                self._project(run),
                owner_uuid,
                {
                    "uuid": topic["uuid"],
                    "stream_uuid": stream.uuid,
                    "name": topic["name"],
                    "source_name": "native",
                    "source": models.NativeSource(),
                },
                session=session,
            )
        if values["provider_synced"]:
            for topic in values["topics"]:
                provider_topic = next(
                    item for item in values["topics"] if item["uuid"] == topic["uuid"]
                )
                helpers.update_workspace_user_stream_topic(
                    self._project(run),
                    owner_uuid,
                    topic["uuid"],
                    {
                        "external_account_uuid": values["external_account_uuid"],
                        "provider_uuid": identity.bridge_instance_uuid,
                        "provider_external_id": provider_topic["provider_topic_id"],
                        "provider_metadata": {
                            "kind": "zulip",
                            "external_chat_uuid": values["external_chat_uuid"],
                        },
                    },
                    session=session,
                )
        if values["provider_synced"]:
            application_services.ExternalChatApplicationService.select_materialized(
                session,
                application_services.ExternalAccountActor(
                    owner_uuid,
                    self._project(run),
                ),
                values["external_chat_uuid"],
            )
        return stream.uuid, {}

    def _apply_message(self, session, run, record):
        values = record["values"]
        provider = values["provider_operation"]
        if provider is not None and provider["direction"] == "inbound":
            contract = provider["provider_contract"]["event"]
            event = json.loads(_json(contract))
            event["project_id"] = str(self._project(run))
            event["payload"]["resource"]["user_uuid"] = str(
                self._map_user(values["sender_uuid"])
            )
            identity = self._identity_for_account(
                session,
                sys_uuid.UUID(values["external_account_uuid"]),
            )
            result = provider_data.apply_provider_event_batch(
                session,
                identity,
                [event],
                provider_event_apply.apply_event,
            )
            if result["results"][0]["status"] not in {"applied", "duplicate"}:
                raise ValueError("fixture provider event was rejected")
        else:
            helpers.create_workspace_user_message(
                self._project(run),
                self._map_user(values["sender_uuid"]),
                session=session,
                enforce_visibility=False,
                return_visible=False,
                compact_events=True,
                uuid=values["uuid"],
                stream_uuid=values["stream_uuid"],
                topic_uuid=values["topic_uuid"],
                payload=message_payloads.WORKSPACE_MESSAGE_PAYLOAD_TYPE.from_simple_type(
                    values["payload"]
                ),
            )
            if provider is not None:
                arguments = dict(provider["provider_contract"]["arguments"])
                identity = self._identity_for_account(
                    session,
                    sys_uuid.UUID(values["external_account_uuid"]),
                )
                arguments.update(
                    {
                        "bridge_instance_uuid": identity.bridge_instance_uuid,
                        "project_id": self._project(run),
                        "owner_user_uuid": self._map_user(values["sender_uuid"]),
                    }
                )
                provider_data.enqueue_provider_operation(session, **arguments)
        if provider is not None and provider["direction"] == "inbound":
            self._record_observation(session, run, provider)
        return sys_uuid.UUID(values["uuid"]), {}

    def _record_observation(self, session, run, provider):
        if provider["direction"] != "inbound":
            raise ValueError("only destination-applied inbound events are observable")
        observation = {
            "schema_version": fixture.RUN_LEDGER_SCHEMA_VERSION,
            "run_id": run["run_id"],
            "source": "fixture.provider",
            "operation_uuid": provider["operation_uuid"],
            "operation_kind": f"provider.message.{provider['direction']}",
            "account_uuid": provider["account_uuid"],
            "owner_user_uuid": provider["owner_user_uuid"],
            "stream_uuid": provider["stream_uuid"],
            "topic_uuid": provider["topic_uuid"],
            "provider_event_uuid": provider["provider_event_uuid"],
            "payload_sha256": provider["payload_sha256"],
            "cursor_scope": provider["cursor_scope"],
            "cursor_ordinal": provider["cursor_ordinal"],
            "idempotency_key": provider["outbox_idempotency_key"],
            "outcome": "succeeded",
            "result_id": provider["operation_uuid"],
            "evidence_source": "workspace_backend",
        }
        session.execute(
            f"INSERT INTO {_OBSERVATION_TABLE} "
            "(run_uuid, operation_uuid, observation) VALUES (%s, %s, %s::jsonb) "
            "ON CONFLICT (run_uuid, operation_uuid) DO NOTHING",
            (run["run_id"], provider["operation_uuid"], _json(observation)),
        )

    def _apply_event(self, session, run, record):
        values = record["values"]
        message = models.WorkspaceMessage.objects.get_one(
            filters={
                "project_id": dm_filters.EQ(self._project(run)),
                "uuid": dm_filters.EQ(values["message_uuid"]),
            },
            session=session,
        )
        recipients = message.get_recipients(session=session)
        messenger_events.create_deterministic_fixture_broadcast_event(
            self._project(run),
            message.uuid,
            recipients,
            values["event_kind"],
            {
                "uuid": str(message.uuid),
                "stream_uuid": str(message.stream_uuid),
                "topic_uuid": str(message.topic_uuid),
            },
            values["event_uuid"],
            _timestamp(values["created_at"]),
            session,
        )
        return sys_uuid.UUID(values["event_uuid"]), {}

    def _apply_reaction(self, session, run, record):
        values = record["values"]
        reaction = helpers.create_workspace_message_reaction(
            self._project(run),
            self._map_user(values["user_uuid"]),
            session=session,
            enforce_visibility=False,
            compact_events=True,
            uuid=values["uuid"],
            message_uuid=values["message_uuid"],
            emoji_name=values["emoji_name"],
        )
        return reaction.uuid, {}

    def _apply_file(self, session, run, record):
        values = record["values"]
        binary = fixture.file_content_from_recipe(values["content_recipe"])
        if fixture.sha256(binary) != values["binary_sha256"]:
            raise ValueError("fixture file content digest does not match")
        storage = file_storage.get_workspace_file_storage("s3")
        sidecar = json.loads(_json(values["sidecar"]))
        sidecar.update(
            {
                "owner_uuid": str(self._map_user(sidecar["owner_uuid"])),
                "project_id": str(self._project(run)),
            }
        )
        metadata = file_storage.WorkspaceFileMetadata.from_json(_json(sidecar).encode())
        existing_binary = _optional_storage_read(
            lambda: storage.read(values["uuid"], values["object_name"])
        )
        if existing_binary is not None and existing_binary != binary:
            raise ValueError("fixture S3 object already exists with different content")
        existing_metadata = _optional_storage_read(
            lambda: storage.read_metadata(values["uuid"])
        )
        if existing_metadata is not None and (
            json.loads(existing_metadata.to_json()) != sidecar
        ):
            raise ValueError("fixture S3 sidecar already exists with different content")
        binary_created = existing_binary is None
        metadata_created = existing_metadata is None
        try:
            if binary_created:
                info = storage.save(values["uuid"], binary, values["object_name"])
            else:
                info = file_storage.WorkspaceFileStorageInfo(
                    storage_type=storage.storage_type,
                    storage_id=storage.storage_id,
                    storage_object_id=values["object_name"],
                )
            if storage.read(values["uuid"], values["object_name"]) != binary:
                raise ValueError("fixture S3 content readback does not match")
            if metadata_created:
                storage.save_metadata(values["uuid"], metadata)
            if json.loads(storage.read_metadata(values["uuid"]).to_json()) != sidecar:
                raise ValueError("fixture S3 metadata readback does not match")
            row = helpers.create_workspace_file(
                self._project(run),
                self._map_user(values["sidecar"]["owner_uuid"]),
                values["uuid"],
                session=session,
                name=sidecar["name"],
                description=sidecar["description"],
                stream_uuid=values["sidecar"]["stream_uuid"],
                acl_mode="stream",
                content_type=sidecar["content_type"],
                size_bytes=len(binary),
                hash=values["binary_sha256"],
                storage_type=info.storage_type,
                storage_id=info.storage_id,
                storage_object_id=info.storage_object_id,
            )
        except Exception:
            if metadata_created:
                storage.delete_metadata(values["uuid"])
            if binary_created:
                storage.delete(values["uuid"], values["object_name"])
            raise
        return row.uuid, {
            "storage_type": info.storage_type,
            "storage_id": info.storage_id,
            "storage_object_id": info.storage_object_id,
            "metadata_object_id": values["sidecar_object_name"],
        }

    def _apply_record(self, session, run, record):
        if self._resource_exists(session, run, record) is not None:
            return
        handler = getattr(self, f"_apply_{record['record_kind']}")
        if record["record_kind"] == "event":
            actual_uuid, cleanup = handler(session, run, record)
        else:
            with messenger_events.suppress_unplanned_fixture_events():
                actual_uuid, cleanup = handler(session, run, record)
        self._record_resource(session, run, record, actual_uuid, cleanup)

    def apply_unit(self, session, run, unit):
        existing = session.execute(
            f"SELECT records_sha256 FROM {_UNIT_TABLE} "
            "WHERE run_uuid = %s AND unit_uuid = %s",
            (run["run_id"], unit["unit_id"]),
        ).fetchone()
        if existing is not None:
            if existing["records_sha256"] != unit["records_sha256"]:
                raise ValueError("fixture unit changed during resume")
            return {"unit_id": unit["unit_id"]}
        for record in unit["records"]:
            self._apply_record(session, run, record)
        session.execute(
            f"INSERT INTO {_UNIT_TABLE} "
            "(run_uuid, unit_uuid, records_sha256) VALUES (%s, %s, %s)",
            (run["run_id"], unit["unit_id"], unit["records_sha256"]),
        )
        return {"unit_id": unit["unit_id"]}

    def export_observed(self, session, run):
        rows = session.execute(
            f"SELECT observation FROM {_OBSERVATION_TABLE} "
            "WHERE run_uuid = %s ORDER BY operation_uuid",
            (run["run_id"],),
        ).fetchall()
        return [row["observation"] for row in rows]

    def export_inventory(self, session, run):
        if self._inventory_contract is None:
            raise ValueError("fixture inventory contract is not bound")
        manifest, units = self._inventory_contract
        return concrete_inventory.ActualInventoryExporter(
            self._credentials["workspace_identity_mappings"],
        ).export(session, run, manifest, units)

    def cleanup_manifest(self, session, run):
        rows = session.execute(
            f"SELECT record_kind, logical_uuid, actual_uuid, cleanup "
            f"FROM {_RESOURCE_TABLE} WHERE run_uuid = %s",
            (run["run_id"],),
        ).fetchall()
        cleanup_rank = {
            record_kind: index for index, record_kind in enumerate(_CLEANUP_KIND_ORDER)
        }
        rows = sorted(
            rows,
            key=lambda row: (
                cleanup_rank.get(row["record_kind"], len(cleanup_rank)),
                str(row["logical_uuid"]),
            ),
        )
        used_refs = {
            row["cleanup"].get("credential_ref")
            for row in rows
            if row["cleanup"].get("credential_ref") is not None
        }
        if used_refs != set(self._account_credentials):
            raise ValueError(
                "fixture external account credential coverage is not exact"
            )
        resources = []
        for row in rows:
            if row["record_kind"] == "user":
                continue
            cleanup = {
                key: value
                for key, value in row["cleanup"].items()
                if key != "credential_ref"
            }
            resources.append(
                {
                    "record_kind": row["record_kind"],
                    "logical_uuid": str(row["logical_uuid"]),
                    "actual_uuid": (
                        self._iam_to_logical.get(
                            str(row["actual_uuid"]),
                            str(row["actual_uuid"]),
                        )
                    ),
                    "cleanup": cleanup,
                }
            )
        return {
            "run_id": run["run_id"],
            "project_id": run["test_project_id"],
            "delete_order": resources,
            "ledger_tables": [
                _OBSERVATION_TABLE,
                _RESOURCE_TABLE,
                _UNIT_TABLE,
                _RUN_TABLE,
            ],
        }


def create_adapter(credentials):
    return ConcreteFixtureAdapter(credentials)
