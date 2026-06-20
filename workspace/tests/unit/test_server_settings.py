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

import json
import unittest
from unittest import mock

import webob

from workspace.messanger_api.api import middlewares


class ServerSettingsMiddlewareTest(unittest.TestCase):
    def _make_app(self):
        return middlewares.ServerSettingsMiddleware(
            application=mock.Mock(side_effect=AssertionError("unexpected app call")),
        )

    def test_returns_zulip_like_server_settings(self):
        req = webob.Request.blank(
            "/v1/server_settings",
            base_url="http://127.0.0.1:3000",
        )

        response = req.get_response(self._make_app())
        result = json.loads(response.text)

        self.assertEqual(response.status_int, 200)
        self.assertEqual(response.content_type, "application/json")
        self.assertEqual(result["result"], "success")
        self.assertEqual(result["msg"], "")
        self.assertEqual(result["realm_url"], "http://127.0.0.1:3000")
        self.assertEqual(result["realm_uri"], "http://127.0.0.1:3000")
        self.assertTrue(result["authentication_methods"]["password"])
        self.assertEqual(result["external_authentication_methods"], [])
        self.assertFalse(result["is_incompatible"])

    def test_reports_unsupported_parameters(self):
        req = webob.Request.blank(
            "/v1/server_settings?foo=1&bar=2",
            base_url="http://127.0.0.1:3000",
        )

        response = req.get_response(self._make_app())
        result = json.loads(response.text)

        self.assertEqual(result["ignored_parameters_unsupported"], ["bar", "foo"])

    def test_trailing_slash_is_supported(self):
        req = webob.Request.blank(
            "/v1/server_settings/",
            base_url="http://127.0.0.1:3000",
        )

        response = req.get_response(self._make_app())

        self.assertEqual(response.status_int, 200)

    def test_non_matching_request_passes_through(self):
        expected_response = webob.Response(status=204)

        def downstream_app(environ, start_response):
            return expected_response(environ, start_response)

        downstream = mock.Mock(side_effect=downstream_app)
        middleware = middlewares.ServerSettingsMiddleware(application=downstream)
        req = webob.Request.blank(
            "/v1/folders",
            base_url="http://127.0.0.1:3000",
        )

        response = req.get_response(middleware)

        self.assertEqual(response.status_int, 204)
        downstream.assert_called_once()
