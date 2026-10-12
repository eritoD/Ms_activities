CREATE TABLE IF NOT EXISTS activities_api.notification_outbox (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id uuid NOT NULL,
    kind text NOT NULL,
    title text NOT NULL,
    body text NOT NULL,
    data jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    published_at timestamptz
);
CREATE INDEX IF NOT EXISTS activities_outbox_pending ON activities_api.notification_outbox(id) WHERE published_at IS NULL;

CREATE OR REPLACE FUNCTION activities_api.notify_application() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE a activities_api.activities;
BEGIN
    IF TG_OP = 'DELETE' THEN
        -- The applicant withdrew. A cascade from deleting the activity finds no row and stays silent.
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
            CASE WHEN NEW.status = 'accepted' THEN 'Postulación aceptada' ELSE 'Postulación rechazada' END,
            'Revisa tu postulación a ' || a.title || '.',
            jsonb_build_object('screen', 'activity', 'activityId', a.id));
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS notification_application ON activities_api.activity_applications;
CREATE TRIGGER notification_application AFTER INSERT OR UPDATE OR DELETE ON activities_api.activity_applications
    FOR EACH ROW EXECUTE FUNCTION activities_api.notify_application();

CREATE OR REPLACE FUNCTION activities_api.notify_activity() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF ROW(OLD.title, OLD.sport_code, OLD.description, OLD.starts_at, OLD.location, OLD.capacity, OLD.cancelled_at)
        IS DISTINCT FROM ROW(NEW.title, NEW.sport_code, NEW.description, NEW.starts_at, NEW.location, NEW.capacity, NEW.cancelled_at) THEN
        INSERT INTO activities_api.notification_outbox(user_id, kind, title, body, data)
        SELECT p.applicant_id, 'actividad',
            CASE WHEN NEW.cancelled_at IS NOT NULL THEN 'Actividad cancelada' ELSE 'Cambios en tu actividad' END,
            CASE WHEN NEW.cancelled_at IS NOT NULL THEN NEW.title || ' fue cancelada.'
                ELSE 'Revisa los datos actualizados de ' || NEW.title || '.' END,
            jsonb_build_object('screen', 'activity', 'activityId', NEW.id)
        FROM activities_api.activity_applications p
        WHERE p.activity_id = NEW.id AND p.status IN ('pending', 'accepted') AND p.applicant_id <> NEW.organizer_id;
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS notification_activity ON activities_api.activities;
CREATE TRIGGER notification_activity AFTER UPDATE ON activities_api.activities
    FOR EACH ROW EXECUTE FUNCTION activities_api.notify_activity();

CREATE OR REPLACE FUNCTION activities_api.notify_rating() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' OR ROW(OLD.score, OLD.comment) IS DISTINCT FROM ROW(NEW.score, NEW.comment) THEN
        INSERT INTO activities_api.notification_outbox(user_id, kind, title, body, data)
        VALUES (NEW.rated_id, 'calificacion', 'Recibiste una calificación',
            'Revisa tu reputación en tu perfil.', jsonb_build_object('screen', 'profile'));
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS notification_rating ON activities_api.ratings;
CREATE TRIGGER notification_rating AFTER INSERT OR UPDATE ON activities_api.ratings
    FOR EACH ROW EXECUTE FUNCTION activities_api.notify_rating();
