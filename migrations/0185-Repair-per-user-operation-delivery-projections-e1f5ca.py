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


READ_STATE_SCHEMA_LOCK_KEY = "workspace-read-state-schema-v1"


REPAIR_PER_USER_DELIVERY_PROJECTIONS = """
CREATE INDEX IF NOT EXISTS m_external_operations_v2_target_delivery_idx
ON m_external_operations_v2 (
    target_type, target_uuid, updated_at DESC, uuid DESC
)
INCLUDE (action, status)
WHERE target_uuid IS NOT NULL AND status <> 'discarded';

CREATE TEMP TABLE workspace_delivery_operations_v1 (
    uuid text PRIMARY KEY,
    action text NOT NULL,
    target_type text NOT NULL,
    target_uuid uuid,
    delivery jsonb NOT NULL,
    delivery_status varchar(32) NOT NULL,
    safe_error text,
    updated_at timestamptz NOT NULL
) ON COMMIT DROP;

INSERT INTO workspace_delivery_operations_v1 (
    uuid, action, target_type, target_uuid, delivery,
    delivery_status, safe_error, updated_at
)
SELECT operation.uuid::text,
       operation.action,
       operation.target_type,
       operation.target_uuid,
       jsonb_build_object(
           'external_operation_uuid', operation.uuid::text,
           'status', CASE
               WHEN operation.status IN ('queued', 'running') THEN 'pending'
               WHEN operation.status = 'succeeded' THEN 'delivered'
               ELSE operation.status
           END,
           'safe_error', operation.safe_error,
           'can_retry', operation.can_retry,
           'can_discard', operation.can_discard,
           'updated_at', to_char(
               operation.updated_at AT TIME ZONE 'UTC',
               'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'
           ),
           'duplicate_risk', operation.duplicate_risk,
           'retry_requires_confirmation',
               operation.retry_requires_confirmation,
           'original_url', operation.original_url,
           'reconciliation_reason', operation.reconciliation_reason
       ),
       CASE
           WHEN operation.status IN ('queued', 'running') THEN 'pending'
           WHEN operation.status = 'succeeded' THEN 'delivered'
           ELSE 'failed'
       END,
       operation.safe_error,
       operation.updated_at
FROM m_external_operations_v2 AS operation
WHERE operation.target_uuid IS NOT NULL;

CREATE INDEX workspace_delivery_operations_target_v1
ON workspace_delivery_operations_v1 (
    target_type, target_uuid, action, updated_at DESC, uuid DESC
)
INCLUDE (delivery, delivery_status, safe_error);

-- Operation deletion cascades the provider queue rows, while its public event
-- has bounded retention.  Treat a delivery pointer with no live operation as
-- stale provenance and restore the newest live target operation below.
CREATE TEMP TABLE workspace_bad_message_delivery_v1
ON COMMIT DROP AS
SELECT target.project_id, target.uuid
FROM m_workspace_messages AS target
LEFT JOIN m_external_operations_v2 AS operation
  ON operation.uuid::text =
     target.delivery_metadata->>'external_operation_uuid'
WHERE target.delivery_metadata ? 'external_operation_uuid'
  AND (
      operation.uuid IS NULL
      OR operation.action IN (
          'read_state.set',
          'stream.notification.update',
          'topic.notification.update'
      )
  );

CREATE TEMP TABLE workspace_bad_canonical_message_delivery_v1
ON COMMIT DROP AS
SELECT target.project_id, target.uuid
FROM messenger_messages AS target
LEFT JOIN m_external_operations_v2 AS operation
  ON operation.uuid::text = target.delivery->>'external_operation_uuid'
WHERE target.delivery ? 'external_operation_uuid'
  AND (
      operation.uuid IS NULL
      OR operation.action IN (
          'read_state.set',
          'stream.notification.update',
          'topic.notification.update'
      )
  );

CREATE TEMP TABLE workspace_bad_stream_delivery_v1
ON COMMIT DROP AS
SELECT target.project_id, target.uuid
FROM m_workspace_streams AS target
LEFT JOIN m_external_operations_v2 AS operation
  ON operation.uuid::text =
     target.delivery_metadata->>'external_operation_uuid'
WHERE target.delivery_metadata ? 'external_operation_uuid'
  AND (
      operation.uuid IS NULL
      OR operation.action IN (
          'read_state.set',
          'stream.notification.update',
          'topic.notification.update'
      )
  );

CREATE TEMP TABLE workspace_bad_topic_delivery_v1
ON COMMIT DROP AS
SELECT target.project_id, target.uuid
FROM m_workspace_stream_topics AS target
LEFT JOIN m_external_operations_v2 AS operation
  ON operation.uuid::text =
     target.delivery_metadata->>'external_operation_uuid'
WHERE target.delivery_metadata ? 'external_operation_uuid'
  AND (
      operation.uuid IS NULL
      OR operation.action IN (
          'read_state.set',
          'stream.notification.update',
          'topic.notification.update'
      )
  );

CREATE TEMP TABLE workspace_per_user_delivery_repair_projects_v1
ON COMMIT DROP AS
SELECT project_id FROM workspace_bad_message_delivery_v1
UNION
SELECT project_id FROM workspace_bad_canonical_message_delivery_v1
UNION
SELECT project_id FROM workspace_bad_stream_delivery_v1
UNION
SELECT project_id FROM workspace_bad_topic_delivery_v1;

WITH repair AS (
    SELECT target.project_id,
           target.uuid,
           candidate.delivery,
           candidate.delivery_status,
           candidate.safe_error,
           candidate.updated_at
    FROM workspace_bad_message_delivery_v1 AS target
    LEFT JOIN LATERAL (
        SELECT operation.delivery,
               operation.delivery_status,
               operation.safe_error,
               operation.updated_at
        FROM workspace_delivery_operations_v1 AS operation
        WHERE operation.target_type = 'message'
          AND operation.target_uuid = target.uuid
          AND operation.delivery->>'status' <> 'discarded'
          AND operation.action IN (
              'message.create',
              'message.update',
              'message.delete'
          )
        ORDER BY operation.updated_at DESC, operation.uuid DESC
        LIMIT 1
    ) AS candidate ON TRUE
)
UPDATE m_workspace_messages AS target
SET delivery_metadata = repair.delivery,
    delivery_status = repair.delivery_status,
    delivery_error = repair.safe_error,
    delivery_updated_at = repair.updated_at
FROM repair
WHERE target.project_id = repair.project_id
  AND target.uuid = repair.uuid;

WITH repair AS (
    SELECT target.project_id,
           target.uuid,
           candidate.delivery
    FROM workspace_bad_canonical_message_delivery_v1 AS target
    LEFT JOIN LATERAL (
        SELECT operation.delivery
        FROM workspace_delivery_operations_v1 AS operation
        WHERE operation.target_type = 'message'
          AND operation.delivery->>'status' <> 'discarded'
          AND operation.action IN (
              'message.create',
              'message.update',
              'message.delete'
          )
          AND (
              operation.target_uuid = target.uuid
              OR EXISTS (
                  SELECT 1
                  FROM messenger_message_placements AS placement
                  WHERE placement.project_id = target.project_id
                    AND placement.message_uuid = target.uuid
                    AND operation.target_uuid IN (
                        placement.uuid,
                        placement.legacy_public_uuid
                    )
              )
          )
        ORDER BY operation.updated_at DESC, operation.uuid DESC
        LIMIT 1
    ) AS candidate ON TRUE
)
UPDATE messenger_messages AS target
SET delivery = repair.delivery
FROM repair
WHERE target.project_id = repair.project_id
  AND target.uuid = repair.uuid;

WITH repair AS (
    SELECT target.project_id,
           target.uuid,
           candidate.delivery,
           candidate.delivery_status,
           candidate.safe_error,
           candidate.updated_at
    FROM workspace_bad_stream_delivery_v1 AS target
    LEFT JOIN LATERAL (
        SELECT operation.delivery,
               operation.delivery_status,
               operation.safe_error,
               operation.updated_at
        FROM workspace_delivery_operations_v1 AS operation
        WHERE operation.target_type = 'stream'
          AND operation.target_uuid = target.uuid
          AND operation.delivery->>'status' <> 'discarded'
          AND operation.action IN ('stream.update', 'stream.delete')
        ORDER BY operation.updated_at DESC, operation.uuid DESC
        LIMIT 1
    ) AS candidate ON TRUE
)
UPDATE m_workspace_streams AS target
SET delivery_metadata = repair.delivery,
    delivery_status = repair.delivery_status,
    delivery_error = repair.safe_error,
    delivery_updated_at = repair.updated_at
FROM repair
WHERE target.project_id = repair.project_id
  AND target.uuid = repair.uuid;

WITH repair AS (
    SELECT target.project_id,
           target.uuid,
           candidate.delivery,
           candidate.delivery_status,
           candidate.safe_error,
           candidate.updated_at
    FROM workspace_bad_topic_delivery_v1 AS target
    LEFT JOIN LATERAL (
        SELECT operation.delivery,
               operation.delivery_status,
               operation.safe_error,
               operation.updated_at
        FROM workspace_delivery_operations_v1 AS operation
        WHERE operation.target_type = 'topic'
          AND operation.target_uuid = target.uuid
          AND operation.delivery->>'status' <> 'discarded'
          AND operation.action IN (
              'topic.create',
              'topic.update',
              'topic.delete'
          )
        ORDER BY operation.updated_at DESC, operation.uuid DESC
        LIMIT 1
    ) AS candidate ON TRUE
)
UPDATE m_workspace_stream_topics AS target
SET delivery_metadata = repair.delivery,
    delivery_status = repair.delivery_status,
    delivery_error = repair.safe_error,
    delivery_updated_at = repair.updated_at
FROM repair
WHERE target.project_id = repair.project_id
  AND target.uuid = repair.uuid;

UPDATE m_workspace_event_cursors AS cursor
SET epoch_generation = gen_random_uuid(),
    pruned_through_epoch_version = GREATEST(
        cursor.pruned_through_epoch_version,
        cursor.current_epoch_version
    ),
    updated_at = NOW()
WHERE EXISTS (
    SELECT 1
    FROM workspace_per_user_delivery_repair_projects_v1 AS repair
    WHERE repair.project_id = cursor.project_id
);

CREATE OR REPLACE FUNCTION workspace_reject_per_user_target_delivery_v1()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    operation_action text;
    operation_uuid text;
BEGIN
    IF TG_TABLE_NAME = 'messenger_messages' THEN
        operation_uuid := NEW.delivery->>'external_operation_uuid';
    ELSE
        operation_uuid :=
            NEW.delivery_metadata->>'external_operation_uuid';
    END IF;
    IF operation_uuid IS NULL THEN
        RETURN NEW;
    END IF;
    SELECT operation.action
    INTO operation_action
    FROM m_external_operations_v2 AS operation
    WHERE operation.uuid = operation_uuid::uuid;
    IF operation_action IN (
        'read_state.set',
        'stream.notification.update',
        'topic.notification.update'
    ) THEN
        RETURN NULL;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS workspace_reject_per_user_message_delivery_v1
ON m_workspace_messages;
CREATE TRIGGER workspace_reject_per_user_message_delivery_v1
BEFORE UPDATE OF
    delivery_metadata, delivery_status, delivery_error, delivery_updated_at
ON m_workspace_messages
FOR EACH ROW EXECUTE FUNCTION workspace_reject_per_user_target_delivery_v1();

DROP TRIGGER IF EXISTS workspace_reject_per_user_canonical_delivery_v1
ON messenger_messages;
CREATE TRIGGER workspace_reject_per_user_canonical_delivery_v1
BEFORE UPDATE OF delivery ON messenger_messages
FOR EACH ROW EXECUTE FUNCTION workspace_reject_per_user_target_delivery_v1();

DROP TRIGGER IF EXISTS workspace_reject_per_user_stream_delivery_v1
ON m_workspace_streams;
CREATE TRIGGER workspace_reject_per_user_stream_delivery_v1
BEFORE UPDATE OF
    delivery_metadata, delivery_status, delivery_error, delivery_updated_at
ON m_workspace_streams
FOR EACH ROW EXECUTE FUNCTION workspace_reject_per_user_target_delivery_v1();

DROP TRIGGER IF EXISTS workspace_reject_per_user_topic_delivery_v1
ON m_workspace_stream_topics;
CREATE TRIGGER workspace_reject_per_user_topic_delivery_v1
BEFORE UPDATE OF
    delivery_metadata, delivery_status, delivery_error, delivery_updated_at
ON m_workspace_stream_topics
FOR EACH ROW EXECUTE FUNCTION workspace_reject_per_user_target_delivery_v1();
"""


class MigrationStep(migrations.AbstractMigrationStep):

    def __init__(self):
        self._depends = ["0184-allow-external-stream-snapshot-visibility-51c501.py"]

    @property
    def migration_id(self):
        return "e1f5ca44-b5b5-4bdc-bfdd-bf5996a34b4f"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (READ_STATE_SCHEMA_LOCK_KEY,),
        )
        session.execute(REPAIR_PER_USER_DELIVERY_PROJECTIONS)

    def downgrade(self, session):
        pass


migration_step = MigrationStep()
