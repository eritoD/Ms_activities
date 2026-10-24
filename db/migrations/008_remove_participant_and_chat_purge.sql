DO $$
DECLARE constraint_name text;
BEGIN
    SELECT c.conname INTO constraint_name
    FROM pg_constraint c
    WHERE c.conrelid = 'activities_api.activity_applications'::regclass
        AND c.contype = 'c' AND pg_get_constraintdef(c.oid) ILIKE '%status%';
    IF constraint_name IS NOT NULL THEN
        EXECUTE format('ALTER TABLE activities_api.activity_applications DROP CONSTRAINT %I', constraint_name);
    END IF;
END $$;
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
        WHERE conrelid = 'activities_api.activity_applications'::regclass
            AND conname = 'activity_applications_status_check') THEN
        ALTER TABLE activities_api.activity_applications
            ADD CONSTRAINT activity_applications_status_check
            CHECK (status IN ('pending', 'accepted', 'rejected', 'removed'));
    END IF;
END $$;
CREATE INDEX IF NOT EXISTS activities_chat_purge_idx
    ON activities_api.activities ((COALESCE(cancelled_at, starts_at)), id);

CREATE OR REPLACE FUNCTION activities_api.notify_application() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE a activities_api.activities;
BEGIN
    IF TG_OP = 'DELETE' THEN
        SELECT * INTO a FROM activities_api.activities WHERE id = OLD.activity_id;
        IF a.id IS NOT NULL AND a.cancelled_at IS NULL AND OLD.applicant_id <> a.organizer_id
            AND OLD.status IN ('pending', 'accepted') THEN
            INSERT INTO activities_api.notification_outbox(user_id, kind, title, body, data)
            VALUES (a.organizer_id, 'actividad', 'Cancelaron una postulación',
                CASE WHEN OLD.status = 'accepted' THEN 'Un participante se retiró de ' || a.title || '. Se liberó un cupo.'
                    ELSE 'Una persona retiró su postulación a ' || a.title || '.' END,
                jsonb_build_object('screen', 'activity', 'activityId', a.id));
        END IF;
        RETURN OLD;
    END IF;
    SELECT * INTO a FROM activities_api.activities WHERE id = NEW.activity_id;
    IF TG_OP = 'INSERT' AND NEW.applicant_id <> a.organizer_id THEN
        INSERT INTO activities_api.notification_outbox(user_id, kind, title, body, data)
        VALUES (a.organizer_id, 'actividad', 'Nueva postulación',
            'Hay una nueva postulación a ' || a.title || '.',
            jsonb_build_object('screen', 'activity', 'activityId', a.id));
    ELSIF TG_OP = 'UPDATE' AND OLD.status IS DISTINCT FROM NEW.status THEN
        INSERT INTO activities_api.notification_outbox(user_id, kind, title, body, data)
        VALUES (NEW.applicant_id, 'actividad',
            CASE WHEN NEW.status = 'accepted' THEN 'Postulación aceptada'
                 WHEN NEW.status = 'removed' THEN 'Ya no participas en «' || a.title || '»'
                 ELSE 'Postulación rechazada' END,
            CASE WHEN NEW.status = 'removed' THEN 'El organizador te retiró de esta actividad.'
                 ELSE 'Revisa tu postulación a ' || a.title || '.' END,
            jsonb_build_object('screen', 'activity', 'activityId', a.id));
    END IF;
    RETURN NEW;
END $$;
