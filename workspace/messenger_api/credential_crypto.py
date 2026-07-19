# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

from oslo_config import cfg
import typing
import uuid as sys_uuid

from workspace.common import external_bridge_opts
from workspace.external_bridge_control import hpke
from workspace.external_bridge_control import sql_state


def encrypt_for_active_bridge(
    session: typing.Any,
    account_uuid: sys_uuid.UUID,
    owner_user_uuid: sys_uuid.UUID,
    provider_kind: str,
    account_generation: int,
    credential: dict[str, typing.Any],
) -> tuple[dict[str, typing.Any], dict[str, typing.Any]]:
    """Encrypt for the enrolled bridge public key; no backend decrypt path exists."""
    realm_uuid = cfg.CONF[external_bridge_opts.DOMAIN].realm_uuid
    if realm_uuid is None:
        raise RuntimeError("External bridge realm UUID is not configured")
    recipient = sql_state.active_encryption_target(provider_kind, session)
    associated_data = {
        "realm_uuid": str(realm_uuid),
        "provider_kind": provider_kind,
        "bridge_instance_uuid": recipient["bridge_instance_uuid"],
        "identity_generation": recipient["identity_generation"],
        "credential_key_uuid": recipient["key_uuid"],
        "account_uuid": str(account_uuid),
        "owner_user_uuid": str(owner_user_uuid),
        "account_generation": account_generation,
        "schema": hpke.SCHEMA,
        "algorithm": hpke.ALGORITHM,
    }
    envelope = hpke.encrypt_zulip_credential(
        recipient,
        credential,
        associated_data,
    )
    return recipient, envelope
