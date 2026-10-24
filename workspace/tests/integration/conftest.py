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

import contextlib
import datetime
import gc
import hashlib
import importlib.util
import json
import os
import pathlib
import threading
import urllib.parse
import uuid as sys_uuid
import wsgiref.simple_server

import psycopg
from psycopg import sql
import pytest
import requests

from gcl_iam import engines as iam_engines
from gcl_iam import middlewares as iam_mw
from restalchemy.common import contexts
from restalchemy.api import applications
from restalchemy.api import middlewares
from restalchemy.api.middlewares import contexts as contexts_mw
from restalchemy.api.middlewares import logging as logging_mw
from restalchemy.storage.sql import engines
from restalchemy.storage.sql import migrations as ra_migrations
from oslo_config import cfg

from workspace.common import file_storage_opts
from workspace.common import messenger_reaction_opts
from workspace.common import topic_summary_opts
from workspace.bridges.zulip.dm import models as zulip_models
from workspace.messenger_api.api import context as auth_context
from workspace.messenger_api.api import middlewares as app_middlewares
from workspace.messenger_api.api import store_factory
from workspace.messenger_api.api import store as api_store
from workspace.messenger_api.dm import models as messenger_models
from workspace.workspace_api.api import app as workspace_app


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

DEFAULT_TEST_DB_URL = "postgresql://workspace:pass@localhost:5432/workspace_test"
TEST_DB_URL = os.environ.get("WORKSPACE_TEST_DB_URL", DEFAULT_TEST_DB_URL)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
MIGRATIONS_DIR = REPO_ROOT / "migrations"


class DataMigration:
    """Exercise one data/trigger migration without rolling back unrelated schema."""

    def __init__(self, migration):
        self.migration = migration
        self.applied = True

    def _run(self, filename, operation):
        spec = importlib.util.spec_from_file_location(
            "tested_data_migration", MIGRATIONS_DIR / filename
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with contexts.Context().session_manager() as session:
            getattr(module.migration_step, operation)(session)

    def apply_migration(self, filename):
        if not self.applied:
            self._run(filename, "upgrade")
            self.applied = True

    def rollback_migration(self, filename):
        if self.applied:
            self._run(filename, "downgrade")
            self.applied = False

    def get_latest_migration(self):
        return self.migration


# Headers used by the mocked auth middleware to build the request context.
HEADER_USER = "X-Test-User-Uuid"
HEADER_PROJECT = "X-Test-Project-Id"
HEADER_PERMISSIONS = "X-Test-Permissions"


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

    def __init__(self, user_uuid, project_id, permissions=()):
        self._token = _FakeToken(user_uuid)
        self._info = {
            "project_id": str(project_id),
            "otp_verified": True,
            "otp_enabled": False,
            "permissions": list(permissions),
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
            context_class=auth_context.WorkspaceMessengerAuthContext,
        )

    def _get_response(self, ctx, req):
        user_uuid = req.headers.get(HEADER_USER) or self.DEFAULT_USER
        project_id = req.headers.get(HEADER_PROJECT) or self.DEFAULT_PROJECT
        permissions = tuple(
            value
            for value in (req.headers.get(HEADER_PERMISSIONS) or "").split(",")
            if value
        )
        with ctx.context_manager():
            engine = FakeIamEngine(user_uuid, project_id, permissions)
            with ctx.iam_session(engine):
                req.iam_engine = engine
                # Skip GenesisCoreAuthMiddleware auth logic, just dispatch.
                return contexts_mw.ContextMiddleware._get_response(self, ctx, req)


def build_test_wsgi_application():
    """Same WSGI app + middleware stack as production, mocked auth layer only."""
    file_storage_opts.register_opts()
    try:
        messenger_reaction_opts.register_opts()
    except cfg.DuplicateOptError:
        pass
    try:
        topic_summary_opts.register_opts()
    except cfg.DuplicateOptError:
        pass
    cfg.CONF.set_override(
        "secret_encryption_key",
        "integration-test-topic-summary-key",
        group=topic_summary_opts.DOMAIN,
    )
    application = applications.OpenApiApplication(
        route_class=workspace_app.get_api_application(),
        openapi_engine=workspace_app.get_openapi_engine(),
    )
    return middlewares.attach_middlewares(
        application,
        [
            middlewares.configure_middleware(MockedIamAuthMiddleware),
            app_middlewares.ServerSettingsMiddleware,
            app_middlewares.MutationResponseMiddleware,
            app_middlewares.DatabaseDeadlockRetryMiddleware,
            app_middlewares.ErrorsHandlerMiddleware,
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
        self._seeded_users = set()

    def _headers(self, user=None, project=None, permissions=()):
        headers = {
            HEADER_USER: str(user or self.user_uuid),
            HEADER_PROJECT: str(project or self.project_id),
        }
        if permissions:
            headers[HEADER_PERMISSIONS] = ",".join(permissions)
        return headers

    def request(
        self,
        method,
        path,
        user=None,
        project=None,
        permissions=(),
        **kwargs,
    ):
        user_uuid = str(user or self.user_uuid)
        if self._user_seeder is not None and user_uuid not in self._seeded_users:
            self._user_seeder(user_uuid)
            self._seeded_users.add(user_uuid)
        headers = self._headers(user, project, permissions)
        headers.update(kwargs.pop("headers", {}))
        return requests.request(
            method,
            self.base_url + path,
            headers=headers,
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
        reason = (
            "Test database is not reachable (%s). Create a dedicated workspace_test database."
            % exc
        )
        if os.environ.get("WORKSPACE_REQUIRE_INTEGRATION_TESTS") == "1":
            pytest.fail(reason)
        pytest.skip(reason)

    with psycopg.connect(TEST_DB_URL, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute('DROP SCHEMA IF EXISTS "public" CASCADE;')
            cur.execute('CREATE SCHEMA "public";')

    engines.engine_factory.configure_factory(db_url=TEST_DB_URL)

    engine = ra_migrations.MigrationEngine(migrations_path=str(MIGRATIONS_DIR))
    engine.apply_migration(engine.get_latest_migration())
    api_store.configure_store_factory(store_factory.build_store_factory())

    yield

    api_store.reset_store_factory()
    engines.engine_factory.destroy_all_engines()


@contextlib.contextmanager
def isolated_migration_database(migration, *, current_models=False):
    """Own a disposable database for tests of a published schema revision."""
    database_name = "workspace_migration_" + sys_uuid.uuid4().hex
    database_url = (
        urllib.parse.urlsplit(TEST_DB_URL)._replace(path="/" + database_name).geturl()
    )
    with psycopg.connect(TEST_DB_URL, autocommit=True) as admin:
        admin.execute(
            sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name))
        )
    try:
        engines.engine_factory.destroy_all_engines()
        engines.engine_factory.configure_factory(db_url=database_url)
        engine = ra_migrations.MigrationEngine(migrations_path=str(MIGRATIONS_DIR))
        engine.apply_migration(migration)
        if current_models:
            # These fields are independent of the historical migration under
            # test. Keep current ORM fixtures usable without applying 0231,
            # which would retire the compatibility tables being tested.
            with psycopg.connect(database_url, autocommit=True) as connection:
                connection.execute("""
                    ALTER TABLE m_external_accounts_v2 ADD COLUMN IF NOT EXISTS
                        has_completed_backfill BOOLEAN NOT NULL DEFAULT FALSE;
                    ALTER TABLE bridge_zulip_v5_tasks
                        ADD COLUMN IF NOT EXISTS queue VARCHAR(16) NOT NULL DEFAULT 'background',
                        ADD COLUMN IF NOT EXISTS concurrency_key TEXT NOT NULL DEFAULT '',
                        ADD COLUMN IF NOT EXISTS backfill_uuid UUID,
                        ADD COLUMN IF NOT EXISTS realtime_event_uuid UUID,
                        ADD COLUMN IF NOT EXISTS execution_time DOUBLE PRECISION,
                        ADD COLUMN IF NOT EXISTS queue_wait_time DOUBLE PRECISION,
                        ADD COLUMN IF NOT EXISTS message_created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT 'epoch',
                        ADD COLUMN IF NOT EXISTS message_sequence BIGINT NOT NULL DEFAULT 0;
                    ALTER TABLE bridge_zulip_v5_message_contents
                        ADD COLUMN IF NOT EXISTS conversion_version INTEGER NOT NULL DEFAULT 0,
                        ADD COLUMN IF NOT EXISTS source_timestamp BIGINT NOT NULL DEFAULT 0,
                        ADD COLUMN IF NOT EXISTS source_message_id BIGINT NOT NULL DEFAULT 0;
                    ALTER TABLE IF EXISTS messenger_message_changes ADD COLUMN IF NOT EXISTS
                        message_created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT 'epoch',
                        ADD COLUMN IF NOT EXISTS message_sequence BIGINT NOT NULL DEFAULT 0;
                    ALTER TABLE IF EXISTS bridge_zulip_v5_message_changes ADD COLUMN IF NOT EXISTS
                        message_created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT 'epoch',
                        ADD COLUMN IF NOT EXISTS message_sequence BIGINT NOT NULL DEFAULT 0;
                    ALTER TABLE messenger_user_message_states
                        ADD COLUMN IF NOT EXISTS read_updated_at TIMESTAMP WITHOUT TIME ZONE,
                        ADD COLUMN IF NOT EXISTS starred_updated_at TIMESTAMP WITHOUT TIME ZONE;
                """)
        yield database_url
    finally:
        engines.engine_factory.destroy_all_engines()
        # Historical-schema tests create many short-lived pools. Restalchemy
        # closes session/engine reference cycles when they are collected.
        gc.collect()
        engines.engine_factory.configure_factory(db_url=TEST_DB_URL)
        with psycopg.connect(TEST_DB_URL, autocommit=True) as admin:
            admin.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(
                    sql.Identifier(database_name)
                )
            )


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
def workspace_api(http_server, db):
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


def pytest_addoption(parser):
    parser.addoption(
        "--integration-shard",
        nargs=2,
        type=int,
        metavar=("INDEX", "COUNT"),
        help="Run one zero-based integration shard with its own database.",
    )


def pytest_configure(config):
    shard = config.getoption("integration_shard")
    if shard is not None and not 0 <= shard[0] < shard[1]:
        raise pytest.UsageError("--integration-shard requires 0 <= INDEX < COUNT")
    config.addinivalue_line(
        "markers",
        "pre_outgoing_schema: test published outgoing migrations before their removal",
    )
    config.addinivalue_line(
        "markers",
        "pre_retirement_schema: verify a published migration before compatibility retirement",
    )
    config.addinivalue_line(
        "markers",
        "isolated_schema: keep destructive migration tests off the shared schema",
    )


def pytest_collection_modifyitems(config, items):
    shard = config.getoption("integration_shard")
    if shard is None:
        return
    index, count = shard
    selected, deselected = [], []
    for item in items:
        # Keep module-scoped migration fixtures and their test order intact.
        module = item.nodeid.split("::", 1)[0]
        assigned = int.from_bytes(hashlib.sha256(module.encode()).digest(), "big")
        (selected if assigned % count == index else deselected).append(item)
    items[:] = selected
    config.hook.pytest_deselected(items=deselected)


@pytest.fixture(autouse=True)
def _test_database_url(_database, request, monkeypatch):
    if request.node.get_closest_marker("pre_outgoing_schema") is not None:
        with isolated_migration_database(
            "0264-Add-synchronization-status-to-provider-entities-ca9efd.py"
        ) as database_url:
            path = next(MIGRATIONS_DIR.glob("0265-*.py"))
            spec = importlib.util.spec_from_file_location("outgoing_test_schema", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            # Current incoming code reads intentions; the historical migration
            # under test still owns the old outbound tables and triggers.
            with psycopg.connect(database_url, autocommit=True) as connection:
                connection.execute(module.SCHEMA)
            yield database_url
        return
    if request.node.get_closest_marker("isolated_schema") is not None:
        engine = ra_migrations.MigrationEngine(migrations_path=str(MIGRATIONS_DIR))
        with isolated_migration_database(engine.get_latest_migration()) as database_url:
            yield database_url
        return
    if request.node.get_closest_marker("pre_retirement_schema") is None:
        with psycopg.connect(TEST_DB_URL, autocommit=True) as connection:
            # Permanent tombstones intentionally survive membership cleanup.
            # Do not carry one scenario's deletion history into unrelated tests
            # that roll the entire schema back to a historical revision.
            if connection.execute(
                "SELECT to_regclass('messenger_message_delete_tombstones')"
            ).fetchone()[0]:
                connection.execute("DELETE FROM messenger_message_delete_tombstones")
            if connection.execute(
                "SELECT to_regclass('messenger_domain_outbox_events')"
            ).fetchone()[0]:
                connection.execute("DELETE FROM messenger_domain_outbox_events")
                connection.execute("DELETE FROM messenger_projection_scope_leases")
        api_store.configure_store_factory(store_factory.build_store_factory())
        try:
            yield TEST_DB_URL
        finally:
            api_store.configure_store_factory(store_factory.build_store_factory())
        return
    baseline = "0230-Avoid-redundant-project-user-writes-and-bound-Zulip-reconciliation-batches-fc01aa.py"
    from workspace.tests.integration import legacy_store

    apply = ra_migrations.MigrationEngine.apply_migration

    def apply_with_current_queue_metadata(engine, *args, **kwargs):
        result = apply(engine, *args, **kwargs)
        # Historical rollbacks can recreate these tables. Keep the current
        # queue models usable without applying the compatibility retirement.
        with psycopg.connect(TEST_DB_URL, autocommit=True) as connection:
            connection.execute("""ALTER TABLE bridge_zulip_v5_tasks
                ADD COLUMN IF NOT EXISTS queue VARCHAR(16) NOT NULL DEFAULT 'background',
                ADD COLUMN IF NOT EXISTS concurrency_key TEXT NOT NULL DEFAULT ''""")
            for table in (
                "messenger_message_changes",
                "bridge_zulip_v5_message_changes",
            ):
                connection.execute(
                    f"ALTER TABLE IF EXISTS {table} ADD COLUMN IF NOT EXISTS message_created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT 'epoch', ADD COLUMN IF NOT EXISTS message_sequence BIGINT NOT NULL DEFAULT 0"
                )
        return result

    if hasattr(request.module, "_historical_database"):
        api_store.configure_store_factory(legacy_store.LegacyMessengerStoreFactory())
        with monkeypatch.context() as patch:
            patch.setattr(
                ra_migrations.MigrationEngine,
                "apply_migration",
                apply_with_current_queue_metadata,
            )
            patch.setattr(
                ra_migrations.MigrationEngine,
                "get_latest_migration",
                lambda self: baseline,
            )
            yield TEST_DB_URL
        api_store.configure_store_factory(store_factory.build_store_factory())
        return
    with isolated_migration_database(baseline, current_models=True) as database_url:
        api_store.configure_store_factory(legacy_store.LegacyMessengerStoreFactory())
        with monkeypatch.context() as patch:
            patch.setattr(
                ra_migrations.MigrationEngine,
                "apply_migration",
                apply_with_current_queue_metadata,
            )
            patch.setitem(globals(), "TEST_DB_URL", database_url)
            patch.setattr(
                ra_migrations.MigrationEngine,
                "get_latest_migration",
                lambda self: baseline,
            )
            try:
                yield database_url
            finally:
                api_store.configure_store_factory(store_factory.build_store_factory())


@pytest.fixture
def db(_test_database_url):
    """Direct DB connection for seeding rows the API only needs to read."""
    conn = psycopg.connect(_test_database_url, autocommit=True)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def scheduled_reconciliation(_database):
    """Keep reconciliation waiting while exercising account-bound queue work."""
    task = zulip_models.Task(
        priority=zulip_models.TASK_PRIORITY_RECONCILIATION,
        not_before_at=datetime.datetime.now(datetime.timezone.utc)
        + datetime.timedelta(days=1),
        content=zulip_models.ReconciliationContent(),
    )
    task.insert()
    counters = zulip_models.Task(
        priority=zulip_models.TASK_PRIORITY_REFRESH_MESSAGE_COUNTERS,
        not_before_at=datetime.datetime.now(datetime.timezone.utc)
        + datetime.timedelta(days=1),
        content=zulip_models.RefreshMessageCountersContent(),
    )
    counters.insert()
    yield task
    counters.delete()
    task.delete()


# --------------------------------------------------------------------------- #
# Seeding helpers
# --------------------------------------------------------------------------- #


def seed_user_stream(conn, project_id, user_uuid, name, description="seeded"):
    """Insert a source stream and the matching stream binding.

    Returns the ``uuid`` of the created ``m_workspace_user_streams`` row.
    """
    seed_workspace_user(conn, user_uuid, f"user-{user_uuid}")
    stream_uuid = sys_uuid.uuid4()
    if not conn.execute("SELECT to_regclass('m_workspace_streams')").fetchone()[0]:
        conn.execute(
            "SELECT messenger_v2_register_project_user(%s,%s)", (project_id, user_uuid)
        )
        conn.execute(
            "INSERT INTO messenger_streams (uuid,project_id,owner_uuid,name,description) VALUES (%s,%s,%s,%s,%s)",
            (stream_uuid, project_id, user_uuid, name, description),
        )
        seed_user_stream_binding(conn, project_id, stream_uuid, user_uuid, role="owner")
        return str(stream_uuid)
    binding_uuid = sys_uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO m_workspace_streams
                (uuid, name, description, source_name, source,
                 user_uuid, project_id)
            VALUES (%s, %s, %s, 'native', '{"kind": "native"}'::jsonb, %s, %s)
            """,
            (str(stream_uuid), name, description, str(user_uuid), str(project_id)),
        )
        cur.execute(
            """
            INSERT INTO m_workspace_stream_bindings
                (uuid, project_id, stream_uuid, user_uuid, who_uuid, role,
                 created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, 'owner', NOW(), NOW())
            ON CONFLICT (project_id, stream_uuid, user_uuid) DO NOTHING
            """,
            (
                str(binding_uuid),
                str(project_id),
                str(stream_uuid),
                str(user_uuid),
                str(user_uuid),
            ),
        )
    return str(stream_uuid)


def seed_legacy_external_account(conn, account):
    """Seed external-account schemas published before provider identity support."""
    conn.execute(
        """
        INSERT INTO m_external_accounts_v2
            (uuid, project_id, owner_user_uuid, provider, settings, status,
             error_description, created_at, updated_at)
        VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, NOW(), NOW())
        """,
        (
            account.uuid,
            account.project_id,
            account.owner_user_uuid,
            account.provider,
            json.dumps(account.settings),
            account.status,
            account.error_description,
        ),
    )


def seed_legacy_zulip_channel(conn, channel):
    """Seed channel schemas published before channel types were introduced."""
    conn.execute(
        """
        INSERT INTO bridge_zulip_v5_channels
            (uuid, external_account_uuid, stream_uuid, stream_id, name, description,
             rendered_description, date_created, creator_id, invite_only,
             is_web_public, history_public_to_subscribers, is_archived,
             message_retention_days)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            channel.uuid,
            channel.external_account_uuid,
            channel.stream_uuid,
            channel.stream_id,
            channel.name,
            channel.description,
            channel.rendered_description,
            channel.date_created,
            channel.creator_id,
            channel.invite_only,
            channel.is_web_public,
            channel.history_public_to_subscribers,
            channel.is_archived,
            channel.message_retention_days,
        ),
    )


def seed_workspace_user(conn, user_uuid, username, status="active", last_ping_at=None):
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


def seed_user_stream_binding(conn, project_id, stream_uuid, user_uuid, role="member"):
    seed_workspace_user(conn, user_uuid, f"user-{user_uuid}")
    binding_uuid = sys_uuid.uuid4()
    table = (
        "m_workspace_stream_bindings"
        if conn.execute("SELECT to_regclass('m_workspace_stream_bindings')").fetchone()[
            0
        ]
        else "messenger_stream_bindings"
    )
    if table == "messenger_stream_bindings":
        conn.execute(
            "SELECT messenger_v2_register_project_user(%s,%s)", (project_id, user_uuid)
        )
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {table}
                (uuid, project_id, stream_uuid, user_uuid, who_uuid, role,
                 created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, NOW(), NOW())
            ON CONFLICT (project_id, stream_uuid, user_uuid) DO NOTHING
            """,
            (
                str(binding_uuid),
                str(project_id),
                str(stream_uuid),
                str(user_uuid),
                str(user_uuid),
                role,
            ),
        )


def seed_stream_topic(conn, project_id, stream_uuid, user_uuid, name, is_default=False):
    """Insert a topic and the binding needed for the user topics view."""
    topic_uuid = sys_uuid.uuid4()
    seed_user_stream_binding(conn, project_id, stream_uuid, user_uuid)
    if not conn.execute("SELECT to_regclass('m_workspace_stream_topics')").fetchone()[
        0
    ]:
        conn.execute(
            "INSERT INTO messenger_topics (uuid,project_id,stream_uuid,name) VALUES (%s,%s,%s,%s)",
            (topic_uuid, project_id, stream_uuid, name),
        )
        conn.execute(
            "INSERT INTO messenger_user_topic_bindings (uuid,project_id,user_uuid,topic_uuid,notification_mode) "
            "SELECT messenger_uuid_v5(%s,user_uuid::text),project_id,user_uuid,%s,'default' "
            "FROM messenger_stream_bindings WHERE project_id=%s AND stream_uuid=%s AND active "
            "ON CONFLICT (project_id,user_uuid,topic_uuid) DO NOTHING",
            (topic_uuid, topic_uuid, project_id, stream_uuid),
        )
        if is_default:
            conn.execute(
                "UPDATE messenger_streams SET default_topic_uuid=%s WHERE project_id=%s AND uuid=%s",
                (topic_uuid, project_id, stream_uuid),
            )
        return str(topic_uuid)
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


def seed_stream_topic_flags(conn, topic_uuid, user_uuid, project_id, is_done=False):
    if not conn.execute(
        "SELECT to_regclass('m_workspace_user_topic_flags')"
    ).fetchone()[0]:
        conn.execute(
            "UPDATE messenger_topics SET is_done=%s WHERE project_id=%s AND uuid=%s",
            (is_done, project_id, topic_uuid),
        )
        return
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
