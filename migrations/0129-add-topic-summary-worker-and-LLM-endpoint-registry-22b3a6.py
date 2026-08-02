# Copyright 2016 Eugene Frolov <eugene@frolov.net.ru>
#
# All Rights Reserved.
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

from restalchemy.storage.sql import migrations


USER_TOPICS_VIEW_SQL = """
CREATE OR REPLACE VIEW "m_workspace_user_topics_view" AS
SELECT
    t.uuid,
    t.name,
    t.stream_uuid,
    t.project_id,
    t.created_at,
    t.updated_at,
    (t.uuid = s.default_topic_uuid) AS is_default,
    b.user_uuid,
    COALESCE(uc.unread_count, 0) AS unread_count,
    COALESCE(f.is_done, FALSE) AS is_done,
    COALESCE(f.notification_mode, 'default') AS notification_mode,
    t.color,
    last_message.uuid AS last_message_uuid,
    t.source_name,
    t.source,
    t.summary,
    t.summary_last_message_uuid,
    CASE
        WHEN t.summary IS NULL THEN NULL
        ELSE t.summary_last_message_uuid IS DISTINCT FROM last_message.uuid
    END AS summary_has_new_messages,
    t.summary_system_prompt,
    t.summary_reasoning_effort,
    t.summary_enabled
FROM "m_workspace_stream_topics" AS t
JOIN "m_workspace_streams" AS s
    ON  s.uuid = t.stream_uuid
    AND s.project_id = t.project_id
JOIN "m_workspace_stream_bindings" AS b
    ON  b.stream_uuid = t.stream_uuid
    AND b.project_id  = t.project_id
LEFT JOIN (
    SELECT
        m.topic_uuid,
        m.user_uuid,
        m.project_id,
        COUNT(*) AS unread_count
    FROM "m_workspace_user_messages_view" AS m
    WHERE m.read = false
      AND m.topic_uuid IS NOT NULL
    GROUP BY m.topic_uuid, m.user_uuid, m.project_id
) AS uc
    ON  uc.topic_uuid = t.uuid
    AND uc.user_uuid  = b.user_uuid
    AND uc.project_id = t.project_id
LEFT JOIN LATERAL (
    SELECT m.uuid
    FROM "m_workspace_user_messages_view" AS m
    WHERE m.project_id = t.project_id
      AND m.topic_uuid = t.uuid
      AND m.user_uuid = b.user_uuid
    ORDER BY m.created_at DESC, m.uuid DESC
    LIMIT 1
) AS last_message ON TRUE
LEFT JOIN "m_workspace_user_topic_flags" AS f
    ON  f.uuid       = t.uuid
    AND f.user_uuid  = b.user_uuid
    AND f.project_id = t.project_id
LEFT JOIN "m_confirmed_external_stream_access" AS access
    ON  access.project_id = t.project_id
    AND access.user_uuid  = b.user_uuid
    AND access.stream_uuid = t.stream_uuid
WHERE s.source_name = 'native'
   OR access.user_uuid IS NOT NULL;
"""


PREVIOUS_USER_TOPICS_VIEW_SQL = USER_TOPICS_VIEW_SQL.replace(
    ",\n    t.summary_reasoning_effort,\n    t.summary_enabled\nFROM",
    "\nFROM",
)


class MigrationStep(migrations.AbstractMigrationStep):

    def __init__(self):
        self._depends = ["0128-add-topic-summary-metadata-f3cbd4.py"]

    @property
    def migration_id(self):
        return "22b3a6e2-b440-4f06-9672-d77fd80a6de7"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            ALTER TABLE "m_workspace_stream_topics"
                ADD COLUMN "summary_enabled" BOOLEAN NOT NULL DEFAULT TRUE,
                ADD COLUMN "summary_reasoning_effort" VARCHAR(16),
                ADD CONSTRAINT "m_workspace_topic_summary_reasoning_check"
                    CHECK (
                        "summary_reasoning_effort" IS NULL
                        OR "summary_reasoning_effort" IN (
                            'minimal', 'low', 'medium', 'high'
                        )
                    )
            """
        )
        session.execute(USER_TOPICS_VIEW_SQL)
        expressions = [
            """
            CREATE TABLE "m_workspace_topic_summary_global_settings" (
                "singleton" BOOLEAN PRIMARY KEY DEFAULT TRUE,
                "enabled" BOOLEAN NOT NULL DEFAULT FALSE,
                "created_at" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
                "updated_at" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
                CONSTRAINT "m_workspace_topic_summary_global_singleton_check"
                    CHECK ("singleton" = TRUE)
            )
            """,
            """
            INSERT INTO "m_workspace_topic_summary_global_settings" (
                "singleton", "enabled"
            ) VALUES (TRUE, FALSE)
            """,
            """
            CREATE TABLE "m_workspace_topic_summary_project_settings" (
                "project_id" UUID PRIMARY KEY,
                "enabled" BOOLEAN NOT NULL DEFAULT FALSE,
                "created_at" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
                "updated_at" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
            )
            """,
            """
            CREATE VIEW "m_workspace_topic_summary_settings_view" AS
            SELECT
                project_settings.project_id,
                global_settings.enabled AS global_enabled,
                project_settings.enabled AS project_enabled
            FROM "m_workspace_topic_summary_project_settings" AS project_settings
            CROSS JOIN "m_workspace_topic_summary_global_settings" AS global_settings
            WHERE global_settings.singleton = TRUE
            """,
            """
            CREATE TABLE "m_workspace_llm_endpoints" (
                "uuid" UUID PRIMARY KEY,
                "name" VARCHAR(255) NOT NULL,
                "base_url" VARCHAR(2048) NOT NULL,
                "model" VARCHAR(255) NOT NULL,
                "enabled" BOOLEAN NOT NULL DEFAULT TRUE,
                "priority" INTEGER NOT NULL DEFAULT 100,
                "supports_vision" BOOLEAN NOT NULL DEFAULT FALSE,
                "supports_reasoning" BOOLEAN NOT NULL DEFAULT FALSE,
                "temperature" DOUBLE PRECISION NOT NULL DEFAULT 0.2,
                "max_output_tokens" INTEGER NOT NULL DEFAULT 512,
                "top_p" DOUBLE PRECISION NOT NULL DEFAULT 1.0,
                "presence_penalty" DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                "frequency_penalty" DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                "credential_present" BOOLEAN NOT NULL DEFAULT FALSE,
                "claim_token" UUID,
                "claim_expires_at" TIMESTAMP WITH TIME ZONE,
                "last_success_at" TIMESTAMP WITH TIME ZONE,
                "last_failure_at" TIMESTAMP WITH TIME ZONE,
                "failure_count" INTEGER NOT NULL DEFAULT 0,
                "last_error_code" VARCHAR(128),
                "created_at" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
                "updated_at" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
                CONSTRAINT "m_workspace_llm_endpoint_priority_check"
                    CHECK ("priority" BETWEEN 0 AND 1000000),
                CONSTRAINT "m_workspace_llm_endpoint_temperature_check"
                    CHECK ("temperature" BETWEEN 0.0 AND 2.0),
                CONSTRAINT "m_workspace_llm_endpoint_max_tokens_check"
                    CHECK ("max_output_tokens" BETWEEN 1 AND 32768),
                CONSTRAINT "m_workspace_llm_endpoint_top_p_check"
                    CHECK ("top_p" BETWEEN 0.0 AND 1.0),
                CONSTRAINT "m_workspace_llm_endpoint_presence_penalty_check"
                    CHECK ("presence_penalty" BETWEEN -2.0 AND 2.0),
                CONSTRAINT "m_workspace_llm_endpoint_frequency_penalty_check"
                    CHECK ("frequency_penalty" BETWEEN -2.0 AND 2.0)
            )
            """,
            """
            CREATE INDEX "m_workspace_llm_endpoints_schedule_idx"
                ON "m_workspace_llm_endpoints" (
                    "enabled", "supports_vision", "priority", "uuid"
                )
            """,
            """
            CREATE TABLE "m_workspace_llm_endpoint_secrets" (
                "uuid" UUID PRIMARY KEY,
                "endpoint_uuid" UUID NOT NULL UNIQUE,
                "envelope" JSONB NOT NULL,
                CONSTRAINT "m_workspace_llm_endpoint_secret_endpoint_fkey"
                    FOREIGN KEY ("endpoint_uuid")
                    REFERENCES "m_workspace_llm_endpoints" ("uuid")
                    ON DELETE CASCADE
            )
            """,
            """
            CREATE TABLE "m_workspace_topic_summary_jobs" (
                "topic_uuid" UUID PRIMARY KEY,
                "project_id" UUID NOT NULL,
                "status" VARCHAR(32) NOT NULL,
                "attempt" INTEGER NOT NULL DEFAULT 0,
                "boundary_message_uuid" UUID,
                "effective_prompt" VARCHAR(16384) NOT NULL,
                "reasoning_effort" VARCHAR(16),
                "prompt_fingerprint" VARCHAR(64) NOT NULL,
                "claim_token" UUID,
                "claim_expires_at" TIMESTAMP WITH TIME ZONE,
                "endpoint_uuid" UUID,
                "endpoint_claim_token" UUID,
                "next_attempt_at" TIMESTAMP WITH TIME ZONE,
                "completed_at" TIMESTAMP WITH TIME ZONE,
                "last_error_code" VARCHAR(128),
                "created_at" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
                "updated_at" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
                CONSTRAINT "m_workspace_topic_summary_job_topic_fkey"
                    FOREIGN KEY ("topic_uuid")
                    REFERENCES "m_workspace_stream_topics" ("uuid")
                    ON DELETE CASCADE,
                CONSTRAINT "m_workspace_topic_summary_job_boundary_fkey"
                    FOREIGN KEY ("boundary_message_uuid")
                    REFERENCES "m_workspace_messages" ("uuid")
                    ON DELETE SET NULL,
                CONSTRAINT "m_workspace_topic_summary_job_endpoint_fkey"
                    FOREIGN KEY ("endpoint_uuid")
                    REFERENCES "m_workspace_llm_endpoints" ("uuid")
                    ON DELETE SET NULL,
                CONSTRAINT "m_workspace_topic_summary_job_status_check"
                    CHECK (
                        "status" IN (
                            'waiting_endpoint', 'running', 'retry_wait',
                            'succeeded', 'failed'
                        )
                    ),
                CONSTRAINT "m_workspace_topic_summary_job_attempt_check"
                    CHECK ("attempt" BETWEEN 0 AND 3),
                CONSTRAINT "m_workspace_topic_summary_job_reasoning_check"
                    CHECK (
                        "reasoning_effort" IS NULL
                        OR "reasoning_effort" IN (
                            'minimal', 'low', 'medium', 'high'
                        )
                    )
            )
            """,
            """
            CREATE INDEX "m_workspace_topic_summary_jobs_schedule_idx"
                ON "m_workspace_topic_summary_jobs" (
                    "status", "next_attempt_at", "updated_at"
                )
            """,
            """
            CREATE TABLE "m_workspace_topic_summary_journal" (
                "uuid" UUID PRIMARY KEY,
                "topic_uuid" UUID NOT NULL,
                "project_id" UUID NOT NULL,
                "summary" VARCHAR(4096) NOT NULL,
                "boundary_message_uuid" UUID NOT NULL,
                "boundary_message_created_at" TIMESTAMP WITHOUT TIME ZONE NOT NULL,
                "generated_at" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
                "invalidated_at" TIMESTAMP WITH TIME ZONE,
                CONSTRAINT "m_workspace_topic_summary_journal_topic_fkey"
                    FOREIGN KEY ("topic_uuid")
                    REFERENCES "m_workspace_stream_topics" ("uuid")
                    ON DELETE CASCADE
            )
            """,
            """
            CREATE INDEX "m_workspace_topic_summary_journal_restore_idx"
                ON "m_workspace_topic_summary_journal" (
                    "topic_uuid", "invalidated_at",
                    "boundary_message_created_at" DESC,
                    "boundary_message_uuid" DESC,
                    "generated_at" DESC,
                    "uuid" DESC
                )
            """,
        ]
        for expression in expressions:
            session.execute(expression)

    def downgrade(self, session):
        for name in (
            "m_workspace_topic_summary_journal",
            "m_workspace_topic_summary_jobs",
            "m_workspace_llm_endpoint_secrets",
            "m_workspace_llm_endpoints",
        ):
            self._delete_table_if_exists(session, name)
        session.execute(
            'DROP VIEW IF EXISTS "m_workspace_topic_summary_settings_view"'
        )
        for name in (
            "m_workspace_topic_summary_project_settings",
            "m_workspace_topic_summary_global_settings",
        ):
            self._delete_table_if_exists(session, name)
        session.execute('DROP VIEW IF EXISTS "m_workspace_user_topics_view"')
        session.execute(PREVIOUS_USER_TOPICS_VIEW_SQL)
        session.execute(
            """
            ALTER TABLE "m_workspace_stream_topics"
                DROP CONSTRAINT IF EXISTS
                    "m_workspace_topic_summary_reasoning_check",
                DROP COLUMN IF EXISTS "summary_reasoning_effort",
                DROP COLUMN IF EXISTS "summary_enabled"
            """
        )


migration_step = MigrationStep()
