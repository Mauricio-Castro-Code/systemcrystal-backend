"""
Crea los clientes faltantes y reasigna las notas mal asignadas detectadas
en la auditoría del 2026-07-10.

Casos corregidos:
  - ALEJANDRA FREGOSO: 994COTI-26, 2912COTI-26  →  asignadas a CLI-053 (Alejandra González)
  - RICARDO BARRIENTOS: 2603COTI-26             →  asignada a CLI-355 (Angélica Valencia)

Uso:
    python manage.py corregir_asignaciones           # dry-run (solo muestra qué haría)
    python manage.py corregir_asignaciones --apply   # aplica los cambios
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from api.models import Client, Quotation


FIXES = [
    {
        "client_name": "ALEJANDRA FREGOSO",
        "folios": ["994COTI-26", "2912COTI-26"],
    },
    {
        "client_name": "RICARDO BARRIENTOS",
        "folios": ["2603COTI-26"],
    },
]


class Command(BaseCommand):
    help = "Crea clientes faltantes y reasigna notas mal asignadas."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Aplica los cambios. Sin esta bandera solo muestra el plan.",
        )

    def handle(self, *args, **options):
        apply: bool = options["apply"]

        if not apply:
            self.stdout.write(self.style.WARNING(
                "\n[DRY-RUN] No se guardará nada. Agrega --apply para aplicar.\n"
            ))

        with transaction.atomic():
            for fix in FIXES:
                client_name = fix["client_name"]
                folios = fix["folios"]

                # Verificar que el cliente no existe ya
                existing = Client.objects.filter(client_name__iexact=client_name).first()
                if existing:
                    self.stdout.write(
                        f"  · Cliente '{client_name}' ya existe ({existing.code}) — se usará ese."
                    )
                    client = existing
                else:
                    self.stdout.write(
                        f"  + Crear cliente: '{client_name}'"
                    )
                    if apply:
                        client = Client.objects.create(client_name=client_name)
                        self.stdout.write(self.style.SUCCESS(
                            f"    ✓ Creado como {client.code}"
                        ))
                    else:
                        client = None

                for folio in folios:
                    try:
                        q = Quotation.objects.select_related("client").get(quotation_id=folio)
                    except Quotation.DoesNotExist:
                        self.stdout.write(self.style.ERROR(f"  ✗ No se encontró la nota {folio}"))
                        continue

                    old_name = q.client.client_name if q.client else "—"
                    old_code = q.client.code if q.client else "—"
                    self.stdout.write(
                        f"  → {folio}: '{q.client_name}' | antes={old_name} ({old_code})"
                        + (f" | después={client_name} ({client.code})" if client else "")
                    )

                    if apply and client:
                        q.client = client
                        q.save(update_fields=["client"])
                        self.stdout.write(self.style.SUCCESS(f"    ✓ Reasignada"))

                self.stdout.write("")

            if not apply:
                transaction.set_rollback(True)

        if apply:
            self.stdout.write(self.style.SUCCESS("\nTodos los cambios aplicados correctamente."))
        else:
            self.stdout.write("\nEjecuta con --apply para aplicar los cambios.\n")
