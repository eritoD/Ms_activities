from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException
from psycopg import sql
from psycopg.rows import dict_row


class PostgresActivitiesRepository:
    def __init__(self, pool, schema="activities_api"):
        self.pool, self.schema = pool, schema

    def execute(self, cursor, statement, params=None):
        query = sql.SQL(statement.replace("activities_api", "{schema}")).format(schema=sql.Identifier(self.schema))
        return cursor.execute(query, params)

    def migrate(self):
        with self.pool.connection() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('activities-api-schema', 0))")
            for migration in sorted((Path(__file__).parents[2] / "db/migrations").glob("*.sql")):
                self.execute(conn, migration.read_text())

    def ready(self):
        with self.pool.connection() as conn:
            self.execute(conn, "SELECT id FROM activities_api.activities LIMIT 1")

    def create(self, actor_id, payload):
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            # Serialize retries before checking the date, even if the event has since started.
            cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                        (f"activity:{actor_id}:{payload.client_activity_id}",))
            self.execute(cur, "SELECT * FROM activities_api.activities WHERE organizer_id=%s AND client_activity_id=%s",
                         (actor_id, payload.client_activity_id))
            existing = cur.fetchone()
            if existing:
                if any(existing[key] != value for key, value in payload.model_dump().items()):
                    raise HTTPException(409, "Este intento ya publicó otra actividad. Actualiza la pantalla.")
                return existing
            if payload.starts_at <= datetime.now(timezone.utc):
                raise HTTPException(422, "La fecha y hora deben ser futuras.")
            self.execute(cur, """INSERT INTO activities_api.activities
                (id, organizer_id, client_activity_id, title, sport_code, description, starts_at, location)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                (uuid4(), actor_id, payload.client_activity_id, payload.title, payload.sport_code,
                 payload.description, payload.starts_at, payload.location))
            return cur.fetchone()

    def get(self, activity_id):
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            self.execute(cur, "SELECT * FROM activities_api.activities WHERE id=%s", (activity_id,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(404, "Actividad no disponible.")
            return row

    def upcoming(self, limit, cursor):
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            boundary = None
            if cursor:
                self.execute(cur, "SELECT starts_at, id FROM activities_api.activities WHERE id=%s", (cursor,))
                boundary = cur.fetchone()
                if not boundary:
                    raise HTTPException(422, "Página inválida. Actualiza las actividades.")
            self.execute(cur, """SELECT * FROM activities_api.activities WHERE starts_at > CURRENT_TIMESTAMP
                AND (%s::timestamptz IS NULL OR (starts_at,id) > (%s,%s))
                ORDER BY starts_at,id LIMIT %s""",
                (boundary['starts_at'] if boundary else None, boundary['starts_at'] if boundary else None,
                 boundary['id'] if boundary else None, limit + 1))
            rows = cur.fetchall()
            return rows[:limit], rows[limit - 1]['id'] if len(rows) > limit else None
