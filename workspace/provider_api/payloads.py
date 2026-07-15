# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

from restalchemy.dm import filters as dm_filters

from workspace.messenger_api.dm import models as messenger_models
from workspace.provider_api.dm import models as provider_models


def _payload_value(name, value):
    if value is None:
        return None
    if name == "updated_at":
        return value.isoformat().replace("+00:00", "Z")
    if name == "uuid" or name.endswith("uuid"):
        return str(value).lower()
    return value


def add_provider_delivery_payload(payload, resource, session=None):
    provider_uuid = getattr(resource, "provider_uuid", None)
    if provider_uuid is None:
        payload["provider"] = None
        payload["delivery"] = None
        return payload

    provider = provider_models.WorkspaceProvider.objects.get_one(
        filters={"uuid": dm_filters.EQ(provider_uuid)},
        session=session,
    )
    account_uuid = getattr(resource, "external_user_uuid", None)
    if account_uuid is None:
        account_uuid = getattr(resource, "external_account_uuid", None)
    account = messenger_models.ExternalAccount.objects.get_one(
        filters={
            "uuid": dm_filters.EQ(account_uuid),
            "provider_uuid": dm_filters.EQ(provider_uuid),
        },
        session=session,
    )
    payload["provider"] = {
        "uuid": _payload_value("uuid", provider.uuid),
        "name": provider.name,
        "kind": account.account_type,
    }
    delivery_status = getattr(resource, "delivery_status", None)
    if delivery_status is None:
        payload["delivery"] = None
    else:
        payload["delivery"] = {
            "status": delivery_status,
            "safe_error": getattr(resource, "delivery_error", None),
            "updated_at": _payload_value(
                "updated_at",
                resource.delivery_updated_at,
            ),
        }
    return payload
