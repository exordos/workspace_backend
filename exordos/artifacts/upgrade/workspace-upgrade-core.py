#!/usr/bin/env python3
# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

"""Prepare installed UI compatibility and a bridge-local handoff through native Core."""

import argparse
import configparser
import hashlib
import sys
import urllib.request
import uuid

import psycopg
from packaging import version
from restalchemy.common import contexts
from restalchemy.storage.sql import engines

HANDOFF_SCHEMA = """
CREATE TABLE IF NOT EXISTS workspace_zulip_v5_credential_handoff (
    account_uuid uuid PRIMARY KEY,
    owner_uuid uuid NOT NULL,
    source_hash text NOT NULL,
    sealed jsonb NOT NULL,
    verified_at timestamptz,
    settings_hash text,
    created_at timestamptz NOT NULL DEFAULT now()
);
"""
EXPORT_PATH = "/etc/workspace-zulip-bridge/workspace-v5-handoff.py"
EXPORT_COMMAND = "/opt/workspace-zulip-bridge-venv/bin/python " + EXPORT_PATH


UI_MIN_VERSION = "0.9.2"


def ensure_compatible_ui(core_config):
    """Use the native upgrade path for an installed client needing v5 DTOs."""
    from exordos_core.repo.dm import models as repo_models

    engines.engine_factory.configure_factory(db_url=core_config["db"]["connection_url"])
    try:
        with contexts.Context().session_manager():
            installed = repo_models.RepoElement.objects.get_all(
                filters={"name": "workspace_ui", "installation_state": "INSTALLED"}
            )
            if not installed or version.Version(
                installed[0].version
            ) >= version.Version(UI_MIN_VERSION):
                return False
            candidates = repo_models.RepoElement.objects.get_all(
                filters={
                    "name": "workspace_ui",
                    "version": UI_MIN_VERSION,
                    "status": "AVAILABLE",
                    "installation_state": "UNINSTALLED",
                }
            )
            if not candidates:
                raise RuntimeError("Workspace v5 requires the released UI 0.9.2")
            target = max(candidates, key=lambda item: item.repository.priority)
            installed[0].upgrade(str(target.uuid))
            return True
    finally:
        engines.engine_factory.destroy_all_engines()


def prepare(settings, core_config):
    with psycopg.connect(settings["connection_url"], autocommit=True) as database:
        if (
            database.execute(
                "SELECT to_regclass('public.m_external_credentials_v2')"
            ).fetchone()[0]
            is None
        ):
            return 0
        count = database.execute(
            "SELECT count(*) FROM m_external_credentials_v2 AS credential "
            "JOIN m_external_accounts_v2 AS account "
            "ON account.uuid=credential.external_account_uuid WHERE account.provider='zulip'"
        ).fetchone()[0]
        if not count:
            return 0
        database.execute(HANDOFF_SCHEMA)
    with urllib.request.urlopen(settings["exporter_url"], timeout=60) as response:
        exporter = response.read()
    if hashlib.sha256(exporter).hexdigest() != settings["exporter_sha256"]:
        raise RuntimeError("Legacy handoff artifact checksum mismatch")
    content = (
        "UPGRADE_CONFIG = "
        + repr(
            {
                "connection_url": settings["connection_url"],
                "encryption_key": settings["encryption_key"],
            }
        )
        + "\n"
        + exporter.decode()
    )
    from exordos_core.common.dm import targets
    from exordos_core.compute.dm import models as compute_models
    from exordos_core.config.dm import models as config_models

    engines.engine_factory.configure_factory(db_url=core_config["db"]["connection_url"])
    try:
        with engines.engine_factory.get_engine().session_manager() as session:
            bridges = session.execute("""
                SELECT resource.uuid FROM em_resources AS resource
                JOIN em_elements AS element ON element.uuid=resource.element
                WHERE element.name='workspace_zulip_bridge'
                  AND resource.resource_link_prefix='$core.compute.nodes'
                  AND resource.name='workspace_zulip_bridge'
            """).fetchall()
            if len(bridges) != 1:
                raise RuntimeError(
                    "Legacy credential handoff requires its installed bridge"
                )
            node_uuid = bridges[0]["uuid"]
            node = compute_models.Node.objects.get_one(
                filters={"uuid": str(node_uuid)},
                session=session,
            )
            config_uuid = uuid.uuid5(node_uuid, "workspace-v5-credential-handoff")
            item = config_models.Config.objects.get_one_or_none(
                filters={"uuid": str(config_uuid)},
                session=session,
            )
            if item is None:
                config_models.Config(
                    uuid=config_uuid,
                    project_id=node.project_id,
                    name="workspace-v5-credential-handoff",
                    description="One-time legacy Zulip credential rewrapping",
                    path=EXPORT_PATH,
                    mode="0600",
                    owner="root",
                    group="root",
                    target=targets.NodeTarget(node=node_uuid),
                    body=config_models.TextBodyConfig(content=content),
                    on_change=config_models.OnChangeShell(command=EXPORT_COMMAND),
                ).insert(session=session)
            elif item.body.render() != content:
                item.body = config_models.TextBodyConfig(content=content)
                item.on_change = config_models.OnChangeShell(command=EXPORT_COMMAND)
                item.update(session=session)
        return count
    finally:
        engines.engine_factory.destroy_all_engines()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-file", required=True)
    args = parser.parse_args()
    settings = configparser.ConfigParser(interpolation=None)
    settings.read(args.config_file)
    core_config = configparser.ConfigParser(interpolation=None)
    core_config.read("/etc/exordos_core/exordos_core.conf")
    updated_ui = ensure_compatible_ui(core_config)
    count = prepare(settings["workspace_upgrade"], core_config)
    if updated_ui:
        print("Native Workspace UI upgrade to 0.9.2 requested")
    print(f"Native legacy credential handoff prepared for {count} accounts")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:  # noqa: BLE001 - keep credentials out of deployment logs.
        print(
            f"Native Workspace upgrade preparation failed ({type(error).__name__})",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
