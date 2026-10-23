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


UPGRADE_SQL = r"""
-- Decode selected quote text and forward snapshots without indexing ciphertext.
CREATE FUNCTION messenger_reference_query_text(value text)
RETURNS text LANGUAGE plpgsql IMMUTABLE STRICT AS $$
DECLARE
    result bytea := ''::bytea;
    position integer := 1;
    character text;
BEGIN
    WHILE position <= length(value) LOOP
        character := substr(value, position, 1);
        IF character = '%' AND substr(value, position + 1, 2) ~ '^[0-9a-fA-F]{2}$' THEN
            result := result || decode(substr(value, position + 1, 2), 'hex');
            position := position + 3;
        ELSE
            result := result || convert_to(CASE WHEN character = '+' THEN ' ' ELSE character END, 'UTF8');
            position := position + 1;
        END IF;
    END LOOP;
    RETURN convert_from(result, 'UTF8');
EXCEPTION WHEN data_exception THEN
    RETURN NULL;
END;
$$;

-- CREATE INDEX restricts search_path. Retain the installation schema so that
-- existing quote/forward payloads can resolve the decoding helper at backfill.
CREATE FUNCTION messenger_message_references(payload jsonb)
RETURNS text[] LANGUAGE plpgsql IMMUTABLE STRICT SET search_path FROM CURRENT AS $$
DECLARE
    pending text[] := ARRAY[payload->>'content'];
    targets text[] := ARRAY[]::text[];
    position integer := 1;
    content text;
    reference text[];
    parameter text[];
    embedded text;
BEGIN
    IF payload->>'kind' <> 'markdown' THEN
        RETURN targets;
    END IF;
    WHILE position <= cardinality(pending) LOOP
        content := pending[position];
        position := position + 1;
        -- This is a candidate index. The Markdown parser decides which actual
        -- links to rewrite, so literals cannot cause a missed reverse lookup.
        FOR reference IN SELECT regexp_matches(content,
            '(urn:[a-z][a-z0-9_]*:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?![0-9a-f-])', 'gi')
        LOOP
            targets := array_append(targets, lower(reference[1]));
        END LOOP;
        FOR reference IN SELECT regexp_matches(content,
            'urn:(forward|quote):[0-9a-f-]{36}\?([^\s)>]+)', 'gi')
        LOOP
            parameter := regexp_match(reference[2],
                CASE WHEN lower(reference[1]) = 'forward'
                    THEN '(?:^|&)snapshot=([^&]*)' ELSE '(?:^|&)text=([^&]*)' END);
            IF parameter IS NULL THEN CONTINUE; END IF;
            embedded := messenger_reference_query_text(parameter[1]);
            IF lower(reference[1]) = 'forward' THEN
                BEGIN
                    embedded := translate(embedded, '-_', '+/');
                    embedded := convert_from(decode(embedded || repeat('=', (4 - length(embedded) % 4) % 4), 'base64'), 'UTF8');
                EXCEPTION WHEN data_exception THEN
                    embedded := NULL;
                END;
            END IF;
            IF embedded IS NOT NULL THEN
                pending := array_append(pending, embedded);
            END IF;
        END LOOP;
    END LOOP;
    RETURN ARRAY(SELECT DISTINCT target FROM unnest(targets) AS target ORDER BY target);
END;
$$;

-- Expression indexes follow every canonical write (including bridge imports)
-- atomically. Creating the index also covers existing history without fanout.
CREATE INDEX messenger_messages_entity_references_idx
    ON messenger_messages USING gin (messenger_message_references(payload))
    WHERE deleted_at IS NULL;
"""


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = ["0262-Reconvert-stored-Zulip-silent-user-mentions-80e205.py"]

    @property
    def migration_id(self):
        return "4272f7e8-65f7-415c-ad4e-964ce1a20907"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(UPGRADE_SQL)

    def downgrade(self, session):
        session.execute("""
            DROP INDEX messenger_messages_entity_references_idx;
            DROP FUNCTION messenger_message_references(jsonb);
            DROP FUNCTION messenger_reference_query_text(text);
        """)


migration_step = MigrationStep()
