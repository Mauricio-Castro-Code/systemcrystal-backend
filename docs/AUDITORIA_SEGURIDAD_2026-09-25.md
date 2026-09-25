# Auditoría de seguridad — 25 de septiembre de 2026

## Resultado y alcance

Se encontraron dos problemas de prioridad alta: reasignación de notas ajenas por un chofer y persistencia de datos de una sesión anterior en el frontend. Ambos se reprodujeron localmente. Los demás hallazgos son controles incompletos y dependencias locales con avisos; no equivalen a una explotación demostrada en producción.

Base revisada: backend `52273f8`, frontend `f6975e8`, incluyendo los cambios locales pendientes para ocultar acciones no autorizadas. Auditoría de código, pruebas aisladas con SQLite en memoria y consultas de dependencias a npm/OSV. No se leyó `.env`, no se consultó Supabase ni se enviaron ataques a producción. No se modificó código funcional durante esta auditoría.

No se encontró la skill solicitada “system debugging/systematic debugging” en el proyecto, skills personales ni plugins instalados. Se utilizó la guía de seguridad de django-expert. Se contrastó también el informe anterior `REVISION_SEGURIDAD.md`.

## 1. Alta — Un chofer puede apropiarse de una nota asignada a otro

**Ubicación:** `api/views.py:1284`, `DriverRouteAddOrderView.post`.

Solo se valida que el solicitante sea chofer y que la nota esté activa. Al proporcionar un folio se llama a `assign_order_driver` sin verificar la asignación anterior ni autorización de oficina. Los folios no son un secreto y tienen estructura predecible.

**Reproducción confirmada:** crear una nota y asignarla al chofer B; autenticar al chofer A; enviar su folio a `order-my-route-add-order`. La respuesta fue 200, la asignación cambió a A y se devolvieron datos del cliente. Después de adquirir la asignación, A satisface las comprobaciones de propietario de otros endpoints de su ruta.

**Impacto:** acceso a domicilio, teléfono e instrucciones de otra ruta; alteración de la asignación operativa.

**Corrección propuesta:** limitar la incorporación a notas previamente autorizadas; como mínimo impedir tomar notas de otro chofer. Usar una operación transaccional con bloqueo para evitar carreras de asignación. La función de autoasignación existe deliberadamente; su alcance comercial necesita definirse sin tratar el conocimiento del folio como autorización.

## 2. Alta — Datos de notas sobreviven al cambio de usuario

**Ubicaciones:** `FrontendCrystal/src/app/core/services/auth.service.ts:59`; `order-records.service.ts:150`; `features/pedidos/pages/order-note-page/order-note-page.html:7`.

`clearSession` elimina la sesión, pero no los datos de los servicios singleton. `loadOrderById` conserva la nota anterior cuando el servidor devuelve 403. El detalle muestra cualquier `orderRecord` disponible antes de mostrar un error, y `/pedidos` usa un guard que solo exige estar autenticado.

**Reproducción confirmada en servicios reales con HTTP simulado:** oficina carga una nota con nombre e importe; cierra sesión correctamente; inicia un chofer; la recarga de la nota recibe 403. `getOrderById` todavía entrega el nombre y los $9,900 anteriores. La condición de la plantilla que muestra esos datos se verificó estáticamente; no hubo navegador disponible para una reproducción visual.

**Precondición:** misma instancia de la aplicación en un dispositivo compartido, sin recargar la página, y datos previamente consultados. No es acceso remoto nuevo a la base de datos.

**Corrección propuesta:** vaciar todos los almacenes de datos al cerrar/cambiar sesión; invalidar respuestas HTTP iniciadas por la sesión anterior; no mostrar datos de una nota cuando su autorización falla; guards coherentes con los roles del backend. Revisar también clientes, cotizaciones, dashboard y cachés de documentos.

## 3. Media — Tokens sin vencimiento

**Ubicaciones:** `api/views.py:320`, `config/settings.py:226`; `FrontendCrystal/src/app/core/services/auth.service.ts:110`.

El login reutiliza un token de DRF y el frontend lo persiste en localStorage. La autenticación no comprueba antigüedad.

**Reproducción confirmada:** un token con fecha de creación de hace diez años fue aceptado por `/auth/me/` (200). Esto no prueba robo de tokens ni XSS. Sí confirma que, si se roba uno, no caduca por tiempo. Logout correcto, cambio de contraseña y desactivación sí revocan tokens; un logout sin conexión solo puede limpiar la sesión local.

**Corrección propuesta:** caducidad y revocación verificadas por servidor, política de sesiones y rotación. Evaluar cookies HttpOnly con CSRF/CORS adecuados si se cambia el almacenamiento; no sustituirlas sin adaptar la arquitectura.

## 4. Media — Operaciones costosas sin límite de aplicación

**Ubicaciones:** `api/views.py:1196` y `api/views.py:1251`, `api/route_optimization.py:113`; exportaciones en `api/section_exports.py`.

Optimización de rutas y restricciones del chofer no tienen throttles. Se confirmó que sus `throttle_classes` son listas vacías. La interpretación de notas puede invocar un proveedor de IA; la optimización puede usar Maps. Existen cachés en algunas operaciones, pero no son cuotas por usuario. Exportaciones completas y PDF también consumen recursos sin límites específicos.

**Impacto potencial:** gasto o saturación por una cuenta válida o comprometida. No se hicieron llamadas reales de pago ni pruebas de carga.

**Corrección propuesta:** cuotas por usuario y organización, límites de concurrencia y de tamaño, timeouts y presupuestos del proveedor; procesamiento asíncrono para exportaciones grandes.

## 5. Media condicionada — Dependencias del entorno Python local

- `npm audit --omit=dev`: **0 avisos** en el árbol de producción del frontend. No incluye herramientas de desarrollo.
- OSV sobre las **12 dependencias directas fijadas** en requirements: **0 avisos** devueltos.
- OSV sobre **38 paquetes instalados** del venv: avisos para `sqlparse 0.5.5` y `pip 25.1.1`. Los identificadores GHSA/PYSEC pueden ser alias del mismo problema; no deben sumarse como vulnerabilidades independientes.
- Ejemplo confirmado de sqlparse: [GHSA-cfqr-cjx5-5jcm](https://github.com/andialbrecht/sqlparse/security/advisories/GHSA-cfqr-cjx5-5jcm), consumo elevado de CPU al reformatear SQL; OSV indica corrección en 0.6.0.
- Ejemplo confirmado de pip: [GHSA-4xh5-x5gv-qwph](https://github.com/pypa/pip/pull/13550), extracción de archivos; corrección indicada para ese aviso en 25.3. Ese número no garantiza resolver los demás avisos.

No se identificó una entrada pública que explote directamente esas funciones de sqlparse; pip corresponde al proceso de instalación. **No se verificaron las versiones de la imagen desplegada.** `requirements.txt` no fija todas las dependencias transitivas, y Docker resuelve versiones al construir.

**Corrección propuesta:** actualizar y volver a auditar todo el entorno, fijar un conjunto reproducible de dependencias y auditar la imagen desplegada, sin asumir que el venv local equivale a producción.

## Controles adicionales a endurecer

- Login/registro usan cache por proceso de forma predeterminada; Gunicorn configura dos workers. El límite no es global ni resistente a concurrencia. `NUM_PROXIES=0` usa REMOTE_ADDR; su efecto detrás de Railway depende de la configuración real. Validar el proxy y usar almacenamiento compartido/controles en infraestructura. [Limitaciones documentadas por DRF](https://www.django-rest-framework.org/api-guide/throttling/).
- La CSP de Vercel tiene `base-uri`, `object-src` y `frame-ancestors`, pero no restringe scripts. Es endurecimiento pendiente, no evidencia de XSS explotable. No se encontraron usos de `bypassSecurityTrust*` o `innerHTML` en el código inspeccionado.
- Docker no define usuario no-root. Reducir privilegios y aislar renderizadores de documentos limita el impacto de una eventual vulnerabilidad.
- `check --deploy` con configuración sintética de producción y sin `.env`: únicamente advertencias de HSTS para subdominios y preload. No habilitarlos sin comprobar todos los dominios. El resultado no certifica la configuración real del despliegue.

## Verificación ejecutada

1. **39 pruebas** de `api.tests_security`, `api.tests_proxy`, `api.tests_backups`, `api.tests_section_exports`: aprobadas.
2. **4 pruebas diagnósticas adicionales**: reprodujeron autoasignación ajena, token antiguo y ausencia de throttles; comprobaron rechazo 403 para **11 operaciones administrativas** solicitadas por oficina.
3. **6 pruebas de frontend**: 3 del interceptor, 2 de permisos de inventario y 1 reproducción de datos persistentes entre sesiones: aprobadas. Las pruebas diagnósticas afirman la presencia del fallo, no su corrección.
4. `pip check`: sin incompatibilidades declaradas.
5. Auditorías npm y OSV descritas arriba; resultados guardados en `audits/2026-09-25/`.

Las reproducciones están guardadas como `.txt` para no incorporarlas como pruebas de regresión que normalicen un fallo. Para repetir backend, copiar `backend-probe.py.txt` a `/private/tmp/crystal_security_probe.py` y ejecutar desde BackendCrystal: `PYTHONPATH=/private/tmp venv/bin/python manage.py test crystal_security_probe --settings=config.test_settings`. Para frontend, copiar temporalmente el archivo a `src/app/core/services/audit-session-cache.spec.ts`, ejecutar Angular con `--include` para ese archivo y retirarlo después. Nunca usar datos reales para estas reproducciones.

## Prioridad de remediación y límites

Primero cerrar la reasignación de notas y el cruce de datos entre sesiones; después caducidad de tokens, cuotas y actualización de dependencias. Mantener las comprobaciones del backend aunque la interfaz oculte controles.

No se auditaron infraestructura desplegada, secretos, historial completo de Git, políticas RLS de Supabase, imagen Docker construida, dependencias del sistema operativo ni controles del proveedor. Las pruebas no garantizan ausencia de otras vulnerabilidades. Esta revisión produce evidencia e informe; no aplica correcciones, commits ni despliegues.

## Contraste con systematic-debugging

Se leyó la [skill proporcionada por el usuario](https://github.com/obra/superpowers/blob/main/skills/systematic-debugging/SKILL.md) después de la auditoría inicial. Se aplica a esta revisión, sin instalarla globalmente.

- **Causa y reproducción:** los diagnósticos ya capturan la entrada, resultado HTTP y estado persistido/cacheado para cada fallo alto.
- **Historial:** `git blame` sitúa la autoasignación sin comprobación de propietario en `71b7184c` (11 de septiembre). Los rediseños recientes no introdujeron esa lógica.
- **Comparación con código correcto:** `OrderAssignmentView` reserva la asignación general a administrador/ventas; `OrderRouteConstraintView._get_owned_order` sí verifica que la nota pertenezca al chofer. `DriverRouteAddOrderView` no aplica ninguna de esas barreras antes de cambiar el propietario.
- **Flujo de sesión:** tanto logout como el interceptor de 401 terminan en `clearSession`; este limpia credenciales pero no el estado singleton de notas. La recarga denegada conserva ese estado y la plantilla prioriza el registro antiguo sobre el error. Se trata de aislamiento de datos entre sesiones, no de un fallo de autorización de la API que devuelve 403 correctamente.
- **Estado:** investigación y diagnóstico realizados. La implementación y verificación de correcciones siguen pendientes; las pruebas que demuestran el fallo deben convertirse en pruebas que exijan el comportamiento seguro antes de corregir cada causa por separado.


## Correcciones implementadas — 25 de septiembre de 2026

Esta sección actualiza el estado histórico de la auditoría anterior. Cambios locales, todavía sin desplegar:

- Autoasignación de chofer: solo notas sin chofer; repetir una nota propia es idempotente. Notas de otro chofer devuelven 403. La lectura y asignación están en una transacción con bloqueo de fila. Oficina conserva la reasignación.
- Aislamiento de frontend: los almacenes de notas, cotizaciones, clientes, inventario, equipo, dashboard y rutas se reinician al cambiar/cerrar sesión. El interceptor descarta respuestas de sesiones anteriores, limpia cachés ante 403 y evita que un 401 anterior cierre la nueva sesión. El guard redirige choferes a su ruta.
- Caducidad de tokens: **3600 segundos (una hora)**, elegida por el usuario. El servidor rechaza el token vencido en la siguiente petición; la actividad no renueva su duración. Login reemplaza tokens vencidos. No se migró localStorage a cookies. `AUTH_TOKEN_TTL_SECONDS` permite configuración; al desplegar verificar que no exista una variable que sobreescriba 3600. Tokens existentes con más de una hora serán rechazados.
- Cuotas compartidas por base de datos: login/registro por IP; optimización, restricciones, asignación y documentos por usuario. Respuesta 429 con Retry-After. Exportación masiva permite 5 solicitudes/minuto y 20/día, para permitir descargar las tres secciones. Los límites diarios son configurables en settings. Ventanas fijas, no presupuesto global del proveedor ni límite de concurrencia.
- Dependencias: sqlparse 0.6.0 fijado en requirements y pip 26.2.1 en construcción Docker; entorno local actualizado. Consulta OSV posterior: 0 avisos para 13 dependencias directas y 38 paquetes instalados. No equivale a auditoría de la imagen ni a un lock completo de transitivas.

### Validación y despliegue

- Suite backend: 99 pruebas aprobadas con `config.test_settings` (base temporal).
- Suite frontend: 30 pruebas aprobadas; build de producción correcto. Persisten advertencias preexistentes de presupuesto CSS.
- `pip check`, `makemigrations --check --dry-run` y `git diff --check`: correctos.
- Evidencia posterior en `audits/2026-09-25/remediation-python-*.json`.
- Aplicar **migración 0018_securityratebucket** antes de servir tráfico. El arranque actual de Gunicorn intenta migrar, pero captura errores; verificar explícitamente que 0018 se haya aplicado. No se ejecutaron migraciones en la base real ni un despliegue.

Pendiente de infraestructura: validar identificación de IP detrás del proxy, concurrencia sobre PostgreSQL real, presupuestos globales del proveedor, CSP de scripts, usuario no-root del contenedor e imagen/despliegue. Estos controles no se dan por corregidos ni verificados por las pruebas locales.
