-- HU-64: reply to a specific message of the same group chat.
-- reply_to_id points at another message of the SAME activity: the foreign key is (activity_id, reply_to_id), so the
-- database itself refuses a reply to a message of another chat. is_reply is written when the message is saved. If the
-- quoted message is ever deleted, the reply stays (reply_to_id becomes NULL) and is_reply stays true, so the app can
-- say "message not available" without showing what it said. The only deletion today is the purge of a whole chat,
-- which removes replies and originals together; there is deliberately no trigger, because it would break that bulk
-- delete. Idempotent: every step checks before it changes anything.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_attribute
        WHERE attrelid = to_regclass('activities_api.group_messages') AND attname = 'reply_to_id' AND NOT attisdropped
    ) THEN
        ALTER TABLE activities_api.group_messages
            ADD COLUMN reply_to_id BIGINT,
            ADD COLUMN is_reply BOOLEAN NOT NULL DEFAULT FALSE;
    END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS group_messages_activity_id_id ON activities_api.group_messages (activity_id, id);
CREATE INDEX IF NOT EXISTS group_messages_reply_to ON activities_api.group_messages (reply_to_id)
    WHERE reply_to_id IS NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'group_messages_reply_same_chat'
                   AND conrelid = to_regclass('activities_api.group_messages')) THEN
        ALTER TABLE activities_api.group_messages
            ADD CONSTRAINT group_messages_reply_same_chat FOREIGN KEY (activity_id, reply_to_id)
                REFERENCES activities_api.group_messages (activity_id, id) ON DELETE SET NULL (reply_to_id);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'group_messages_reply_not_itself'
                   AND conrelid = to_regclass('activities_api.group_messages')) THEN
        ALTER TABLE activities_api.group_messages
            ADD CONSTRAINT group_messages_reply_not_itself CHECK (reply_to_id IS NULL OR reply_to_id <> id);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'group_messages_reply_flag'
                   AND conrelid = to_regclass('activities_api.group_messages')) THEN
        ALTER TABLE activities_api.group_messages
            ADD CONSTRAINT group_messages_reply_flag CHECK (reply_to_id IS NULL OR is_reply);
    END IF;
END $$;
