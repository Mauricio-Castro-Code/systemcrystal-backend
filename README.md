# BackendCrystal

Backend inicial en Django para System Crystal, alineado con los modelos que hoy usa el frontend Angular.

## Stack

- Django 5
- Django REST Framework
- Token Authentication
- **Supabase (Postgres)** vía `psycopg` + `dj-database-url`
- CORS habilitado para `http://localhost:4200`

## Configuracion

1. Crea el archivo `.env` a partir de `.env.example`.
2. Activa un entorno Python e instala dependencias con `python3 -m pip install -r requirements.txt`.
3. **Conectar Supabase:** entra a Supabase → tu proyecto → *Project Settings → Database → Connection string → URI* y copia la cadena (preferentemente la del *Session pooler*, puerto 5432). Pegala en `.env` como:
   ```
   SUPABASE_DATABASE_URL=postgresql://postgres.<ref>:<password>@aws-0-us-east-1.pooler.supabase.com:5432/postgres
   ```
   El SSL queda forzado por default (`DB_SSL_REQUIRE=True`). Si dejas la URL vacia, el backend cae a SQLite local (`db.sqlite3`) para desarrollo.
4. Genera una `DJANGO_SECRET_KEY` aleatoria de al menos 50 caracteres. El arranque en producción rechaza claves ausentes o inseguras. `DJANGO_DEBUG` ahora es `False` por defecto; para desarrollo local configura `DJANGO_DEBUG=True` y `DJANGO_SECURE_SSL_REDIRECT=False`.
   Deja `REGISTRATION_ACCESS_KEY` vacía para deshabilitar el registro público, o configura una clave privada para crear usuarios de ventas. Los administradores se crean con `python manage.py createsuperuser` o desde Equipo con una sesión administradora.
5. Corre `python3 manage.py migrate` y listo — Django crea las tablas en Supabase.
6. Si quieres usar otra plantilla Excel, define `NOTE_EXCEL_TEMPLATE_PATH` y/o `QUOTATION_EXCEL_TEMPLATE_PATH`. Si los dejas vacios, el backend usa `templates/excel/Nota.xlsx`.

## Generacion de PDF (plantilla Excel + LibreOffice)

El diseño visual del PDF (logos, colores, fórmulas, layout) vive en la plantilla `.xlsx` — Python solo llena celdas y LibreOffice convierte a PDF preservando el formato.

Instalar LibreOffice:

- **macOS:** `brew install --cask libreoffice`
- **Linux:** `apt-get install -y libreoffice`

Si el binario no esta en `PATH`, defínelo con `LIBREOFFICE_BINARY` en `.env`:

```
LIBREOFFICE_BINARY=/Applications/LibreOffice.app/Contents/MacOS/soffice
```

Si LibreOffice no esta disponible, en macOS/Windows el backend cae a Excel nativo via AppleScript / COM. Puedes forzar el backend con `DOCUMENT_RENDERER_BACKEND=macos|windows|auto` (default `auto`).

Gotchas ya resueltos en `api/excel_exports.py`:

- Celdas escritas con `openpyxl` reciben `PatternFill` blanco explícito (sin esto LibreOffice las renderiza grises).
- Las imágenes WMF de la plantilla (logos, íconos) se restauran a nivel ZIP después de `wb.save()`, ya que `openpyxl` las descarta.
- Los warnings de WMF al cargar la plantilla se silencian.
- Plantillas en `.xls` antiguo se convierten automáticamente a `.xlsx` con LibreOffice.

## Comandos

```bash
cd /Users/mauriciocv/Desktop/SS_Programs/SystemCrystal/BackendCrystal
python3 manage.py migrate
python3 manage.py seed_demo_data
python3 manage.py runserver
```

## Credenciales demo

`seed_demo_data` solo funciona con `DJANGO_DEBUG=True`. Nunca usar estas credenciales en producción.

- Usuario: `admin`
- Password: `OrderFlow123`

## Endpoints base

- `POST /api/auth/login/`
- `POST /api/auth/register/`
- `GET /api/auth/me/`
- `POST /api/auth/logout/`
- `GET /api/clients/`
- `GET|POST /api/quotations/`
- `GET|PUT|DELETE /api/quotations/<quotation_id>/`
- `GET /api/quotations/<quotation_id>/export/excel/`
- `GET /api/quotations/<quotation_id>/export/pdf/`
- `POST /api/quotations/<quotation_id>/confirm/`
- `GET|POST /api/orders/`
- `GET|PUT|DELETE /api/orders/<order_id>/`
- `GET /api/orders/<order_id>/export/excel/`
- `GET /api/orders/<order_id>/export/pdf/`
- `GET /api/dashboard/overview/`
- `GET /api/health/`

## Respaldos manuales por sección

- `GET /api/clients/export/excel/`: descarga `Clientes.xlsx`, con todos los clientes y una hoja de direcciones.
- `GET /api/orders/export/excel/`: ZIP con un Excel por nota activa, usando la plantilla `Nota.xlsx` y el folio como nombre.
- `GET /api/orders/archive/export/excel/`: el mismo ZIP para las notas recogidas del registro.

Requieren ventas o administrador e incluyen toda la sección, independientemente de filtros o paginación. Las notas canceladas se incluyen en la sección correspondiente a su estado operativo. No se generan PDFs para estos respaldos. Una sección de notas vacía devuelve un ZIP vacío; clientes devuelve sus encabezados.

La plantilla admite hasta 21 renglones de equipo por nota. Si una nota heredada excede ese límite, la descarga informa su folio y falla completa: no omite notas ni sustituye su formato por una tabla. El respaldo programado conserva su comportamiento anterior.

## Contrato de datos

Las respuestas de clientes, cotizaciones y pedidos siguen la misma forma de datos que hoy consumen los servicios del frontend, para que la sustitucion de `localStorage` por llamadas HTTP sea directa.

## Seguridad y pruebas

- HTTPS y cookies seguras están habilitados por defecto fuera de desarrollo. Railway se detecta por `RAILWAY_PROJECT_ID` y se reconoce automáticamente su cabecera `X-Forwarded-Proto`. En otros alojamientos con proxy confiable, configurar `DJANGO_TRUST_PROXY_SSL_HEADER=True`. Un valor explícito `False` deshabilita esta detección y puede causar un bucle de redirecciones detrás de Railway.
- Las rutas generales requieren ventas o administrador. Los choferes conservan sesión, ruta y actualización operativa de sus propias notas; no pueden usar la actualización masiva ni modificar cobros.
- El login tiene un límite básico de 10 solicitudes/minuto y el registro de 5/hora por IP. Configurar `DJANGO_NUM_PROXIES` según la infraestructura. La caché local se separa por proceso: para protección en producción se necesita también un límite en el proxy/WAF y una caché compartida; el throttling de DRF no garantiza protección contra fuerza bruta.
- Cambiar una contraseña o desactivar un usuario revoca sus tokens. Los tokens actuales no tienen caducidad automática.
- `.dockerignore` excluye secretos, bases locales, entornos virtuales y exportaciones de la imagen.

Pruebas sin leer `.env` ni conectar a Supabase:

```bash
python manage.py test api --settings=config.test_settings --noinput
python manage.py makemigrations --check --dry-run --settings=config.test_settings
```

`config.test_settings` utiliza SQLite en memoria y un hash rápido únicamente para pruebas; nunca usarlo para desplegar. Antes de publicar, ejecutar `python manage.py check --deploy` con la configuración real y probar también contra PostgreSQL de staging.
