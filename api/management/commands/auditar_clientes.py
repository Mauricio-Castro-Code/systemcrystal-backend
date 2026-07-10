"""
Cruza el client_name guardado en cada nota/cotización contra el nombre
del cliente FK asignado. Reporta los casos donde los apellidos no coinciden
(señal de que la asignación del import fue incorrecta).

Uso:
    python manage.py auditar_clientes
    python manage.py auditar_clientes --csv > auditoria.csv
"""

import csv
import sys
import unicodedata

from django.core.management.base import BaseCommand, CommandError

from api.models import Quotation


def _norm(text: str) -> str:
    """Minúsculas + sin acentos."""
    text = str(text or "").strip().lower()
    return "".join(
        c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn"
    )


def _tokens(text: str) -> set[str]:
    """Palabras significativas (>= 3 letras) del nombre normalizado."""
    return {w for w in _norm(text).split() if len(w) >= 3}


def _score(name_in_note: str, client_name: str) -> float:
    """
    Fracción de palabras de la nota que aparecen en el nombre del cliente.
    1.0 = coincidencia perfecta; 0.0 = sin palabras en común.
    """
    a = _tokens(name_in_note)
    b = _tokens(client_name)
    if not a:
        return 1.0
    return len(a & b) / len(a)


class Command(BaseCommand):
    help = "Detecta notas cuyo client_name no coincide con el cliente asignado."

    def add_arguments(self, parser):
        parser.add_argument(
            "--umbral",
            type=float,
            default=0.5,
            help="Score mínimo para considerar una asignación válida (0–1, default 0.5).",
        )
        parser.add_argument(
            "--csv",
            action="store_true",
            help="Salida en formato CSV (útil para redirigir a un archivo).",
        )
        parser.add_argument(
            "--todos",
            action="store_true",
            help="Incluye también las notas sin cliente asignado (client=NULL).",
        )

    def handle(self, *args, **options):
        umbral: float = options["umbral"]
        as_csv: bool = options["csv"]
        include_null: bool = options["todos"]

        qs = (
            Quotation.objects.select_related("client")
            .order_by("quotation_id")
        )

        rows = []
        for q in qs:
            if q.client is None:
                if include_null:
                    rows.append({
                        "folio": q.quotation_id,
                        "nombre_nota": q.client_name,
                        "cliente_asignado": "—",
                        "codigo_cliente": "—",
                        "score": "—",
                        "problema": "Sin cliente asignado",
                    })
                continue

            score = _score(q.client_name, q.client.client_name)
            if score < umbral:
                rows.append({
                    "folio": q.quotation_id,
                    "nombre_nota": q.client_name,
                    "cliente_asignado": q.client.client_name,
                    "codigo_cliente": q.client.code,
                    "score": f"{score:.2f}",
                    "problema": "Nombre no coincide",
                })

        if not rows:
            self.stdout.write(self.style.SUCCESS(
                f"✓ No se encontraron inconsistencias (umbral={umbral})."
            ))
            return

        if as_csv:
            writer = csv.DictWriter(
                sys.stdout,
                fieldnames=["folio", "nombre_nota", "cliente_asignado", "codigo_cliente", "score", "problema"],
            )
            writer.writeheader()
            writer.writerows(rows)
        else:
            self.stdout.write(
                self.style.WARNING(f"\n⚠  {len(rows)} nota(s) con asignación sospechosa:\n")
            )
            col = "{:<14} {:<30} {:<30} {:<12} {:<6} {}"
            self.stdout.write(col.format(
                "FOLIO", "NOMBRE EN NOTA", "CLIENTE ASIGNADO", "CÓDIGO", "SCORE", "PROBLEMA"
            ))
            self.stdout.write("─" * 110)
            for r in rows:
                self.stdout.write(col.format(
                    r["folio"],
                    r["nombre_nota"][:29],
                    r["cliente_asignado"][:29],
                    r["codigo_cliente"],
                    r["score"],
                    r["problema"],
                ))
            self.stdout.write("")
