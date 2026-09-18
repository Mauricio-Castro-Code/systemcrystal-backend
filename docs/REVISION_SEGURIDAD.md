# Revisión de código y seguridad de SystemCrystal

Revisión local de BackendCrystal y FrontendCrystal iniciada el 17 de septiembre de 2026.
La validación descrita se realizó localmente, antes de publicar los cambios. No se
modificó la base de datos de Supabase ni el contenido de `.env` durante la revisión.

## Hallazgos corregidos

| Prioridad | Problema encontrado | Corrección |
| --- | --- | --- |
| Alta | Registro anónimo con clave predeterminada y opción de crear administradores | Se eliminó la clave predeterminada. Sin clave configurada el registro se rechaza; solo crea ventas. Se retiró el selector de administrador de la pantalla de registro. |
| Alta | Un chofer podía saltarse restricciones usando endpoints generales, incluida la actualización masiva de cobros | Permiso predeterminado de ventas/administrador. Sesión y operaciones propias del chofer mantienen permisos explícitos. |
| Alta | El servidor solicitaba cualquier URL guardada como enlace de Maps y seguía sus redirecciones | Lista exacta de dominios HTTPS permitidos, validación de cada salto, límite de redirecciones y cierre de respuestas sin descargar sus cuerpos. También se validan enlaces al guardarlos y presentarlos. |
| Alta | Dependencias con avisos de seguridad y Django sin soporte | Django 5.2.17 LTS, REST Framework 3.17.2, Pillow 12.3.0; Angular 21.2.23, herramientas Angular 21.2.24 y Vitest 4.1.11. Lockfile regenerado y verificado. |
| Alta | `COPY . .` podía incorporar `.env`, bases locales y otros datos privados a Docker | Nuevo `.dockerignore`. `collectstatic` se ejecuta sin cargar `.env` y sus fallos ya no se ignoran. No se inspeccionaron imágenes ya publicadas. |
| Alta | Un folio podía convertirse en ruta fuera del directorio temporal o incluir caracteres de script | Nombre de salida saneado independientemente del folio mostrado en el documento. |
| Media | Texto de usuario que empieza por `=` podía convertirse en fórmula de Excel | Escritura explícita como texto; las fórmulas de la plantilla siguen siendo fórmulas. |
| Media | Validadores de contraseña configurados pero no ejecutados por la API | Validación en registro, creación y edición de usuarios. Cambio de contraseña y desactivación revocan tokens. |
| Media | Edición de usuario guardaba cambios antes de rechazar un cambio de rol | Operación atómica, incluida la revocación de tokens. Se bloquea cambiar la contraseña de otro administrador según la regla existente de acceso. |
| Media | Login/registro sin límite básico y diferencia de procesamiento para cuentas inexistentes | Throttling de DRF; login siempre usa el backend de autenticación de Django. Este límite local no sustituye un control de fuerza bruta en la infraestructura. |
| Media | Valores de desarrollo inseguros al arrancar producción | `DEBUG=False` por defecto, clave secreta obligatoria de al menos 50 caracteres, HTTPS, cookies seguras y HSTS. Datos demo bloqueados fuera de DEBUG. |
| Media | Importación XLSX sin límite de tamaño | Máximo 5 MiB en la API, 25 MiB descomprimidos y 1.000 entradas ZIP antes de procesar el libro. |
| Media | Un 401 durante logout podía iniciar otro logout repetidamente | Limpieza local de sesión sin nueva solicitud; el interceptor solo actúa sobre la API propia. |
| Baja | Errores y registros podían exponer rutas internas o detalles de solicitudes externas | Mensajes genéricos al exportar, eliminación del log de conexión a BD y reducción de datos registrados al fallar Maps. |
| Baja | Faltaban cabeceras de protección en el frontend | `nosniff`, rechazo de framing, política de referrer y CSP parcial en Vercel. |

## Limpieza y correcciones funcionales

- Eliminados 22 archivos del frontend: copias `* 2`, lockfile duplicado, pantallas placeholder sin rutas activas, feature Productos desconectado, tabla de actividad sin uso y servicio local de clientes sustituido por la API.
- Se comprobó el grafo de imports desde `src/main.ts`, incluyendo rutas lazy. Se conservó `environment.prod.ts`, que se referencia desde la configuración de compilación.
- Eliminados `ClientSerializer` sin referencias, imports Python/TypeScript sin uso y un mensaje de depuración de PDF. Se reutiliza `IsAdminUser` de DRF.
- TypeScript ahora detecta declaraciones y parámetros sin uso.
- Las notas archivadas vuelven a limitarse a `RECOGIDO`, de acuerdo con la pantalla y las pruebas existentes.
- Se añadió compatibilidad del ordenamiento de folios con SQLite; PostgreSQL mantiene su ordenamiento previo. Para folios malformados el fallback SQLite utiliza su prefijo numérico y puede ordenar distinto a PostgreSQL.
- Pruebas aisladas con SQLite en memoria, sin leer `.env` ni contactar Supabase. Se corrigieron fixtures antiguos de administrador, contraseñas de registro similares al correo y el proveedor de service worker ausente en la prueba de Angular.

## Verificación

- Backend: **64 pruebas aprobadas**, incluidas 14 nuevas de seguridad.
- Frontend: **4 pruebas aprobadas**, incluidas 3 nuevas contra regresiones del interceptor.
- Compilación de producción Angular aprobada; TypeScript sin errores.
- Ruff, reglas F: sin errores. `git diff --check`: sin errores.
- `makemigrations --check --dry-run`: sin migraciones pendientes. `pip check`: verificación del entorno local.
- Auditoría npm: pasó de **32 paquetes afectados** (incluida una alerta crítica) a **0 vulnerabilidades conocidas** en el árbol actualizado.
- Auditoría Python: avisos en Django, REST Framework y Pillow; **0 avisos conocidos** al volver a resolver y auditar `requirements.txt` actualizado.
- `check --deploy` con configuración de producción sintética: sin errores; mantiene los avisos de HSTS para subdominios y preload. No se activan sin conocer todos los dominios afectados.
- Permanecen 5 advertencias de presupuesto de estilos SCSS. No impiden compilar.

La compilación de Angular se abortaba dentro del sandbox y pasó fuera de él.
La resolución de dependencias de npm 10 fallaba en `edgesOut`; se resolvió con npm
11.19.1 temporal, sin cambiar el npm global, y se actualizó `packageManager`.

## Antes de desplegar

1. Configurar una `DJANGO_SECRET_KEY` aleatoria fuerte. Revisar `.env.example` y el README del backend. Para desarrollo usar explícitamente `DJANGO_DEBUG=True` y desactivar la redirección HTTPS si se utiliza HTTP local.
2. Configurar `DJANGO_TRUST_PROXY_SSL_HEADER=True` únicamente cuando el proxy confiable reescriba `X-Forwarded-Proto`. Sin esa configuración, la terminación TLS en un proxy puede causar redirecciones repetidas. Configurar también `DJANGO_NUM_PROXIES` según el número real de proxies confiables.
3. Dejar `REGISTRATION_ACCESS_KEY` vacía para cerrar el registro o asignar una clave privada nueva. Crear el primer administrador con `createsuperuser`; la interfaz pública ya no concede ese rol.
4. Si alguna imagen anterior se construyó con `.env` en el contexto, revisar las imágenes y rotar las credenciales que contuvieran. Revisar también si existen cuentas demo o administradores creados por el registro anterior. No se inspeccionaron cuentas ni secretos de producción.
5. Establecer rate limiting en proxy/WAF y caché compartida. La caché local actual tiene contadores por proceso; los dos workers de Gunicorn no comparten esos contadores. Considerar límites específicos para optimización de rutas y exportación de documentos.
6. Probar en staging PostgreSQL, Docker, login/roles, importación/exportación real con LibreOffice y enlaces Maps antes de publicar.

## Riesgos y mejoras pendientes

- Los tokens DRF continúan sin vencimiento automático y el frontend los guarda en `localStorage`; un XSS podría extraerlos. La CSP añadida es parcial y no restringe todavía `script-src`. Migrar a cookies HttpOnly o tokens de corta duración requiere revisar autenticación, CSRF y despliegue conjuntamente.
- Docker todavía ejecuta la aplicación como root; conviene introducir un usuario dedicado y probar permisos de LibreOffice y archivos temporales en una imagen construida.
- No se probó el despliegue real, el proxy, las políticas de Supabase, la rotación de secretos, TLS, copias de seguridad ni todos los flujos en un navegador. Las auditorías de paquetes son una comprobación de avisos conocidos, no una garantía de ausencia de vulnerabilidades.
- Los listados amplios y módulos extensos pueden beneficiarse de paginación y separación de responsabilidades; no se reescribieron contratos de la aplicación durante esta limpieza.

## Referencias

- [Versiones soportadas de Django](https://www.djangoproject.com/download/): Django 5.0 ya no recibe correcciones; 5.2 es LTS.
- [Limitaciones del throttling de DRF](https://www.django-rest-framework.org/api-guide/throttling/): controles de aplicación no equivalen a protección garantizada contra fuerza bruta o denegación de servicio.
- Dependencias contrastadas con el registro npm mediante `npm audit` y con avisos consultados por `pip-audit`.
