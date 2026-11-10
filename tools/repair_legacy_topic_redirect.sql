-- Opt-in maintenance for an old, privately extended topic schema.
-- The current schema does not define this redirect column or constraint.
DO $$
DECLARE definition text;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended('workspace-read-state-schema-v1',0));
    SELECT pg_get_constraintdef(oid) INTO definition FROM pg_constraint
    WHERE conrelid='messenger_topics'::regclass AND conname='messenger_topics_redirect_fkey';
    IF definition IS NULL OR definition=
        'FOREIGN KEY (project_id, redirect_topic_uuid) REFERENCES messenger_topics(project_id, uuid) ON DELETE SET NULL (redirect_topic_uuid)' THEN
        RETURN;
    END IF;
    IF definition<>
        'FOREIGN KEY (project_id, redirect_topic_uuid) REFERENCES messenger_topics(project_id, uuid) ON DELETE SET NULL' THEN
        RAISE EXCEPTION 'Unexpected legacy topic redirect constraint; inspect before repair';
    END IF;
    ALTER TABLE messenger_topics DROP CONSTRAINT messenger_topics_redirect_fkey;
    ALTER TABLE messenger_topics ADD CONSTRAINT messenger_topics_redirect_fkey
        FOREIGN KEY(project_id,redirect_topic_uuid) REFERENCES messenger_topics(project_id,uuid)
        ON DELETE SET NULL (redirect_topic_uuid);
END $$;
