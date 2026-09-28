# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

from restalchemy.storage.sql import migrations


UPGRADE = """
ALTER TABLE workspace_v3.topic_bindings
    ALTER CONSTRAINT topic_bindings_stream_binding_fkey
    DEFERRABLE INITIALLY IMMEDIATE;
ALTER TABLE workspace_v3.message_flags
    ALTER CONSTRAINT message_flags_stream_binding_fkey
    DEFERRABLE INITIALLY IMMEDIATE;
ALTER TABLE workspace_v3.drafts
    ALTER CONSTRAINT drafts_stream_binding_fkey
    DEFERRABLE INITIALLY IMMEDIATE;
ALTER TABLE workspace_v3.folder_items
    ALTER CONSTRAINT folder_items_stream_binding_fkey
    DEFERRABLE INITIALLY IMMEDIATE;

CREATE FUNCTION workspace_v3.enqueue_message_delete_counter_projection()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $function$
BEGIN
    INSERT INTO workspace_v3.projection_tasks (
        project_id, task_type, scope_type, scope_uuid, user_uuid, payload
    )
    SELECT DISTINCT message.project_id, 'read_counters', 'user_stream',
           message.stream_uuid, binding.user_uuid,
           '{"emit_message_events": false}'::jsonb
    FROM old_rows AS message
    JOIN workspace_v3.stream_bindings AS binding
      ON binding.project_id = message.project_id
     AND binding.stream_uuid = message.stream_uuid
    ON CONFLICT (
        project_id, task_type, scope_type, scope_uuid, user_uuid
    ) WHERE status = 'pending'
      AND task_type = 'read_counters'
      AND payload IN (
          '{"emit_message_events": false}'::jsonb,
          '{"emit_message_event": false}'::jsonb
      ) DO NOTHING;

    INSERT INTO workspace_v3.projection_tasks (
        project_id, task_type, scope_type, scope_uuid, user_uuid, payload
    )
    SELECT DISTINCT message.project_id, 'read_counters', 'user_topic',
           message.topic_uuid, binding.user_uuid,
           '{"emit_message_events": false}'::jsonb
    FROM old_rows AS message
    JOIN workspace_v3.topic_bindings AS binding
      ON binding.project_id = message.project_id
     AND binding.topic_uuid = message.topic_uuid
    ON CONFLICT (
        project_id, task_type, scope_type, scope_uuid, user_uuid
    ) WHERE status = 'pending'
      AND task_type = 'read_counters'
      AND payload IN (
          '{"emit_message_events": false}'::jsonb,
          '{"emit_message_event": false}'::jsonb
      ) DO NOTHING;
    RETURN NULL;
END
$function$;

CREATE TRIGGER messages_counter_projection_delete
    AFTER DELETE ON workspace_v3.messages
    REFERENCING OLD TABLE AS old_rows
    FOR EACH STATEMENT
    EXECUTE FUNCTION workspace_v3.enqueue_message_delete_counter_projection();
"""

DOWNGRADE = """
DROP TRIGGER messages_counter_projection_delete ON workspace_v3.messages;
DROP FUNCTION workspace_v3.enqueue_message_delete_counter_projection();

ALTER TABLE workspace_v3.topic_bindings
    ALTER CONSTRAINT topic_bindings_stream_binding_fkey
    NOT DEFERRABLE;
ALTER TABLE workspace_v3.message_flags
    ALTER CONSTRAINT message_flags_stream_binding_fkey
    NOT DEFERRABLE;
ALTER TABLE workspace_v3.drafts
    ALTER CONSTRAINT drafts_stream_binding_fkey
    NOT DEFERRABLE;
ALTER TABLE workspace_v3.folder_items
    ALTER CONSTRAINT folder_items_stream_binding_fkey
    NOT DEFERRABLE;
"""


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self) -> None:
        self._depends = ["0195-Preserve-provider-source-content-hash-7aa861.py"]

    @property
    def migration_id(self) -> str:
        return "d38bd2eb-c18c-4f51-9fa1-4c55c9453555"

    @property
    def is_manual(self) -> bool:
        return False

    def upgrade(self, session) -> None:
        session.execute(UPGRADE)

    def downgrade(self, session) -> None:
        session.execute(DOWNGRADE)


migration_step = MigrationStep()
