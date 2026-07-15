# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

from restalchemy.api import controllers as ra_controllers
from restalchemy.api import resources as ra_resources

from workspace.provider_api.api import controllers as provider_controllers
from workspace.provider_api.dm import models as provider_models


class WorkspaceApiEndpointController(ra_controllers.RoutesListController):
    __TARGET_PATH__ = "/v1/"


class MessengerApiEndpointController(ra_controllers.RoutesListController):
    __TARGET_PATH__ = "/v1/messenger/"


class MailApiEndpointController(ra_controllers.RoutesListController):
    __TARGET_PATH__ = "/v1/mail/"


class CalendarApiEndpointController(ra_controllers.RoutesListController):
    __TARGET_PATH__ = "/v1/calendar/"


class WorkspaceProviderCatalogController(
    ra_controllers.BaseResourceControllerPaginated,
):
    __packer__ = provider_controllers.WorkspaceProviderJSONPacker
    __resource__ = ra_resources.ResourceByRAModel(
        model_class=provider_models.WorkspaceProvider,
        hidden_fields=["registered_at", "last_seen_at"],
        convert_underscore=False,
        process_filters=True,
    )
