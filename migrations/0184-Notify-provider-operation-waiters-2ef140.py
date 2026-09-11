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


FUNCTION_NAME = "workspace_notify_provider_operations_v1"
OPERATION_TRIGGER_NAME = "workspace_provider_operations_notify_v1"
SNAPSHOT_TRIGGER_NAME = "workspace_provider_read_snapshots_notify_v1"
POLICY_TRIGGER_NAME = "workspace_provider_policy_notify_v1"
BRIDGE_TRIGGER_NAME = "workspace_provider_bridge_notify_v1"
NOTIFY_CHANNEL_PREFIX = "workspace_provider_ops_"

CREATE_FUNCTION = f"""
    CREATE FUNCTION {FUNCTION_NAME}()
    RETURNS trigger
    LANGUAGE plpgsql
    AS $function$
    DECLARE
        bridge_uuid uuid;
        notify_channel text;
    BEGIN
        IF TG_TABLE_NAME = 'm_external_provider_operations_v1' THEN
            IF TG_OP = 'DELETE' THEN
                RETURN NULL;
            END IF;
            IF NEW.status <> 'queued' THEN
                RETURN NULL;
            END IF;
        ELSIF TG_TABLE_NAME = 'm_external_provider_policies_v1' THEN
            IF TG_OP = 'UPDATE'
               AND NEW.enabled IS NOT DISTINCT FROM OLD.enabled
               AND NEW.emergency_suspended IS NOT DISTINCT FROM
                   OLD.emergency_suspended THEN
                RETURN NULL;
            END IF;
            FOR bridge_uuid IN
                SELECT bridge.uuid
                FROM m_external_bridge_instances_v2 AS bridge
                WHERE bridge.provider = NEW.provider
            LOOP
                notify_channel := '{NOTIFY_CHANNEL_PREFIX}'
                    || replace(bridge_uuid::text, '-', '');
                PERFORM pg_notify(notify_channel, '');
            END LOOP;
            RETURN NULL;
        ELSIF TG_TABLE_NAME = 'm_external_bridge_instances_v2' THEN
            IF TG_OP = 'UPDATE'
               AND NEW.status IS NOT DISTINCT FROM OLD.status
               AND NEW.capabilities IS NOT DISTINCT FROM OLD.capabilities
               AND OLD.last_heartbeat_at IS NOT NULL
               AND OLD.last_heartbeat_at >=
                   statement_timestamp() - interval '60 seconds' THEN
                RETURN NULL;
            END IF;
            bridge_uuid := NEW.uuid;
        END IF;
        IF TG_TABLE_NAME <> 'm_external_bridge_instances_v2' THEN
            IF TG_OP = 'DELETE' THEN
                bridge_uuid := OLD.bridge_instance_uuid;
            ELSE
                bridge_uuid := NEW.bridge_instance_uuid;
            END IF;
        END IF;
        notify_channel := '{NOTIFY_CHANNEL_PREFIX}'
            || replace(bridge_uuid::text, '-', '');
        PERFORM pg_notify(notify_channel, '');
        RETURN NULL;
    END
    $function$
"""

CREATE_OPERATION_TRIGGER = f"""
    CREATE TRIGGER {OPERATION_TRIGGER_NAME}
    AFTER INSERT OR UPDATE OF status, available_at, lease_expires_at
        ON m_external_provider_operations_v1
    FOR EACH ROW
    EXECUTE FUNCTION {FUNCTION_NAME}()
"""

CREATE_SNAPSHOT_TRIGGER = f"""
    CREATE TRIGGER {SNAPSHOT_TRIGGER_NAME}
    AFTER INSERT OR DELETE ON m_external_provider_read_snapshots_v1
    FOR EACH ROW
    EXECUTE FUNCTION {FUNCTION_NAME}()
"""

CREATE_POLICY_TRIGGER = f"""
    CREATE TRIGGER {POLICY_TRIGGER_NAME}
    AFTER INSERT OR UPDATE OF enabled, emergency_suspended
        ON m_external_provider_policies_v1
    FOR EACH ROW
    EXECUTE FUNCTION {FUNCTION_NAME}()
"""

CREATE_BRIDGE_TRIGGER = f"""
    CREATE TRIGGER {BRIDGE_TRIGGER_NAME}
    AFTER INSERT OR UPDATE OF status, capabilities, last_heartbeat_at
        ON m_external_bridge_instances_v2
    FOR EACH ROW
    EXECUTE FUNCTION {FUNCTION_NAME}()
"""


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = ["0183-Backfill-Messenger-projection-task-gaps-bf0cd6.py"]

    @property
    def migration_id(self):
        return "2ef1402b-5a8a-4203-a5d6-63d2c27f857e"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(CREATE_FUNCTION, ())
        session.execute(CREATE_OPERATION_TRIGGER, ())
        session.execute(CREATE_SNAPSHOT_TRIGGER, ())
        session.execute(CREATE_POLICY_TRIGGER, ())
        session.execute(CREATE_BRIDGE_TRIGGER, ())

    def downgrade(self, session):
        session.execute(
            f"""
            DROP TRIGGER IF EXISTS {BRIDGE_TRIGGER_NAME}
            ON m_external_bridge_instances_v2
            """,
            (),
        )
        session.execute(
            f"""
            DROP TRIGGER IF EXISTS {POLICY_TRIGGER_NAME}
            ON m_external_provider_policies_v1
            """,
            (),
        )
        session.execute(
            f"""
            DROP TRIGGER IF EXISTS {SNAPSHOT_TRIGGER_NAME}
            ON m_external_provider_read_snapshots_v1
            """,
            (),
        )
        session.execute(
            f"""
            DROP TRIGGER IF EXISTS {OPERATION_TRIGGER_NAME}
            ON m_external_provider_operations_v1
            """,
            (),
        )
        session.execute(f"DROP FUNCTION IF EXISTS {FUNCTION_NAME}()", ())


migration_step = MigrationStep()
