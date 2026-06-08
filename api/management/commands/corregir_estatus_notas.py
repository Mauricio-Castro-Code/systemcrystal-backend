"""
Corrige el estatus de notas importadas con fecha de evento igual o posterior a hoy.

Las notas que fueron importadas como RECOGIDO/COBRADO pero cuyo evento es hoy
o en el futuro se cambian a PROGRAMADA / AL_CORRIENTE para que aparezcan
en Notas Activas.

Uso:
    python manage.py corregir_estatus_notas
    python manage.py corregir_estatus_notas --dry-run
    python manage.py corregir_estatus_notas --desde 2026-05-14
"""

from __future__ import annotations

import datetime

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from api.models import Order, OrderWorkflowEvent


class Command(BaseCommand):
    help = "Cambia a PROGRAMADA/AL_CORRIENTE las notas importadas con evento futuro."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            default=False,
            help="Muestra cuantas notas se corregiran sin modificar la base de datos.",
        )
        parser.add_argument(
            "--desde",
            type=str,
            default=None,
            help="Fecha de corte YYYY-MM-DD (default: hoy). "
                 "Notas con evento >= esta fecha se activan.",
        )

    def handle(self, *args, **options):
        dry_run: bool = options["dry_run"]
        desde_raw: str | None = options["desde"]

        if desde_raw:
            try:
                cutoff = datetime.date.fromisoformat(desde_raw)
            except ValueError:
                self.stderr.write(
                    self.style.ERROR(f"Fecha invalida: '{desde_raw}'. Usa YYYY-MM-DD.")
                )
                return
        else:
            cutoff = datetime.date.today()

        self.stdout.write(f"\nFecha de corte : {cutoff}")
        self.stdout.write(
            "Buscando notas con evento >= corte que esten como RECOGIDO o COBRADO...\n"
        )

        orders_to_fix = list(
            Order.objects.select_related("quotation").filter(
                quotation__event_date__gte=cutoff,
                operational_status=Order.OperationalStatus.RECOGIDO,
            )
        )

        if not orders_to_fix:
            self.stdout.write(
                self.style.SUCCESS(
                    "No hay notas que corregir. Todo esta en orden."
                )
            )
            return

        self.stdout.write(f"Notas a corregir: {len(orders_to_fix)}\n")

        for order in orders_to_fix:
            event_date = order.quotation.event_date
            self.stdout.write(
                f"  {order.order_id:<12}  evento={event_date}  "
                f"{order.operational_status} / {order.billing_status}"
                f"  →  PROGRAMADA / AL_CORRIENTE"
            )

        if dry_run:
            self.stdout.write(
                self.style.WARNING(
                    "\nMODO SIMULACION — no se modifico nada."
                )
            )
            return

        fixed = 0
        with transaction.atomic():
            for order in orders_to_fix:
                prev_op = order.operational_status
                prev_billing = order.billing_status

                order.operational_status = Order.OperationalStatus.PROGRAMADA
                order.billing_status = Order.BillingStatus.AL_CORRIENTE
                order.save(update_fields=["operational_status", "billing_status", "updated_at"])

                OrderWorkflowEvent.objects.create(
                    order=order,
                    category=OrderWorkflowEvent.Category.OPERATIONAL,
                    from_status=prev_op,
                    to_status=Order.OperationalStatus.PROGRAMADA,
                    comment="Correccion automatica: evento futuro, se activa la nota.",
                )
                if prev_billing == Order.BillingStatus.COBRADO:
                    OrderWorkflowEvent.objects.create(
                        order=order,
                        category=OrderWorkflowEvent.Category.BILLING,
                        from_status=prev_billing,
                        to_status=Order.BillingStatus.AL_CORRIENTE,
                        comment="Correccion automatica: evento futuro, se reactiva cobranza.",
                    )

                fixed += 1

        self.stdout.write("")
        self.stdout.write(
            self.style.SUCCESS(f"Corregidas: {fixed} notas ahora aparecen en Notas Activas.")
        )
