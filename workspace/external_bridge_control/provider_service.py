# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Transport-neutral private Provider API v1 service."""

import collections.abc
import typing
import uuid as sys_uuid

from workspace.external_bridge_control import provider_data


API_ROOT = "/api/workspace-provider/v1"


class ProviderIngressUnavailableError(provider_data.ProviderDataError):
    status = 503
    error = "provider_ingress_unavailable"


class ProviderDataService:
    def __init__(
        self,
        apply_event: collections.abc.Callable[
            [dict[str, typing.Any], typing.Any, typing.Any],
            str | sys_uuid.UUID | None,
        ]
        | None = None,
    ) -> None:
        self.apply_event = apply_event

    @staticmethod
    def matches(path: str) -> bool:
        return path == API_ROOT or path.startswith(f"{API_ROOT}/")

    def handle(
        self,
        session: typing.Any,
        identity: typing.Any,
        method: str,
        path: str,
        query: collections.abc.Mapping[str, object],
        payload: object,
    ) -> object | None:
        if not self.matches(path):
            return None
        if query:
            raise ValueError("Provider API routes do not accept query parameters")
        if not isinstance(payload, dict):
            raise TypeError("Provider API request payload must be an object")
        if method == "POST" and path == f"{API_ROOT}/operations/actions/lease":
            return provider_data.lease_provider_operations(
                session,
                identity,
                request_uuid=payload["request_uuid"],
                limit=payload.get("limit", 50),
                lease_seconds=payload.get("lease_seconds", 30),
            )
        if method == "POST" and path == f"{API_ROOT}/operation-results":
            return provider_data.report_provider_results(
                session,
                identity,
                payload["results"],
            )
        if method == "POST" and path == f"{API_ROOT}/events":
            if self.apply_event is None:
                raise ProviderIngressUnavailableError(
                    "Canonical provider event application is not enabled"
                )
            return provider_data.apply_provider_event_batch(
                session,
                identity,
                payload["events"],
                self.apply_event,
            )
        return None
