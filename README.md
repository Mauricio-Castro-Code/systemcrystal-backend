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
4. Define `REGISTRATION_ACCESS_KEY` para controlar quien puede crear usuarios.
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

## Contrato de datos

Las respuestas de clientes, cotizaciones y pedidos siguen la misma forma de datos que hoy consumen los servicios del frontend, para que la sustitucion de `localStorage` por llamadas HTTP sea directa.
