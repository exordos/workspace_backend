# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

from gcl_iam import middlewares as iam_mw
from restalchemy.api import applications
from restalchemy.api import middlewares
from restalchemy.api import routes
from restalchemy.api.middlewares import logging as logging_mw
from restalchemy.openapi import engines as openapi_engines
from restalchemy.openapi import structures as openapi_structures

from workspace import version as app_version
from workspace.messenger_api.api import context as auth_context
from workspace.messenger_api.api import middlewares as app_middlewares
from workspace.workspace_api.api import routes as app_routes


class WorkspaceApiApp(routes.RootRoute):
    pass


WorkspaceApiApp.v1 = routes.route(app_routes.WorkspaceApiEndpointRoute)


class WorkspaceOpenApiComponents(openapi_structures.OpenApiComponents):
    def build(self, request):
        specification = super().build(request)
        specification["components"]["securitySchemes"] = {
            "bearerAuth": {
                "type": "http",
                "scheme": "bearer",
                "bearerFormat": "JWT",
            },
        }
        return specification


class WorkspaceOpenApiPaths(openapi_structures.OpenApiPaths):
    def build(self, request, components):
        specification = super().build(request, components)
        for path in specification["paths"].values():
            for operation in path.values():
                if isinstance(operation, dict) and "responses" in operation:
                    operation["security"] = [{"bearerAuth": []}]
        return specification


def get_api_application():
    return WorkspaceApiApp


def get_openapi_engine():
    return openapi_engines.OpenApiEngine(
        info=openapi_structures.OpenApiInfo(
            title="Workspace v1 API",
            version=app_version.version_info,
            description="IAM-authenticated Workspace API",
        ),
        paths=WorkspaceOpenApiPaths(),
        components=WorkspaceOpenApiComponents(),
    )


def build_wsgi_application(iam_engine_driver):
    return middlewares.attach_middlewares(
        applications.OpenApiApplication(
            route_class=get_api_application(),
            openapi_engine=get_openapi_engine(),
        ),
        [
            middlewares.configure_middleware(
                iam_mw.GenesisCoreAuthMiddleware,
                iam_engine_driver=iam_engine_driver,
                context_class=auth_context.WorkspaceAuthContext,
            ),
            app_middlewares.ServerSettingsMiddleware,
            iam_mw.ErrorsHandlerMiddleware,
            logging_mw.LoggingMiddleware,
        ],
    )
