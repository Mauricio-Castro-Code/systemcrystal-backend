"""
Diagnostico de plantillas Excel.

Genera un .xlsx "en bruto" (lo que openpyxl produce SIN nuestra restauracion
a nivel ZIP) y lo compara con la plantilla original para detectar que
archivos se estan perdiendo.

Uso:
    python manage.py diagnose_excel_template
    python manage.py diagnose_excel_template --template ruta/a/tu/Nota.xlsx
"""

from __future__ import annotations

import warnings
import zipfile
from collections import Counter
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Detecta que archivos pierde openpyxl al guardar la plantilla Excel."

    def add_arguments(self, parser):
        parser.add_argument(
            "--template",
            default=None,
            help="Ruta a la plantilla .xlsx (default: settings.NOTE_EXCEL_TEMPLATE_PATH)",
        )
        parser.add_argument(
            "--out-dir",
            default=None,
            help="Directorio para escribir el .xlsx de prueba (default: temporal)",
        )

    def handle(self, *args, **options):
        from openpyxl import load_workbook

        template_arg = options.get("template")
        template_path = Path(
            template_arg if template_arg else settings.NOTE_EXCEL_TEMPLATE_PATH,
        ).expanduser().resolve()

        if not template_path.exists():
            self.stderr.write(self.style.ERROR(
                f"No existe la plantilla: {template_path}",
            ))
            return

        out_dir_arg = options.get("out_dir")
        if out_dir_arg:
            out_dir = Path(out_dir_arg).expanduser().resolve()
            out_dir.mkdir(parents=True, exist_ok=True)
        else:
            import tempfile
            out_dir = Path(tempfile.mkdtemp(prefix="diagnose-excel-"))

        raw_output = out_dir / "raw_openpyxl_output.xlsx"

        self.stdout.write(f"Plantilla: {template_path}")
        self.stdout.write(f"Output crudo de openpyxl: {raw_output}\n")

        # Reproducir lo que hacemos en produccion: load + save sin restaurar
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            wb = load_workbook(str(template_path))
        wb.save(str(raw_output))
        wb.close()

        with zipfile.ZipFile(template_path, "r") as zt:
            template_files = set(zt.namelist())
        with zipfile.ZipFile(raw_output, "r") as zo:
            output_files = set(zo.namelist())

        only_template = sorted(template_files - output_files)
        only_output = sorted(output_files - template_files)

        self.stdout.write(self.style.WARNING(
            f"\n=== Archivos del template QUE OPENPYXL PERDIO ({len(only_template)}) ==="
        ))
        for f in only_template:
            self.stdout.write(f"  - {f}")

        if only_output:
            self.stdout.write(self.style.NOTICE(
                f"\n=== Archivos NUEVOS que openpyxl agrego ({len(only_output)}) ==="
            ))
            for f in only_output:
                self.stdout.write(f"  + {f}")

        carpetas: Counter[str] = Counter()
        for f in only_template:
            if "/" in f:
                carpetas[f.rsplit("/", 1)[0] + "/"] += 1

        if carpetas:
            self.stdout.write(self.style.WARNING(
                "\n=== Sugerencia: agrega estos prefijos a "
                "_TEMPLATE_PRESERVED_PREFIXES en api/excel_exports.py ==="
            ))
            for carpeta, count in carpetas.most_common():
                self.stdout.write(f"  '{carpeta}'   -> {count} archivo(s) perdidos")

        # Comparar referencias visuales dentro del XML de cada hoja.
        import re
        tags = (
            "drawing",
            "legacyDrawing",
            "legacyDrawingHF",
            "picture",
            "oleObjects",
            "controls",
        )

        self.stdout.write(self.style.WARNING(
            "\n=== Referencias visuales en el XML de la hoja ==="
        ))
        with zipfile.ZipFile(template_path, "r") as zt, \
             zipfile.ZipFile(raw_output, "r") as zo:
            sheet_names = sorted(
                n for n in zt.namelist()
                if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")
            )
            algo_perdido = False
            for sheet_name in sheet_names:
                tpl_xml = zt.read(sheet_name).decode("utf-8", errors="replace")
                if sheet_name in zo.namelist():
                    out_xml = zo.read(sheet_name).decode("utf-8", errors="replace")
                else:
                    out_xml = ""
                self.stdout.write(f"\n  {sheet_name}")
                for tag in tags:
                    pat = re.compile(
                        rf"<{tag}\b(?:[^>]*/>|[^>]*>.*?</{tag}>)",
                        re.DOTALL,
                    )
                    en_template = bool(pat.search(tpl_xml))
                    en_output = bool(pat.search(out_xml))
                    if not en_template:
                        continue
                    if en_output:
                        self.stdout.write(self.style.SUCCESS(
                            f"    OK  <{tag}> presente en template y output"
                        ))
                    else:
                        algo_perdido = True
                        self.stdout.write(self.style.ERROR(
                            f"    XX  <{tag}> en template pero OPENPYXL LO QUITO del output"
                        ))

        if not carpetas and not algo_perdido:
            self.stdout.write(self.style.SUCCESS(
                "\nNo se detectan archivos ni referencias perdidas. "
                "El problema puede estar mas adentro (anclas de drawing, "
                "estilos, o el render mismo de LibreOffice)."
            ))
        elif algo_perdido:
            self.stdout.write(self.style.WARNING(
                "\nSe detectaron referencias visuales que openpyxl quita del "
                "XML de la hoja. La solucion en api/excel_exports.py "
                "(_transplant_sheet_data) ya las restaura."
            ))

        self.stdout.write(self.style.SUCCESS(
            f"\nListo. Inspecciona el ZIP crudo con:\n"
            f"  unzip -l {raw_output}\n"
            f"O extraelo:\n"
            f"  unzip {raw_output} -d {out_dir / 'raw_extracted'}\n"
        ))
