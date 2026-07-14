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

"""Integration test harness for the messenger API.

The suite spins up the *real* WSGI application (real routes, controllers, ORM)
on a real HTTP server backed by a dedicated test PostgreSQL database, and runs
the schema migrations against that database before any test executes.

The only thing that is mocked is the IAM authentication middleware: instead of
verifying a JWT and calling Genesis Core IAM over HTTP, a stub middleware
installs a deterministic IAM context (``user_uuid`` / ``project_id``) taken from
request headers.  This keeps the tests hermetic while exercising everything
below the auth layer, including the composite-primary-key controllers.
"""

import os
import pathlib
import threading
import uuid as sys_uuid
import wsgiref.simple_server

import psycopg
import pytest
import requests

from gcl_iam import engines as iam_engines
from gcl_iam import middlewares as iam_mw
from restalchemy.api import applications
from restalchemy.api import middlewares
from restalchemy.api.middlewares import contexts as contexts_mw
from restalchemy.api.middlewares import logging as logging_mw
from restalchemy.storage.sql import engines
from restalchemy.storage.sql import migrations as ra_migrations

from workspace.common import file_storage_opts
from workspace.messenger_api.api import context as auth_context
from workspace.messenger_api.api import middlewares as app_middlewares
from workspace.messenger_api.dm import models as messenger_models
from workspace.workspace_api.api import app as workspace_app


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

DEFAULT_TEST_DB_URL = "postgresql://workspace:pass@localhost:5432/workspace_test"
TEST_DB_URL = os.environ.get("WORKSPACE_TEST_DB_URL", DEFAULT_TEST_DB_URL)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
MIGRATIONS_DIR = REPO_ROOT / "migrations"

# Headers used by the mocked auth middleware to build the request context.
HEADER_USER = "X-Test-User-Uuid"
HEADER_PROJECT = "X-Test-Project-Id"


# --------------------------------------------------------------------------- #
# Mocked IAM layer
# --------------------------------------------------------------------------- #


class _PolicyShouldNotBeCalledEnforcer:
    """Enforcer stub that fails if workspace controllers ask for policy."""

    def enforce(self, rule, do_raise=False, exc=None):
        raise AssertionError(f"Policy enforcement is disabled, got {rule!r}")

    def enforce_raw(self, rule, do_raise=False, exc=None):
        raise AssertionError(f"Policy enforcement is disabled, got {rule!r}")


class _FakeToken:
    otp_enabled = False

    def __init__(self, user_uuid):
        self.user_uuid = str(user_uuid)


class FakeIamEngine:
    """Drop-in replacement for ``gcl_iam.engines.IamEngine``.

    Exposes exactly the surface used by ``WorkspaceAuthContext``.
    The enforcer is intentionally hostile: workspace must not call policies.
    """

    def __init__(self, user_uuid, project_id):
        self._token = _FakeToken(user_uuid)
        self._info = {
            "project_id": str(project_id),
            "otp_verified": True,
            "otp_enabled": False,
            "permissions": [],
            "user_info": {
                "uuid": str(user_uuid),
                "name": f"user-{user_uuid}",
                "first_name": "",
                "last_name": "",
                "email": "",
            },
        }

    @property
    def token_info(self):
        return self._token

    def introspection_info(self):
        return self._info

    def get_introspection_info(self):
        return iam_engines.IntrospectionInfo(info=self._info)

    @property
    def enforcer(self):
        return _PolicyShouldNotBeCalledEnforcer()


class MockedIamAuthMiddleware(iam_mw.GenesisCoreAuthMiddleware):
    """Auth middleware that skips JWT/IAM and installs a fake IAM context.

    ``user_uuid`` / ``project_id`` are read from the request headers so a single
    running server can impersonate different users across tests.
    """

    DEFAULT_USER = "11111111-1111-1111-1111-111111111111"
    DEFAULT_PROJECT = "22222222-2222-2222-2222-222222222222"

    def __init__(self, application):
        super().__init__(
            application=application,
            iam_engine_driver=None,
            context_class=auth_context.WorkspaceAuthContext,
        )

    def _get_response(self, ctx, req):
        user_uuid = req.headers.get(HEADER_USER) or self.DEFAULT_USER
        project_id = req.headers.get(HEADER_PROJECT) or self.DEFAULT_PROJECT
        with ctx.context_manager():
            engine = FakeIamEngine(user_uuid, project_id)
            with ctx.iam_session(engine):
                req.iam_engine = engine
                # Skip GenesisCoreAuthMiddleware auth logic, just dispatch.
                return contexts_mw.ContextMiddleware._get_response(self, ctx, req)


def build_test_wsgi_application():
    """Same WSGI app + middleware stack as production, mocked auth layer only."""
    file_storage_opts.register_opts()
    application = applications.OpenApiApplication(
        route_class=workspace_app.get_api_application(),
        openapi_engine=workspace_app.get_openapi_engine(),
    )
    return middlewares.attach_middlewares(
        application,
        [
            middlewares.configure_middleware(MockedIamAuthMiddleware),
            app_middlewares.ServerSettingsMiddleware,
            iam_mw.ErrorsHandlerMiddleware,
            logging_mw.LoggingMiddleware,
        ],
    )


# --------------------------------------------------------------------------- #
# HTTP test client
# --------------------------------------------------------------------------- #


class ApiClient:
    """Thin HTTP client bound to the running test server.

    Every request carries the impersonation headers; ``user``/``project`` can be
    overridden per call to test cross-user isolation.
    """

    def __init__(self, base_url, user_uuid, project_id, user_seeder=None):
        self.base_url = base_url.rstrip("/")
        self.user_uuid = str(user_uuid)
        self.project_id = str(project_id)
        self._user_seeder = user_seeder

    def _headers(self, user=None, project=None):
        return {
            HEADER_USER: str(user or self.user_uuid),
            HEADER_PROJECT: str(project or self.project_id),
        }

    def request(self, method, path, user=None, project=None, **kwargs):
        if self._user_seeder is not None:
            self._user_seeder(str(user or self.user_uuid))
        return requests.request(
            method,
            self.base_url + path,
            headers=self._headers(user, project),
            timeout=10,
            **kwargs,
        )

    def get(self, path, **kwargs):
        return self.request("GET", path, **kwargs)

    def post(self, path, **kwargs):
        return self.request("POST", path, **kwargs)

    def put(self, path, **kwargs):
        return self.request("PUT", path, **kwargs)

    def delete(self, path, **kwargs):
        return self.request("DELETE", path, **kwargs)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


class _QuietHandler(wsgiref.simple_server.WSGIRequestHandler):
    def log_message(self, *args, **kwargs):
        pass


@pytest.fixture(scope="session")
def _database():
    """Configure the ORM engine against the test DB and migrate it to HEAD."""
    try:
        with psycopg.connect(TEST_DB_URL, connect_timeout=3):
            pass
    except Exception as exc:  # pragma: no cover - environment guard
        pytest.skip(
            "Test database is not reachable at %s (%s). Create it with: "
            "CREATE DATABASE workspace_test OWNER workspace;" % (TEST_DB_URL, exc)
        )

    with psycopg.connect(TEST_DB_URL, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA IF EXISTS \"public\" CASCADE;")
            cur.execute("CREATE SCHEMA \"public\";")

    engines.engine_factory.configure_factory(db_url=TEST_DB_URL)

    engine = ra_migrations.MigrationEngine(migrations_path=str(MIGRATIONS_DIR))
    engine.apply_migration(engine.get_latest_migration())

    yield

    engines.engine_factory.destroy_all_engines()


@pytest.fixture(scope="session")
def http_server(_database):
    """Start the real WSGI app on an ephemeral port for the whole session."""
    server = wsgiref.simple_server.make_server(
        "127.0.0.1",
        0,
        build_test_wsgi_application(),
        handler_class=_QuietHandler,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@pytest.fixture
def api(http_server, db):
    """API client scoped to a fresh, unique (user, project) pair per test."""
    user_uuid = sys_uuid.uuid4()
    return ApiClient(
        base_url=http_server,
        user_uuid=user_uuid,
        project_id=sys_uuid.uuid4(),
        user_seeder=lambda value: seed_workspace_user(
            db,
            value,
            f"user-{value}",
        ),
    )


@pytest.fixture
def db():
    """Direct DB connection for seeding rows the API only needs to read."""
    conn = psycopg.connect(TEST_DB_URL, autocommit=True)
    try:
        yield conn
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Seeding helpers
# --------------------------------------------------------------------------- #


def seed_user_stream(conn, project_id, user_uuid, name, description="seeded"):
    """Insert a source stream and the matching stream binding.

    Returns the ``uuid`` of the created ``m_workspace_user_streams`` row.
    """
    seed_workspace_user(conn, user_uuid, f"user-{user_uuid}")
    stream_uuid = sys_uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO m_workspace_streams
                (uuid, name, description, source_name, source,
                 user_uuid, project_id)
            VALUES (%s, %s, %s, 'native', '{"kind": "native"}'::jsonb, %s, %s)
            """,
            (str(stream_uuid), name, description, str(user_uuid),
             str(project_id)),
        )
        cur.execute(
            """
            INSERT INTO m_workspace_stream_bindings
                (uuid, project_id, stream_uuid, user_uuid, who_uuid, role,
                 created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, 'owner', NOW(), NOW())
            ON CONFLICT (project_id, stream_uuid, user_uuid) DO NOTHING
            """,
            (str(sys_uuid.uuid4()), str(project_id), str(stream_uuid),
             str(user_uuid), str(user_uuid)),
        )
    return str(stream_uuid)


def seed_workspace_user(conn, user_uuid, username, status="active",
                        last_ping_at=None):
    avatar = messenger_models.build_workspace_user_default_avatar(user_uuid)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO m_workspace_users
                (uuid, username, source, status, avatar, last_ping_at,
                 created_at, updated_at)
            VALUES (
                %s,
                %s,
                'iam',
                %s,
                %s,
                COALESCE(%s::timestamp with time zone, NOW()),
                NOW(),
                NOW()
            )
            ON CONFLICT (uuid) DO UPDATE
                SET username = EXCLUDED.username,
                    status = EXCLUDED.status,
                    avatar = COALESCE(
                        m_workspace_users.avatar,
                        EXCLUDED.avatar
                    ),
                    last_ping_at = EXCLUDED.last_ping_at,
                    updated_at = NOW()
            """,
            (str(user_uuid), username, status, avatar, last_ping_at),
        )


def seed_user_stream_binding(conn, project_id, stream_uuid, user_uuid,
                             role="member"):
    seed_workspace_user(conn, user_uuid, f"user-{user_uuid}")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO m_workspace_stream_bindings
                (uuid, project_id, stream_uuid, user_uuid, who_uuid, role,
                 created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, NOW(), NOW())
            ON CONFLICT (project_id, stream_uuid, user_uuid) DO NOTHING
            """,
            (str(sys_uuid.uuid4()), str(project_id), str(stream_uuid),
             str(user_uuid), str(user_uuid), role),
        )


def seed_stream_topic(conn, project_id, stream_uuid, user_uuid, name,
                      is_default=False):
    """Insert a topic and the binding needed for the user topics view."""
    topic_uuid = sys_uuid.uuid4()
    seed_user_stream_binding(conn, project_id, stream_uuid, user_uuid)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO m_workspace_stream_topics
                (uuid, project_id, name, stream_uuid, created_at, updated_at)
            VALUES (%s, %s, %s, %s, NOW(), NOW())
            """,
            (str(topic_uuid), str(project_id), name, str(stream_uuid)),
        )
        if is_default:
            cur.execute(
                """
                UPDATE m_workspace_streams
                SET default_topic_uuid = %s
                WHERE uuid = %s AND project_id = %s
                """,
                (str(topic_uuid), str(stream_uuid), str(project_id)),
            )
    return str(topic_uuid)


def seed_stream_topic_flags(conn, topic_uuid, user_uuid, project_id,
                            is_done=False):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO m_workspace_user_topic_flags
                (uuid, user_uuid, project_id, is_done, created_at, updated_at)
            VALUES (%s, %s, %s, %s, NOW(), NOW())
            ON CONFLICT (uuid, user_uuid) DO UPDATE SET
                is_done = EXCLUDED.is_done,
                updated_at = NOW()
            """,
            (str(topic_uuid), str(user_uuid), str(project_id), is_done),
        )
