CREATE TABLE IF NOT EXISTS activities_api.group_messages (
    id bigserial PRIMARY KEY,
    activity_id uuid NOT NULL REFERENCES activities_api.activities (id) ON DELETE CASCADE,
    author_id uuid NOT NULL,
    text varchar(1000) NOT NULL CHECK (length(btrim(text)) >= 1),
    client_message_id uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (activity_id, author_id, client_message_id)
);
CREATE INDEX IF NOT EXISTS group_messages_activity_idx ON activities_api.group_messages (activity_id, id);

-- Per member: up to which message they read and whether they muted the chat's alerts.
CREATE TABLE IF NOT EXISTS activities_api.group_chat_state (
    activity_id uuid NOT NULL REFERENCES activities_api.activities (id) ON DELETE CASCADE,
    user_id uuid NOT NULL,
    last_read_id bigint NOT NULL DEFAULT 0 CHECK (last_read_id >= 0),
    muted boolean NOT NULL DEFAULT false,
    PRIMARY KEY (activity_id, user_id)
);

CREATE OR REPLACE FUNCTION activities_api.notify_group_message() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE a activities_api.activities;
BEGIN
    SELECT * INTO a FROM activities_api.activities WHERE id = NEW.activity_id;
    -- Every other member (organizer and accepted applicants) who did not mute the chat.
    -- The alert never carries the message text.
    INSERT INTO activities_api.notification_outbox(user_id, kind, title, body, data)
    SELECT m.user_id, 'mensaje', 'Nuevo mensaje en ' || a.title, 'Abre el chat de la actividad para leerlo.',
        jsonb_build_object('screen', 'activity_chat', 'activityId', a.id)
    FROM (SELECT a.organizer_id AS user_id
          UNION
          SELECT p.applicant_id FROM activities_api.activity_applications p
          WHERE p.activity_id = a.id AND p.status = 'accepted') m
    WHERE m.user_id <> NEW.author_id
        AND NOT EXISTS (SELECT 1 FROM activities_api.group_chat_state s
                        WHERE s.activity_id = a.id AND s.user_id = m.user_id AND s.muted);
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS notification_group_message ON activities_api.group_messages;
CREATE TRIGGER notification_group_message AFTER INSERT ON activities_api.group_messages
    FOR EACH ROW EXECUTE FUNCTION activities_api.notify_group_message();
