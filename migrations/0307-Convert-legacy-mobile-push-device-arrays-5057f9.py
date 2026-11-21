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
        self._depends = ["0306-Use-SDK-events-for-native-mobile-push-537aee.py"]

    @property
    def migration_id(self):
        return "5057f988-d781-4bd4-abf7-3cbc6a30ec5f"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        # The old handler queued a singleton array per device. Retaining the
        # original UUID is required by both SDK idempotency and HPKE AAD.
        session.execute("""
            WITH legacy AS (
                SELECT uuid, event_data->'body' AS body,
                       event_data #> '{body,event_params}' AS params,
                       event_data #> '{body,event_params,devices,0}' AS device
                FROM gcl_sdk_events
                WHERE status IN ('NEW', 'IN_PROGRESS')
                  AND event_data->>'kind' = 'WorkspacePush'
                  AND event_data #>> '{body,event_params,kind}' = 'notification_topic_update'
                  AND jsonb_typeof(event_data #> '{body,event_params,devices}') = 'array'
                  AND event_data #> '{body,event_params,devices,1}' IS NULL
                  AND event_data #> '{body,event_params,devices,0,content}' IS NOT NULL
                  AND event_data #>> '{body,_membership_generation}' IS NOT NULL
                  AND event_data #>> '{body,last_retry_at}' IS NOT NULL
            )
            UPDATE gcl_sdk_events event
            SET event_data = jsonb_set(event.event_data, '{body,event_params}',
                (legacy.params - 'devices') || jsonb_build_object(
                    'device_uuid', legacy.device->'device_uuid',
                    'registration_token', legacy.device->'registration_token',
                    'payload', jsonb_build_object(
                        'uuid', legacy.body->'uuid',
                        'kind', legacy.params->'kind',
                        'organization_uuid', legacy.params->'organization_uuid',
                        'project_uuid', legacy.params->'project_uuid',
                        'stream_uuid', legacy.params->'stream_uuid',
                        'topic_uuid', legacy.params->'topic_uuid',
                        'message_uuid', legacy.params->'message_uuid',
                        'user_uuid', legacy.body #> '{exchange,user_uuid}',
                        'device_uuid', legacy.device->'device_uuid',
                        'content', legacy.device->'content'
                    )::text
                )
            )
            FROM legacy WHERE event.uuid = legacy.uuid;

            -- Old arrays that cannot be converted must not block the sender
            -- or retain device credentials after becoming terminal.
            UPDATE gcl_sdk_events
            SET status = CASE WHEN status IN ('NEW', 'IN_PROGRESS') THEN 'ERROR' ELSE status END,
                event_data = event_data #- '{body,event_params,devices}'
            WHERE event_data->>'kind' = 'WorkspacePush'
              AND event_data #> '{body,event_params,devices}' IS NOT NULL;
        """)

    def downgrade(self, session):
        # The previous schema also uses per-device payloads. Never restore
        # expired delivery data or turn a terminal event back into a retry.
        pass


migration_step = MigrationStep()
