# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

from restalchemy.api import applications
from restalchemy.api import middlewares
from restalchemy.api import routes
from restalchemy.api.middlewares import logging as logging_mw
from restalchemy.openapi import engines as openapi_engines
from restalchemy.openapi import structures as openapi_structures

from workspace import version as app_version
from workspace.common.api.middlewares import errors as errors_mw
from workspace.provider_api.api import routes as app_routes


class ProviderApiApp(routes.RootRoute):
    pass


ProviderApiApp.v1 = routes.route(app_routes.ProviderApiEndpointRoute)


OPENAPI_PARAMETER_NAMES = {
    "ServiceWorkspaceProviderUuid": "provider_uuid",
    "ServiceExternalAccountUuid": "external_account_uuid",
    "ServiceProviderBlobUuid": "blob_uuid",
    "ServiceMailFolderUuid": "entity_uuid",
    "ServiceMailMessageUuid": "entity_uuid",
    "ServiceCalendarUuid": "entity_uuid",
    "ServiceCalendarEventUuid": "entity_uuid",
    "ServiceProviderCommandUuid": "command_uuid",
    "ServiceCalendarProviderCommandUuid": "command_uuid",
    "ServiceMessengerUserUuid": "entity_uuid",
    "ServiceMessengerStreamUuid": "entity_uuid",
    "ServiceMessengerTopicUuid": "entity_uuid",
    "ServiceMessengerMessageUuid": "entity_uuid",
    "ServiceMessengerReactionUuid": "entity_uuid",
    "ServiceMessengerProviderCommandUuid": "command_uuid",
}


def _rewrite_openapi_parameter_names(value):
    if isinstance(value, list):
        return [_rewrite_openapi_parameter_names(item) for item in value]
    if isinstance(value, dict):
        return {
            _rewrite_openapi_parameter_names(key): (
                _rewrite_openapi_parameter_names(item)
            )
            for key, item in value.items()
        }
    if not isinstance(value, str):
        return value
    for old_name, new_name in OPENAPI_PARAMETER_NAMES.items():
        value = value.replace(old_name, new_name)
    return value


class ProviderOpenApiPaths(openapi_structures.OpenApiPaths):
    def build(self, request, components):
        return _rewrite_openapi_parameter_names(
            super().build(request, components),
        )


def get_api_application():
    return ProviderApiApp


def get_openapi_engine():
    return openapi_engines.OpenApiEngine(
        info=openapi_structures.OpenApiInfo(
            title="Workspace Provider Service v1 API",
            version=app_version.version_info,
            description="Trusted unauthenticated provider API",
        ),
        paths=ProviderOpenApiPaths(),
        components=openapi_structures.OpenApiComponents(),
    )


def build_wsgi_application():
    return middlewares.attach_middlewares(
        applications.OpenApiApplication(
            route_class=get_api_application(),
            openapi_engine=get_openapi_engine(),
        ),
        [
            errors_mw.ErrorsHandlerMiddleware,
            logging_mw.LoggingMiddleware,
        ],
    )
