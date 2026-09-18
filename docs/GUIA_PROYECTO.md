# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Proyecto

Sistema de gestión administrativa para una empresa de renta de vajilla, loza, sillas y mesas para eventos.

**Paleta de colores:** Azul `#1d2f58`, Verde `#277740`, Blanco `#f2f2f2`

---

## Comandos de desarrollo

### Backend (`BackendCrystal/`)

```bash
# Activar entorno virtual
source venv/bin/activate

# Instalar dependencias
pip install -r requirements.txt

# Migraciones
python manage.py makemigrations
python manage.py migrate

# Servidor de desarrollo (localhost:8000)
python manage.py runserver

# Tests
python manage.py test api --settings=config.test_settings

# Crear superusuario
python manage.py createsuperuser
```

Copiar `.env.example` a `.env` y configurar antes de correr por primera vez. Sin `SUPABASE_DATABASE_URL`, usa SQLite local (`db.sqlite3`).

### Frontend (`FrontendCrystal/`)

```bash
# Instalar dependencias
npm install

# Servidor de desarrollo (localhost:4200)
npm start

# Build de producción
npm run build

# Tests (vitest)
npm test

# Formatear código
npx prettier --write src/
```

---

## Arquitectura

### Backend

**Stack:** Django 5.2.17 + Django REST Framework + Token Auth. Una sola app Django: `api/`.

| Archivo | Responsabilidad |
|---------|----------------|
| `api/models.py` | Todos los modelos y lógica de generación de folios |
| `api/views.py` | Vistas APIView; cada recurso tiene List/Detail/acciones |
| `api/services.py` | Lógica de negocio transaccional (crear/actualizar notas, confirmar órdenes) |
| `api/presenters.py` | Serialización de salida (construye dicts para Response) |
| `api/serializers.py` | Validación de entrada con DRF serializers |
| `api/excel_exports.py` | Generación de Excel (openpyxl) y PDF (LibreOffice o fpdf2) |
| `api/note_import.py` | Lectura de una nota desde `.xlsx` (mismo mapa de celdas que `excel_exports`) para reimportarla; usado por `OrderImportView` (`/orders/import/`) |
| `api/client_directory.py` | Lógica de directorio y perfil de clientes |
| `config/settings.py` | Configuración; lee `.env` propio sin python-dotenv |

**Despliegue:** `Dockerfile` + `gunicorn.conf.py` para producción; archivos generados (Excel/PDF temporales) viven en `var/`.

**Autenticación:** Token DRF. El registro está protegido por `REGISTRATION_ACCESS_KEY` (env var). Usuarios admin tienen `is_staff=True`; el guard `IsAdminUser` protege endpoints sensibles.

**Base de datos:** SQLite en desarrollo; Supabase (Postgres) en producción vía `SUPABASE_DATABASE_URL` o `DATABASE_URL`.

**Generación de documentos:** Las notas (pedidos y cotizaciones) se exportan a Excel usando una plantilla `templates/excel/Nota.xlsx` con openpyxl. La conversión a PDF usa LibreOffice si `LIBREOFFICE_BINARY` está configurado; de lo contrario usa fpdf2.

### Folios (IDs de documentos)

- **Clientes:** `CLI-001`, `CLI-002`, …
- **Cotizaciones:** `001COTI-26`, `002COTI-26`, … (sufijo = año 2 dígitos)
- **Órdenes/Notas:** `0001-26`, `0002-26`, … (sufijo = año 2 dígitos). Los folios de órdenes **reutilizan huecos** de folios eliminados para mantener la secuencia anual compacta (`next_available_order_value`).

### Ciclo de vida de documentos

```
Cotización (DRAFT) ──[confirmar]──► Orden (OneToOne con Quotation confirmada)
                                      │
                                      ├─ operational_status: PROGRAMADA → ENTREGADO → POR_RECOGER → RECOGIDO
                                      └─ billing_status: AL_CORRIENTE → POR_COBRAR → COBRADO
```

Cada cambio de estado genera un `OrderWorkflowEvent` (auditoría). Las órdenes en estado `RECOGIDO` van al archivo (`/orders/archive/`).

### Frontend

**Stack:** Angular 21 (standalone components), Angular Material, TypeScript 5.9, SCSS, Prettier.

**URL del API:** definida por `src/environments/environment.ts` y `environment.prod.ts`; `core/config/api.config.ts` expone `API_BASE_URL`.

**Organización por features** (`src/app/features/`):

| Feature | Ruta | Descripción |
|---------|------|-------------|
| `auth` | `/login` | Login/registro |
| `dashboard` | `/dashboard` | Vista general + agenda de entregas |
| `pedidos` | `/pedidos` | Notas/órdenes activas y archivo |
| `cotizaciones` | `/cotizaciones` | Cotizaciones en borrador |
| `clientes` | `/clientes` | Directorio y perfil de clientes |
| `inventario` | `/inventario` | Catálogo de productos |
| `contabilidad` | `/contabilidad` | Reportes financieros (solo admin) |
| `equipo` | `/equipo` | Gestión de miembros del equipo/usuarios (endpoints `/team/`) |
| `fletes` | `/fletes` | Zonas de flete |

El ruteo raíz vive en `src/app/app-routing.module.ts` (lazy `loadChildren` por feature), no en un `app.routes.ts`.

**Guards:** `authGuard` (requiere sesión), `adminGuard` (requiere `isAdmin`), `guestGuard` (redirige si ya hay sesión).

**Servicios core** (`src/app/core/services/`): un servicio por recurso del API; `auth.service.ts` gestiona el token en localStorage y el estado de sesión.

**Layout:** `main-layout` (sidebar + router-outlet) para rutas protegidas; `auth-layout` para login.
