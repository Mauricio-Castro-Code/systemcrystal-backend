# Respaldo diario a Google Drive

## Qué está preparado

La tarea `.github/workflows/daily-backup.yml` está programada a las **18:00 de
America/Mexico_City** todos los días y permite ejecución manual. Se ejecuta en
GitHub Actions, consulta PostgreSQL directamente y no depende de que Railway o
el frontend estén funcionando. GitHub puede retrasar el inicio por carga; no es
un servicio con garantía de ejecución al segundo.

**No queda activa hasta configurar las dos credenciales, validar una copia y
establecer `BACKUPS_ENABLED=true`.** No basta con disponer del enlace de Drive.

Destino elegido: [carpeta de respaldos](https://drive.google.com/drive/folders/1xUIt7SbjkMYWIRbIAK7qk41Ewzatoca1).

Cada ejecución crea un ZIP nuevo, por ejemplo:

```text
Respaldo_Crystal_2026-09-17_18-00-00_123456.zip
  Clientes.xlsx            (Clientes y Direcciones)
  Registro_Notas.xlsx      (Notas, Artículos, Costos extra e Historial)
  Notas/
    0001-26__123.xlsx
    0002-26__124.xlsx
  Resumen.json             (fecha, cantidades, índice y hashes SHA-256)
```

Incluye todas las notas: activas, recogidas y canceladas. El registro identifica
sus estados. Se respeta la plantilla de nota; si una nota heredada supera sus 21
renglones, se conserva íntegra en formato tabular. El identificador interno añadido
al nombre evita colisiones entre folios malformados. Los archivos se consultan
sin la app y los saldos se guardan como valores, sin depender de recalcular fórmulas.

Esto es un **respaldo de consulta**, no un respaldo completo de PostgreSQL. No incluye
contraseñas, tokens, cotizaciones sin confirmar ni toda la configuración/inventario
del sistema. No sirve por sí solo para una restauración automática de la aplicación.

## 1. Autorizar Google Drive

Funciona tanto con una cuenta personal como con una cuenta Workspace mediante OAuth.
No se utiliza una cuenta de servicio: una cuenta de servicio no tiene cuota propia
para subir archivos a un Drive personal.

1. En [Google Cloud Console](https://console.cloud.google.com/), elegir un proyecto propio
   y habilitar **Google Drive API**.
2. Configurar Google Auth Platform / consentimiento y crear un cliente OAuth de tipo
   **Aplicación de escritorio**. Descargar su JSON a una ubicación privada local,
   fuera del repositorio. No enviarlo por chat ni subirlo a Git.
3. Configurar la publicación adecuada para uso continuo. Con una app externa en
   **Testing**, Google normalmente expira el refresh token a los 7 días. Para uso
   permanente, revisar los requisitos de publicación/verificación de Google, o
   usar una app interna cuando la organización Workspace lo permita.
4. Ejecutar localmente desde el backend:

```bash
venv/bin/python scripts/authorize_backup_drive.py \
  --client-config /ruta/privada/cliente-oauth.json \
  --output var/backup-google-oauth.json
```

El navegador pide consentimiento al propietario de la carpeta. **El scope de Drive
solicitado permite acceso amplio al Drive de esa cuenta**, necesario aquí para usar
una carpeta preexistente por ID sin Google Picker. El código utiliza exclusivamente
la carpeta configurada, pero la credencial en sí tiene más alcance. Preferir una
cuenta dedicada a respaldos o no concederlo si ese alcance no es aceptable; una
integración posterior con Picker puede limitarlo al scope `drive.file`.

La carpeta debe estar restringida a personas concretas, con permiso de escritura
para la cuenta autorizada. El exportador rechaza acceso público o compartido con
todo un dominio. No modifica los permisos de la carpeta ni crea enlaces públicos.

El archivo de autorización se crea con permisos `0600`, sin imprimir su contenido,
mediante OAuth con PKCE y callback exclusivo en `127.0.0.1`. Su directorio `var/`
queda excluido de Git y Docker.

## 2. Conexión PostgreSQL de solo lectura

Crear o proporcionar una conexión dedicada con `SELECT` únicamente sobre las tablas
necesarias. Utilizar la conexión directa compatible con el runner o el **Session pooler**
de Supabase, con TLS. No utilizar la clave `service_role` de REST como URL PostgreSQL.

Ejemplo de permisos para un rol ya creado, sin incluir contraseñas en el script:

```sql
GRANT CONNECT ON DATABASE postgres TO crystal_backup;
GRANT USAGE ON SCHEMA public TO crystal_backup;
GRANT SELECT ON
  public.api_client,
  public.api_clientaddress,
  public.api_order,
  public.api_quotation,
  public.api_quotationitem,
  public.api_orderextracost,
  public.api_orderworkflowevent
TO crystal_backup;
ALTER ROLE crystal_backup SET default_transaction_read_only = on;
```

La configuración `config.backup_settings` no lee `.env`, no utiliza credenciales de
sesiones web, exige `BACKUP_DATABASE_URL`, requiere TLS y activa transacciones de
solo lectura. La instantánea de datos usa `REPEATABLE READ` en PostgreSQL para que
clientes, registros y artículos coincidan aunque la app siga en uso.

## 3. Configurar GitHub

En el repositorio **systemcrystal-backend**, Settings → Secrets and variables → Actions:

| Tipo | Nombre | Contenido |
| --- | --- | --- |
| Secret | `BACKUP_DATABASE_URL` | URL PostgreSQL del usuario de lectura |
| Secret | `BACKUP_GOOGLE_OAUTH_JSON` | Contenido del archivo OAuth generado localmente |
| Variable | `BACKUP_DRIVE_FOLDER_ID` | `1xUIt7SbjkMYWIRbIAK7qk41Ewzatoca1` |
| Variable | `BACKUPS_ENABLED` | `true` para activar; `false` mientras se configura |

Ejemplo para enviar el archivo privado a GitHub Secrets sin imprimirlo:

```bash
gh secret set BACKUP_GOOGLE_OAUTH_JSON \
  --repo Mauricio-Castro-Code/systemcrystal-backend < var/backup-google-oauth.json
```

Para la URL de PostgreSQL, `gh secret set BACKUP_DATABASE_URL --repo
Mauricio-Castro-Code/systemcrystal-backend` solicita su valor de forma interactiva.
No escribirlo en comandos que queden en el historial ni en archivos versionados.

## 4. Validar y activar

1. Completar los secretos y poner `BACKUPS_ENABLED=true`.
2. En Actions → **Respaldo diario de clientes y notas** → Run workflow.
3. Confirmar el resultado exitoso y abrir el ZIP en la carpeta elegida. Comparar las
   cantidades de `Resumen.json` y comprobar una nota y un cliente conocidos.
4. Si falla, revisar permisos, espacio y conectividad; no considerar el respaldo
   operativo hasta terminar una copia real. Se puede devolver `BACKUPS_ENABLED=false`.
5. Configurar las notificaciones de fallos de Actions para el propietario. El código
   no envía correos ni mensajes adicionales. Revisar periódicamente que existe una
   copia reciente; una tarea que no llega a iniciarse no produce un fallo de ejecución.

GitHub permite retrasos y en repositorios públicos puede desactivar cron tras 60 días
sin actividad. Revisar también cuotas/minutos de Actions y cuota de Drive.

## Retención y seguridad

- Cada ZIP tiene fecha local e identificador temporal, sin sobrescribir copias anteriores.
- No hay eliminación automática; la retención se decidirá antes de implementar borrados.
- Los ZIP no tienen contraseña. El acceso se controla en Drive; contienen información
  privada de clientes, por lo que no deben compartirse públicamente.
- No se publican archivos de respaldo como artifacts de GitHub, en el repositorio o
  en logs. Los temporales se eliminan al finalizar el comando, incluso ante errores.
- La subida usa una sesión resumable, comprueba tamaño y MD5 del archivo remoto y no
  declara éxito si no coinciden. Una interrupción hace fallar la tarea para permitir
  repetirla; no elimina copias antiguas. Los SHA-256 internos permiten verificar los Excel.
- Si Supabase falla durante la lectura, no se crea una copia nueva. Las anteriores
  permanecen en Drive. Con frecuencia diaria, se puede perder hasta un día de cambios,
  o más si varias ejecuciones fallan; por eso es importante supervisarlas.

## Pruebas locales

```bash
venv/bin/python manage.py test api --settings=config.test_settings --noinput
```

Para generar únicamente en una carpeta local (usar una conexión de lectura válida):

```bash
venv/bin/python manage.py backup_to_drive --settings=config.backup_settings \
  --local-only --output-dir var/backups
```

Referencias: [cron y sus limitaciones](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule),
[OAuth y expiración](https://developers.google.com/identity/protocols/oauth2),
[subidas a Drive](https://developers.google.com/workspace/drive/api/guides/manage-uploads).
