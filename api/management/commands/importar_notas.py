"""
Importa notas desde una carpeta de archivos Excel (.xls) a la base de datos.

Uso:
    python manage.py importar_notas /ruta/a/NOTAS_2026/

Flags opcionales:
    --dry-run                       Solo muestra lo que se importaria, sin guardar.
    --estatus-operativo RECOGIDO    Estatus operativo para notas con fecha de evento pasada
                                    (default: RECOGIDO).
    --estatus-cobro     COBRADO     Estatus de cobro para notas con fecha de evento pasada
                                    (default: COBRADO).

Logica automatica de estatus:
    Si la fecha de evento es hoy o futura  → PROGRAMADA + AL_CORRIENTE (notas activas).
    Si la fecha de evento es pasada o nula → usa --estatus-operativo / --estatus-cobro.
"""

from __future__ import annotations

import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from api.models import (
    Client,
    DocumentSequence,
    Order,
    OrderWorkflowEvent,
    Quotation,
    QuotationItem,
    only_digits,
)


# ── Helpers de lectura segura ──────────────────────────────────────────────────


def safe_str(value) -> str:
    if value is None:
        return ""
    return str(value).strip()


def safe_date(value) -> datetime.date | None:
    if value is None:
        return None
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    return None


def safe_decimal(value) -> Decimal:
    if value is None or value == "":
        return Decimal("0")
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def safe_int(value) -> int:
    if value is None or value == "":
        return 0
    try:
        return int(float(str(value)))
    except (ValueError, TypeError):
        return 0


# ── Lector de celdas del Excel (template original Crystal) ────────────────────
#
# Los datos estan en la hoja "Hoja3" (indice 4, 0-based). Mapa:
#   Nombre        B9  (8,1)   Domicilio  B10 (9,1)   Telefono  I10 (9,8)
#   Colonia       B11 (10,1)  Referencias B12 (11,1)  FechaElab I12 (11,8)
#   F.entrega     B15 (14,1)  Folio       I15 (14,8)
#   F.evento      B16 (15,1)  F.devol.    B17 (16,1)
#   Instrucciones B45 (44,1)
#   Subtotal J44(43,9) Flete J45(44,9) IVA J46(45,9) Deposito J47(46,9)
#   Total J48(47,9)  Descuento J49(48,9)  Anticipo J50(49,9)
#   Items filas 20-42 (0-idx), cols B=1 C=2 I=8 J=9


class NotaExcelReader:
    """Lee la hoja Hoja3 del template original de nota de Crystal (.xls via xlrd)."""

    def __init__(self, path: Path):
        self.path = path

    def read(self) -> dict | None:
        if self.path.suffix.lower() not in (".xls", ".xlsx"):
            return None
        try:
            import xlrd
        except ImportError:
            return None

        try:
            wb = xlrd.open_workbook(str(self.path))
        except Exception:
            return None

        # Hoja3 esta en el indice 4; buscar por nombre como respaldo
        ws = None
        for idx in range(wb.nsheets - 1, -1, -1):
            sheet = wb.sheets()[idx]
            if sheet.name == "Hoja3":
                ws = sheet
                break
        if ws is None and wb.nsheets >= 5:
            ws = wb.sheets()[4]
        if ws is None:
            return None

        def cv(row, col):
            try:
                ct = ws.cell_type(row, col)
                if ct == 0:  # XL_CELL_EMPTY
                    return None
                return ws.cell_value(row, col)
            except IndexError:
                return None

        def date_cell(row, col):
            try:
                import xlrd as _x
                ct = ws.cell_type(row, col)
                val = ws.cell_value(row, col)
                if ct == _x.XL_CELL_DATE:
                    return _x.xldate_as_datetime(val, wb.datemode).date()
                if ct == _x.XL_CELL_NUMBER and val > 1000:
                    return _x.xldate_as_datetime(val, wb.datemode).date()
                return None
            except Exception:
                return None

        client_name  = safe_str(cv(8, 1))   # B9
        address      = safe_str(cv(9, 1))   # B10
        phone        = safe_str(cv(9, 8))   # I10
        neighborhood = safe_str(cv(10, 1))  # B11
        reference    = safe_str(cv(11, 1))  # B12
        note_date    = date_cell(11, 8)     # I12

        delivery_date   = date_cell(14, 1)     # B15
        folio           = safe_str(cv(14, 8))  # I15
        event_date      = date_cell(15, 1)     # B16
        collection_date = date_cell(16, 1)     # B17

        if not folio or "-" not in folio:
            folio = self.path.stem  # "0001-26" desde el nombre del archivo

        # Localizar la fila del "Subtotal" dinamicamente (el template tiene 2 versiones:
        # una con subtotal en J42 y otra en J44 segun la cantidad de articulos).
        subtotal_row = None
        for r in range(38, 58):
            v = ws.cell_value(r, 8) if ws.cell_type(r, 8) == 1 else ""
            if "Subtotal" in str(v):
                subtotal_row = r
                break

        if subtotal_row is not None:
            # Instrucciones de entrega estan en la fila siguiente al subtotal, col B
            delivery_instructions = safe_str(cv(subtotal_row + 1, 1))
            subtotal         = safe_decimal(cv(subtotal_row,     9))
            freight          = safe_decimal(cv(subtotal_row + 1, 9))
            tax_amount       = safe_decimal(cv(subtotal_row + 2, 9))
            security_deposit = safe_decimal(cv(subtotal_row + 3, 9))
            total_estimated  = safe_decimal(cv(subtotal_row + 4, 9))
            discount         = safe_decimal(cv(subtotal_row + 5, 9))
            advance_payment  = safe_decimal(cv(subtotal_row + 6, 9))
        else:
            delivery_instructions = ""
            subtotal = freight = tax_amount = security_deposit = Decimal("0")
            total_estimated = discount = advance_payment = Decimal("0")

        # Items: desde fila 20 hasta la fila del subtotal (exclusive)
        item_end = subtotal_row if subtotal_row is not None else 43
        items = []
        for row in range(20, item_end):
            qty   = safe_int(cv(row, 1))   # B
            equip = safe_str(cv(row, 2))   # C
            if not qty and not equip:
                continue
            items.append({
                "quantity":   qty,
                "equipment":  equip,
                "unit_price": safe_decimal(cv(row, 8)),  # I
                "total":      safe_decimal(cv(row, 9)),  # J
            })

        return {
            "folio":                 folio,
            "client_name":           client_name,
            "phone":                 phone,
            "address":               address,
            "neighborhood":          neighborhood,
            "reference":             reference,
            "birth_date":            None,
            "note_date":             note_date,
            "delivery_date":         delivery_date,
            "event_date":            event_date,
            "collection_date":       collection_date,
            "delivery_instructions": delivery_instructions,
            "subtotal":              subtotal,
            "freight":               freight,
            "tax_amount":            tax_amount,
            "security_deposit":      security_deposit,
            "total_estimated":       total_estimated,
            "discount":              discount,
            "advance_payment":       advance_payment,
            "items":                 items,
        }


# ── Comando ────────────────────────────────────────────────────────────────────


class Command(BaseCommand):
    help = "Importa notas desde una carpeta de archivos Excel al sistema."

    def add_arguments(self, parser):
        parser.add_argument(
            "carpeta",
            type=str,
            help="Ruta a la carpeta que contiene los archivos .xlsx de notas.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            default=False,
            help="Muestra lo que se importaria sin guardar nada en la base de datos.",
        )
        parser.add_argument(
            "--estatus-operativo",
            type=str,
            default=Order.OperationalStatus.RECOGIDO,
            choices=[s.value for s in Order.OperationalStatus],
            help=(
                "Estatus operativo para las notas importadas. "
                "Default: RECOGIDO (historico completado)."
            ),
        )
        parser.add_argument(
            "--estatus-cobro",
            type=str,
            default=Order.BillingStatus.COBRADO,
            choices=[s.value for s in Order.BillingStatus],
            help=(
                "Estatus de cobro para las notas importadas. "
                "Default: COBRADO."
            ),
        )

    def handle(self, *args, **options):
        folder = Path(options["carpeta"])
        dry_run: bool = options["dry_run"]
        op_status: str = options["estatus_operativo"]
        billing_status: str = options["estatus_cobro"]

        if not folder.exists() or not folder.is_dir():
            raise CommandError(
                f"La carpeta '{folder}' no existe o no es un directorio."
            )

        excel_files = sorted(
            list(folder.glob("*.xlsx")) + list(folder.glob("*.xls")),
            key=lambda p: p.name,
        )

        if not excel_files:
            self.stdout.write(
                self.style.WARNING("No se encontraron archivos .xlsx en la carpeta.")
            )
            return

        self.stdout.write(
            f"\nArchivos .xlsx encontrados: {len(excel_files)}"
        )
        self.stdout.write(
            f"Estatus operativo: {op_status}  |  Estatus cobro: {billing_status}"
        )

        if dry_run:
            self.stdout.write(
                self.style.WARNING(
                    "MODO SIMULACION — no se guardara nada en la base de datos.\n"
                )
            )
        else:
            self.stdout.write("")

        imported = 0
        skipped = 0
        errors = 0
        max_sequence: dict[str, int] = {}

        existing_ids: set[str] = set(Order.objects.values_list("order_id", flat=True))

        for excel_path in excel_files:
            if excel_path.stem in existing_ids:
                skipped += 1
                self.stdout.write(
                    f"  EXISTE {excel_path.stem} — ya esta en la base de datos, omitiendo"
                )
                continue

            try:
                result, year_suffix, seq_value = self._process_file(
                    excel_path, dry_run, op_status, billing_status, existing_ids
                )
            except Exception as exc:
                errors += 1
                self.stdout.write(
                    self.style.ERROR(f"  ERROR  {excel_path.name}: {exc}")
                )
                continue

            if result == "imported":
                imported += 1
                if year_suffix and seq_value:
                    if max_sequence.get(year_suffix, 0) < seq_value:
                        max_sequence[year_suffix] = seq_value
            elif result == "skipped":
                skipped += 1

        if not dry_run and max_sequence:
            self._sync_sequences(max_sequence)

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(f"Importadas : {imported}"))
        self.stdout.write(f"Omitidas   : {skipped}")
        if errors:
            self.stdout.write(self.style.ERROR(f"Errores    : {errors}"))
        self.stdout.write("")

        if imported and not dry_run:
            self.stdout.write(
                self.style.SUCCESS(
                    "Listo. Recarga el dashboard para ver las notas importadas."
                )
            )

    def _resolve_statuses(
        self,
        event_date: datetime.date | None,
        fallback_op: str,
        fallback_billing: str,
    ) -> tuple[str, str]:
        """Devuelve (op_status, billing_status) segun la fecha del evento."""
        today = datetime.date.today()
        if event_date is not None and event_date >= today:
            return Order.OperationalStatus.PROGRAMADA, Order.BillingStatus.AL_CORRIENTE
        return fallback_op, fallback_billing

    def _process_file(
        self,
        path: Path,
        dry_run: bool,
        op_status: str,
        billing_status: str,
        existing_ids: set[str] | None = None,
    ) -> tuple[str, str, int]:
        """Retorna (resultado, year_suffix, seq_value)."""

        data = NotaExcelReader(path).read()

        if data is None:
            self.stdout.write(
                self.style.WARNING(
                    f"  SKIP   {path.name}: no se pudo leer (archivo corrupto o formato invalido)"
                )
            )
            return "skipped", "", 0

        folio = data["folio"]
        client_name = data["client_name"]

        if not folio:
            self.stdout.write(
                self.style.WARNING(
                    f"  SKIP   {path.name}: sin folio y nombre de archivo invalido"
                )
            )
            return "skipped", "", 0

        if not client_name:
            self.stdout.write(
                self.style.WARNING(
                    f"  SKIP   {path.name}: celda B9 vacia, sin nombre de cliente"
                )
            )
            return "skipped", "", 0

        if existing_ids is not None and folio in existing_ids:
            self.stdout.write(
                f"  EXISTE {folio} — ya esta en la base de datos, omitiendo"
            )
            return "skipped", "", 0

        if existing_ids is None and Order.objects.filter(order_id=folio).exists():
            self.stdout.write(
                f"  EXISTE {folio} — ya esta en la base de datos, omitiendo"
            )
            return "skipped", "", 0

        year_suffix, seq_value = self._parse_folio(folio)

        resolved_op, resolved_billing = self._resolve_statuses(
            data["event_date"], op_status, billing_status
        )

        if dry_run:
            self.stdout.write(
                f"  OK     {folio} — {client_name}"
                f"  ({data['phone']})"
                f"  ${data['total_estimated']:,.0f}"
                f"  {len(data['items'])} art."
                f"  [{resolved_op} / {resolved_billing}]"
            )
            return "imported", year_suffix, seq_value

        resolved_op, resolved_billing = self._resolve_statuses(
            data["event_date"], op_status, billing_status
        )

        with transaction.atomic():
            client = self._find_or_create_client(data)
            self._create_order(data, client, folio, resolved_op, resolved_billing)

        self.stdout.write(f"  OK     {folio} — {client_name}")
        return "imported", year_suffix, seq_value

    def _parse_folio(self, folio: str) -> tuple[str, int]:
        """Extrae el year_suffix y valor numerico del folio '0582-26' -> ('26', 582)."""
        parts = folio.split("-")
        if (
            len(parts) == 2
            and parts[0].isdigit()
            and parts[1].isdigit()
            and len(parts[1]) <= 2  # evitar year_suffix invalido (ej. '265')
        ):
            return parts[1], int(parts[0])
        return "", 0

    def _find_or_create_client(self, data: dict) -> Client:
        """Busca cliente por telefono (digitos exactos). Si no existe, crea uno nuevo."""
        phone_digits = only_digits(data["phone"])

        if phone_digits:
            client = Client.objects.filter(phone_digits=phone_digits).first()
            if client:
                return client

        return Client.objects.create(
            client_name=data["client_name"],
            contact_person=data["client_name"],
            phone_number=data["phone"],
            address=", ".join(
                p for p in [data["address"], data["neighborhood"]] if p
            ),
        )

    def _create_order(
        self,
        data: dict,
        client: Client,
        folio: str,
        op_status: str,
        billing_status: str,
    ) -> Order:
        note_date = data["note_date"]
        if note_date:
            confirmed_at = timezone.make_aware(
                datetime.datetime.combine(note_date, datetime.time.min)
            )
        else:
            confirmed_at = timezone.now()

        quotation = Quotation(
            client=client,
            status=Quotation.Status.CONFIRMED,
            client_name=data["client_name"],
            phone_number=data["phone"],
            address=data["address"],
            neighborhood=data["neighborhood"],
            reference=data["reference"],
            delivery_instructions=data["delivery_instructions"],
            birth_date=data["birth_date"],
            delivery_date=data["delivery_date"],
            event_date=data["event_date"],
            collection_date=data["collection_date"],
            freight=data["freight"],
            apply_tax=data["tax_amount"] > 0,
            tax_amount=data["tax_amount"],
            security_deposit=data["security_deposit"],
            discount=data["discount"],
            advance_payment=data["advance_payment"],
            subtotal=data["subtotal"],
            total_estimated=data["total_estimated"],
        )
        quotation.save()

        if data["items"]:
            QuotationItem.objects.bulk_create([
                QuotationItem(
                    quotation=quotation,
                    quantity=item["quantity"],
                    equipment=item["equipment"],
                    unit_price=item["unit_price"],
                    total=item["total"],
                )
                for item in data["items"]
            ])

        order = Order(
            order_id=folio,
            quotation=quotation,
            operational_status=op_status,
            billing_status=billing_status,
            confirmed_at=confirmed_at,
        )
        order.save()

        OrderWorkflowEvent.objects.create(
            order=order,
            category=OrderWorkflowEvent.Category.OPERATIONAL,
            to_status=op_status,
            comment="Importado desde archivo Excel historico.",
        )
        OrderWorkflowEvent.objects.create(
            order=order,
            category=OrderWorkflowEvent.Category.BILLING,
            to_status=billing_status,
            comment="Importado desde archivo Excel historico.",
        )

        return order

    def _sync_sequences(self, max_sequence: dict[str, int]) -> None:
        """Actualiza los contadores de folio para que no haya colision con los importados."""
        for year_suffix, max_value in max_sequence.items():
            seq, _ = DocumentSequence.objects.get_or_create(
                key=DocumentSequence.Key.ORDER,
                year_suffix=year_suffix,
                defaults={"last_value": max_value},
            )
            if seq.last_value < max_value:
                seq.last_value = max_value
                seq.save(update_fields=["last_value"])
            self.stdout.write(
                f"Secuencia {year_suffix}: contador actualizado a {max_value}"
            )
