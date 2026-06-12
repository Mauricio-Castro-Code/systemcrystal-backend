import os

bind = f"0.0.0.0:{os.environ.get('PORT', '8000')}"
workers = 2


def on_starting(server):
    """Aplica migraciones pendientes al arrancar.

    Railway/Docker solo ejecuta gunicorn (no corre `migrate`), asi que lo
    hacemos aqui una vez en el proceso maestro, antes de levantar los workers.
    """
    import django

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    django.setup()

    from django.core.management import call_command

    try:
        call_command("migrate", "--noinput")
    except Exception as error:  # noqa: BLE001 - no debe tumbar el arranque
        server.log.error("Error al aplicar migraciones en el arranque: %s", error)
