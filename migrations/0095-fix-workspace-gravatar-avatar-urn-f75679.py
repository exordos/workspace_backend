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


class MigrationStep(migrations.AbstractMigrationStep):

    def __init__(self):
        self._depends = ["0094-mark-own-messages-read-8413a3.py"]

    @property
    def migration_id(self):
        return "f756797f-5210-47b0-b646-38ce4b4439a7"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            ALTER TABLE "m_workspace_users"
                DROP CONSTRAINT IF EXISTS
                    "m_workspace_users_avatar_urn_check";
            """
        )
        session.execute(
            """
            WITH "zulip_users" AS (
                SELECT DISTINCT ON ("user_uuid")
                    "user_uuid",
                    COALESCE(
                        NULLIF(
                            "account_settings"->'user_info'
                                ->>'delivery_email',
                            ''
                        ),
                        "account_settings"->'user_info'->>'email'
                    ) AS "email"
                FROM "m_external_accounts"
                WHERE "account_type" = 'zulip'
                  AND jsonb_typeof(
                      "account_settings"->'user_info'
                  ) = 'object'
                ORDER BY "user_uuid", "created_at", "uuid"
            )
            UPDATE "m_workspace_users" AS "users"
            SET "avatar" = 'urn:gravatar:'
                || md5(
                    lower(
                        btrim(
                            COALESCE(
                                "zulip_users"."email",
                                "users"."email",
                                "users"."uuid"::text
                            )
                        )
                    )
                )
            FROM "zulip_users"
            WHERE "users"."uuid" = "zulip_users"."user_uuid"
              AND "users"."avatar" LIKE 'urn:gavatar:%';
            """
        )
        session.execute(
            """
            UPDATE "m_workspace_users"
            SET "avatar" = 'urn:gravatar:' || md5(
                lower(
                    btrim(
                        COALESCE(
                            NULLIF("email", ''),
                            "uuid"::text
                        )
                    )
                )
            )
            WHERE "avatar" LIKE 'urn:gavatar:%';
            """
        )
        session.execute(
            """
            UPDATE "m_workspace_events" AS "events"
            SET "payload" = jsonb_set(
                "events"."payload",
                '{avatar}',
                to_jsonb("users"."avatar"),
                false
            )
            FROM "m_workspace_users" AS "users"
            WHERE "events"."payload"->>'kind' = 'user.updated'
              AND "events"."payload"->>'avatar' LIKE 'urn:gavatar:%'
              AND "events"."payload"->>'uuid' = "users"."uuid"::text;
            """
        )
        session.execute(
            """
            UPDATE "m_workspace_events"
            SET "payload" = jsonb_set(
                "payload",
                '{avatar}',
                to_jsonb(
                    'urn:gravatar:' || md5(
                        lower(
                            btrim(
                                COALESCE(
                                    NULLIF("payload"->>'email', ''),
                                    "payload"->>'uuid'
                                )
                            )
                        )
                    )
                ),
                false
            )
            WHERE "payload"->>'kind' = 'user.updated'
              AND "payload"->>'avatar' LIKE 'urn:gavatar:%';
            """
        )
        session.execute(
            """
            DO $migration$
            DECLARE
                "user_record" RECORD;
                "legacy_avatar" TEXT;
            BEGIN
                FOR "user_record" IN
                    SELECT "uuid", "avatar"
                    FROM "m_workspace_users"
                LOOP
                    "legacy_avatar" := 'urn:gavatar:'
                        || "user_record"."uuid"::text;
                    UPDATE "m_workspace_messages"
                    SET "payload" = jsonb_set(
                        "payload",
                        '{content}',
                        to_jsonb(
                            replace(
                                "payload"->>'content',
                                "legacy_avatar",
                                "user_record"."avatar"
                            )
                        ),
                        false
                    )
                    WHERE "payload"->>'content' LIKE
                        '%' || "legacy_avatar" || '%';

                    UPDATE "m_workspace_events"
                    SET "payload" = jsonb_set(
                        "payload",
                        '{payload,content}',
                        to_jsonb(
                            replace(
                                "payload"#>>'{payload,content}',
                                "legacy_avatar",
                                "user_record"."avatar"
                            )
                        ),
                        false
                    )
                    WHERE "payload"#>>'{payload,content}' LIKE
                        '%' || "legacy_avatar" || '%';
                END LOOP;
            END;
            $migration$;
            """
        )
        session.execute(
            r"""
            UPDATE "m_workspace_messages"
            SET "payload" = jsonb_set(
                "payload",
                '{content}',
                to_jsonb(
                    regexp_replace(
                        "payload"->>'content',
                        'urn:gavatar:([0-9a-f]{8})-([0-9a-f]{4})-'
                            '([0-9a-f]{4})-([0-9a-f]{4})-'
                            '([0-9a-f]{12})',
                        'urn:gravatar:\1\2\3\4\5',
                        'gi'
                    )
                ),
                false
            )
            WHERE "payload"->>'content' LIKE '%urn:gavatar:%';
            """
        )
        session.execute(
            r"""
            UPDATE "m_workspace_events"
            SET "payload" = jsonb_set(
                "payload",
                '{payload,content}',
                to_jsonb(
                    regexp_replace(
                        "payload"#>>'{payload,content}',
                        'urn:gavatar:([0-9a-f]{8})-([0-9a-f]{4})-'
                            '([0-9a-f]{4})-([0-9a-f]{4})-'
                            '([0-9a-f]{12})',
                        'urn:gravatar:\1\2\3\4\5',
                        'gi'
                    )
                ),
                false
            )
            WHERE "payload"#>>'{payload,content}' LIKE '%urn:gavatar:%';
            """
        )
        session.execute(
            """
            ALTER TABLE "m_workspace_users"
                ADD CONSTRAINT "m_workspace_users_avatar_urn_check"
                CHECK (
                    (
                        "avatar" LIKE 'urn:image:%'
                        AND length("avatar") = 46
                    )
                    OR (
                        "avatar" LIKE 'urn:gravatar:%'
                        AND length("avatar") IN (45, 77)
                    )
                    OR "avatar" LIKE 'urn:url:http://%'
                    OR "avatar" LIKE 'urn:url:https://%'
                );
            """
        )

    def downgrade(self, session):
        session.execute(
            """
            ALTER TABLE "m_workspace_users"
                DROP CONSTRAINT IF EXISTS
                    "m_workspace_users_avatar_urn_check";
            """
        )
        session.execute(
            """
            DO $migration$
            DECLARE
                "user_record" RECORD;
                "legacy_avatar" TEXT;
            BEGIN
                FOR "user_record" IN
                    SELECT "uuid", "avatar"
                    FROM "m_workspace_users"
                LOOP
                    "legacy_avatar" := 'urn:gavatar:'
                        || "user_record"."uuid"::text;
                    UPDATE "m_workspace_messages"
                    SET "payload" = jsonb_set(
                        "payload",
                        '{content}',
                        to_jsonb(
                            replace(
                                "payload"->>'content',
                                "user_record"."avatar",
                                "legacy_avatar"
                            )
                        ),
                        false
                    )
                    WHERE "payload"->>'content' LIKE
                        '%' || "user_record"."avatar" || '%';

                    UPDATE "m_workspace_events"
                    SET "payload" = jsonb_set(
                        "payload",
                        '{payload,content}',
                        to_jsonb(
                            replace(
                                "payload"#>>'{payload,content}',
                                "user_record"."avatar",
                                "legacy_avatar"
                            )
                        ),
                        false
                    )
                    WHERE "payload"#>>'{payload,content}' LIKE
                        '%' || "user_record"."avatar" || '%';
                END LOOP;
            END;
            $migration$;
            """
        )
        session.execute(
            r"""
            UPDATE "m_workspace_messages"
            SET "payload" = jsonb_set(
                "payload",
                '{content}',
                to_jsonb(
                    regexp_replace(
                        "payload"->>'content',
                        'urn:gravatar:([0-9a-f]{8})([0-9a-f]{4})'
                            '([0-9a-f]{4})([0-9a-f]{4})([0-9a-f]{12})',
                        'urn:gavatar:\1-\2-\3-\4-\5',
                        'gi'
                    )
                ),
                false
            )
            WHERE "payload"->>'content' LIKE '%urn:gravatar:%';
            """
        )
        session.execute(
            r"""
            UPDATE "m_workspace_events"
            SET "payload" = jsonb_set(
                "payload",
                '{payload,content}',
                to_jsonb(
                    regexp_replace(
                        "payload"#>>'{payload,content}',
                        'urn:gravatar:([0-9a-f]{8})([0-9a-f]{4})'
                            '([0-9a-f]{4})([0-9a-f]{4})([0-9a-f]{12})',
                        'urn:gavatar:\1-\2-\3-\4-\5',
                        'gi'
                    )
                ),
                false
            )
            WHERE "payload"#>>'{payload,content}' LIKE '%urn:gravatar:%';
            """
        )
        session.execute(
            """
            UPDATE "m_workspace_users"
            SET "avatar" = 'urn:gavatar:' || "uuid"::text
            WHERE "avatar" LIKE 'urn:gravatar:%'
               OR (
                    "avatar" LIKE 'urn:url:%'
                    AND EXISTS (
                        SELECT 1
                        FROM "m_external_accounts" AS "accounts"
                        WHERE "accounts"."user_uuid" =
                            "m_workspace_users"."uuid"
                          AND "accounts"."account_type" = 'zulip'
                    )
               );
            """
        )
        session.execute(
            """
            UPDATE "m_workspace_events" AS "events"
            SET "payload" = jsonb_set(
                "events"."payload",
                '{avatar}',
                to_jsonb("users"."avatar"),
                false
            )
            FROM "m_workspace_users" AS "users"
            WHERE "events"."payload"->>'kind' = 'user.updated'
              AND "events"."payload"->>'uuid' = "users"."uuid"::text
              AND "users"."avatar" LIKE 'urn:gavatar:%'
              AND "events"."payload"->>'avatar' <>
                    "users"."avatar";
            """
        )
        session.execute(
            """
            ALTER TABLE "m_workspace_users"
                ADD CONSTRAINT "m_workspace_users_avatar_urn_check"
                CHECK (
                    (
                        "avatar" LIKE 'urn:image:%'
                        AND length("avatar") = 46
                    )
                    OR (
                        "avatar" LIKE 'urn:gavatar:%'
                        AND length("avatar") = 48
                    )
                    OR "avatar" LIKE 'urn:url:http://%'
                    OR "avatar" LIKE 'urn:url:https://%'
                );
            """
        )


migration_step = MigrationStep()
