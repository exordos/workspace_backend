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

from gcl_iam import middlewares as iam_mw
from restalchemy.api import applications
from restalchemy.api.middlewares import logging as logging_mw
from restalchemy.api import middlewares
from restalchemy.api import routes
from restalchemy.openapi import engines as openapi_engines
from restalchemy.openapi import structures as openapi_structures

from workspace.messenger_api.api import context as auth_context
from workspace.messenger_api.api import middlewares as app_middlewares
from workspace.messenger_api.api import openapi_contract
from workspace.messenger_api.api import routes as app_routes
from workspace.messenger_api.api import versions
from workspace import version as app_version


class MessengerApiApp(routes.RootRoute):
    pass


class MessengerOpenApiComponents(openapi_structures.OpenApiComponents):
    def build(self, request):
        specification = super().build(request)
        specification = openapi_contract.add_public_projection_contract(specification)
        return openapi_contract.add_avatar_upload_schema(specification)


class MessengerOpenApiPaths(openapi_structures.OpenApiPaths):
    def build(self, request, components):
        specification = super().build(request, components)
        specification = openapi_contract.add_collection_pagination_contract(
            specification,
        )
        specification = openapi_contract.add_message_pagination_contract(
            specification,
            "/v1/messages/",
        )
        specification = openapi_contract.add_draft_contract(
            specification,
            "/v1/drafts/",
            components,
        )
        return openapi_contract.add_current_user_contract(
            specification,
            "/v1/me/",
        )


setattr(
    MessengerApiApp,
    versions.API_VERSION_1_0,
    routes.route(app_routes.ApiEndpointRoute),
)


def get_api_application():
    return MessengerApiApp


def get_openapi_engine():
    openapi_engine = openapi_engines.OpenApiEngine(
        info=openapi_structures.OpenApiInfo(
            title=f"Workspace {versions.API_VERSION_1_0} Messenger API",
            version=app_version.version_info,
            description=f"OpenAPI - Workspace {versions.API_VERSION_1_0}",
        ),
        paths=MessengerOpenApiPaths(),
        components=MessengerOpenApiComponents(),
    )
    return openapi_engine


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
                context_class=auth_context.WorkspaceMessengerAuthContext,
            ),
            app_middlewares.ServerSettingsMiddleware,
            app_middlewares.ErrorsHandlerMiddleware,
            logging_mw.LoggingMiddleware,
        ],
    )
