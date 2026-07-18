# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import json
import logging
import pathlib
import stat
import sys
import threading
import typing

from oslo_config import cfg
from restalchemy.common import config_opts as ra_config_opts
from restalchemy.common import contexts
from restalchemy.storage.sql import engines

from workspace.common import config
from workspace.common import external_bridge_control_opts
from workspace.common import file_storage_opts
from workspace.common import log as infra_log
from workspace.external_bridge_control import files
from workspace.external_bridge_control import file_repository
from workspace.external_bridge_control import pki
from workspace.external_bridge_control import provider_event_apply
from workspace.external_bridge_control import provider_service
from workspace.external_bridge_control import server
from workspace.external_bridge_control import service
from workspace.external_bridge_control import sql_state
from workspace.messenger_migration import writer_gate


CONF = cfg.CONF
external_bridge_control_opts.register_opts(CONF)
file_storage_opts.register_opts(CONF)
ra_config_opts.register_posgresql_db_opts(CONF)


def _load_enrollments(
    path: str | pathlib.Path,
) -> list[dict[str, typing.Any]]:
    path = pathlib.Path(path)
    mode = path.stat().st_mode
    if path.is_symlink() or not path.is_file() or mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ValueError("Enrollment config must be a private regular file")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload["schema_version"] != 1 or not isinstance(payload["enrollments"], list):
        raise ValueError("Enrollment config schema is invalid")
    return payload["enrollments"]


def _validate_persistent_root(
    path: str | pathlib.Path,
    require_dedicated_filesystem: bool,
) -> None:
    path = pathlib.Path(path)
    state = path.lstat()
    parent_state = path.parent.lstat()
    if (
        path.is_symlink()
        or not path.is_dir()
        or state.st_mode & 0o077
        or state.st_uid != 0
        or state.st_gid != 0
    ):
        raise ValueError(
            "External bridge persistent store must be a root-owned mode-0700 directory"
        )
    if require_dedicated_filesystem and state.st_dev == parent_state.st_dev:
        raise ValueError(
            "External bridge persistent store is not a dedicated filesystem"
        )


def build_runtime(
    conf: cfg.ConfigOpts = CONF,
) -> tuple[server.BootstrapServer, server.PrivateServer]:
    options = conf[external_bridge_control_opts.DOMAIN]
    root = pathlib.Path(options.persistent_store_path)
    _validate_persistent_root(root, options.require_dedicated_filesystem)
    control_pki = pki.PersistentControlPki(
        root / "pki",
        options.realm_uuid,
        options.hostname,
    )
    control_pki.initialize()
    enrollments = _load_enrollments(options.enrollment_config_path)
    engines.engine_factory.configure_postgresql_factory(conf=conf)
    session_factory = engines.engine_factory.get_engine().session_manager
    request_context = contexts.Context()
    with session_factory() as session:
        for enrollment in enrollments:
            sql_state.ensure_bridge_instance(
                session,
                enrollment["bridge_instance_uuid"],
                enrollment["provider_kind"],
                enrollment["enrollment_generation"],
            )
    for enrollment in enrollments:
        control_pki.register_enrollment(
            enrollment["bridge_instance_uuid"],
            enrollment["provider_kind"],
            enrollment["enrollment_generation"],
            enrollment["enrollment_token"],
        )
    control_state = sql_state.SQLControlState(
        options.realm_uuid,
        control_pki.control_hmac_key(),
    )
    base_url = f"https://{options.hostname}:{options.https_port}"
    canonical_files = file_repository.CanonicalFileRepository()
    file_manager = files.ExternalFileTransferManager(
        control_state,
        base_url,
        control_pki.control_hmac_key(),
        resolve_workspace_file=canonical_files.resolve,
        commit_file_projection=canonical_files.commit_projection,
    )

    def enrollment_persist(
        identity: pki.BridgeIdentity,
        encryption_public_key: dict[str, str],
    ) -> None:
        sql_state.persist_encryption_target(
            contexts.Context().get_session(),
            identity,
            encryption_public_key,
        )

    private_service = service.PrivateBridgeService(
        control_pki,
        control_state,
        file_manager,
        enrollment_persist=enrollment_persist,
        provider_data_service=provider_service.ProviderDataService(
            apply_event=provider_event_apply.apply_event,
        ),
    )
    bootstrap_server = server.BootstrapServer(
        (options.bind_host, options.bootstrap_port), control_pki
    )
    private_server = server.PrivateServer(
        (options.bind_host, options.https_port),
        private_service,
        control_pki.build_server_ssl_context(),
        request_session_factory=request_context.session_manager,
    )
    return bootstrap_server, private_server


def main() -> None:
    config.parse(sys.argv[1:])
    infra_log.configure()
    log = logging.getLogger(__name__)
    bootstrap_server, private_server = build_runtime(CONF)
    heartbeat_stop, heartbeat_thread = writer_gate.start_heartbeat(
        engines.engine_factory.get_engine().session_manager,
        "external_bridge",
    )
    bootstrap_thread = threading.Thread(
        target=bootstrap_server.serve_forever,
        name="workspace-bridge-ca-bootstrap",
        daemon=True,
    )
    bootstrap_thread.start()
    log.info(
        "Started private bridge bootstrap on %s and HTTPS API on %s",
        bootstrap_server.server_address,
        private_server.server_address,
    )
    try:
        private_server.serve_forever()
    finally:
        heartbeat_stop.set()
        bootstrap_server.shutdown()
        bootstrap_server.server_close()
        private_server.server_close()
        bootstrap_thread.join()
        heartbeat_thread.join()


if __name__ == "__main__":
    main()
