# Ms_Activities — actividades deportivas

Microservicio independiente para que deportistas activos y con correo verificado publiquen actividades y consulten las publicadas por otros. Usa FastAPI y PostgreSQL, siguiendo la estructura de Ms_Matching.

## Estructura

- `app/api/v1/activities.py`: endpoints y dependencias de autenticación.
- `app/services/activities_service.py`: creación, consulta y datos públicos del organizador.
- `app/services/users_client.py`: validación del deportista mediante HTTP a Ms_Users.
- `app/repositories/postgres_activities_repository.py`: persistencia, transacciones y paginación.
- `app/schemas/activities.py`: contratos y validaciones.
- `db/migrations/001_activities.sql`: esquema propio `activities_api`, sin modificar tablas de ejemplo anteriores.
- `tests/test_activities.py`: pruebas con PostgreSQL en esquemas aislados.

## API

El gateway publica estas rutas; todas requieren un token de acceso de Ms_Users.

| Método | Ruta | Resultado |
|---|---|---|
| POST | `/api/v1/activities` | Publica una actividad (201). |
| GET | `/api/v1/activities?limit=20&cursor=UUID` | Próximas actividades por fecha, con `items` y `next_cursor`. |
| GET | `/api/v1/activities/{id}` | Detalle, incluido organizador público. |

Ejemplo del cuerpo para publicar (usar una fecha futura y un UUID nuevo por publicación):

```json
{
  "client_activity_id": "20000000-0000-4000-8000-000000000001",
  "title": "Running en el parque",
  "sport_code": "running",
  "starts_at": "2027-01-10T19:00:00-03:00",
  "location": "Parque Bicentenario, entrada principal, Vitacura",
  "description": "Trote recreativo de 5 km. Llevar agua."
}
```

El organizador se obtiene del JWT; no se acepta un `organizer_id` enviado por el cliente. Users comprueba el estado actual del deportista en cada solicitud. La respuesta del organizador solo incluye ID, nombre, inicial del apellido y foto pública. Una cuenta deshabilitada deja de aparecer como organizadora.

Fecha y hora deben incluir zona horaria y ser futuras. La app interpreta la fecha ingresada en la zona del dispositivo y envía UTC. La lista omite actividades iniciadas; su detalle sigue disponible por ID. Las actividades se ordenan por fecha e ID, con páginas de 1 a 100 registros. El cursor avanza sobre registros almacenados; una página puede tener menos resultados si algún organizador dejó de estar disponible.

`client_activity_id` permite reintentar una publicación sin duplicarla. La clave es única por organizador; reutilizarla con otro contenido devuelve 409. La base serializa reintentos concurrentes.

## Ejecutar

Con Docker, configurar `ACTIVITIES_DATABASE_URL` y el JWT compartido en `../Ms_gateway/.env`, y ejecutar desde esa carpeta:

```sh
docker compose up --build -d
```

El servicio escucha internamente en el puerto 8003. El gateway se conecta por `http://ms_activities:8003`. La base debe existir y el usuario debe poder crear su propio esquema. Las migraciones idempotentes se ejecutan al iniciar, protegidas por un bloqueo transaccional.

Para desarrollo sin Docker, crear un entorno virtual, instalar `requirements.txt` y exportar las variables descritas en `.env.example` antes de ejecutar:

```sh
uvicorn app.main:app --host 127.0.0.1 --port 8003
```

`USERS_SERVICE_URL` debe apuntar a Ms_Users. Los secretos de JWT, issuer y audience deben coincidir con Users y gateway. No se carga `.env` automáticamente.

Comprobación interna: `GET /api/v1/activities/health/ready`. El gateway incluye este servicio en su readiness y no publica su ruta interna de health.

## Pruebas

```sh
pip install -r requirements-dev.txt
# Configurar ACTIVITIES_TEST_DATABASE_URL con la conexión PostgreSQL de pruebas.
python -m pytest tests -q
```

Cada prueba crea y elimina exclusivamente un esquema `activities_test_<uuid>`. Sin esa variable, las pruebas de PostgreSQL se omiten.

## Alcance de esta entrega

Publicación, listado, paginación y detalle. La pantalla principal y Actividades usan datos reales y conservan el estilo de las tarjetas existentes. El lugar se ingresa como texto; no se calculan distancias para actividades. Inscripciones, cupos, edición, cancelación y chat grupal quedan fuera de esta primera entrega.

## Validación local — 6 de octubre de 2026

- 16 pruebas de integración PostgreSQL y 9 de autenticación/comunicación con Users correctas.
- 58 pruebas del gateway y 33 del frontend correctas.
- TypeScript, ESLint de los archivos modificados y exportación web completados.
- Flujo real por gateway Docker: cuenta A publica; cuenta B lista y abre el detalle; persistencia verificada con otra conexión PostgreSQL. Los reintentos mantienen el ID y una identidad enviada en el cuerpo se rechaza. Las dos cuentas y la actividad de QA se eliminaron al terminar.
- Para ejecutar localmente se reutilizaron imágenes Docker con las mismas dependencias debido a la espera del registro externo; los Dockerfiles versionables conservan la instalación reproducible desde requirements.
