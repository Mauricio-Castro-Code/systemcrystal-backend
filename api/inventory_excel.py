"""Inventory workbook exchange with a signed, optimistic-concurrency preview."""
from decimal import Decimal, InvalidOperation
from io import BytesIO
from zipfile import ZipFile

from django.core import signing
from django.db import IntegrityError, transaction
from django.http import HttpResponse
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.worksheet.datavalidation import DataValidation
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import InventoryProduct, normalize_text

HEADERS = ['ID', 'Equipo', 'Categoría', 'Cantidad', 'Precio normal de renta', 'Precio temporada alta', 'Costo de reposición']
MAX_BYTES = 5 * 1024 * 1024
MAX_ROWS = 5000
TOKEN_SALT = 'inventory-excel-v1'
TOKEN_AGE = 15 * 60


def fingerprint(product):
    return [product.name, product.category, product.quantity, str(product.unit_price),
            str(product.high_season_price), str(product.replacement_cost), product.updated_at.isoformat()]



class InventoryExcelExportView(APIView):
    def get(self, request):
        products = list(InventoryProduct.objects.all()[:MAX_ROWS + 1])
        if len(products) > MAX_ROWS:
            raise ValidationError({'errors': [f'El inventario supera los {MAX_ROWS} productos permitidos para el intercambio Excel.']})
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = 'Inventario'
        sheet.append(HEADERS)
        for product in products:
            sheet.append([product.pk, product.name, product.get_category_display(), product.quantity,
                          product.unit_price, product.high_season_price, product.replacement_cost])
            sheet.cell(sheet.max_row, 2).data_type = "s"
        for _ in range(min(50, MAX_ROWS - len(products))):
            sheet.append([None, None, None, None, None, None, None])
        for cell in sheet[1]:
            cell.font = Font(bold=True, color='FFFFFF')
            cell.fill = PatternFill('solid', fgColor='173F4F')
        for column, width in zip('ABCDEFG', [12, 40, 22, 15, 27, 25, 25]):
            sheet.column_dimensions[column].width = width
        for row in sheet.iter_rows(min_row=2):
            for cell in row[4:]: cell.number_format = '#,##0.00'
        sheet.freeze_panes = 'B2'
        sheet.auto_filter.ref = sheet.dimensions
        validation = DataValidation(type='list', formula1='"Vajilla,Mobiliario,Mantelería,Carpas,Otros"')
        sheet.add_data_validation(validation)
        validation.add(f'C2:C{MAX_ROWS + 1}')
        instructions = workbook.create_sheet('Instrucciones')
        for text in [
            'Completa Inventario y conserva los encabezados. No uses fórmulas.',
            'Conserva el ID de productos existentes. Deja ID vacío para buscar por nombre o crear un producto.',
            'Categorías: Vajilla, Mobiliario, Mantelería, Carpas, Otros (también se aceptan códigos).',
            'Cantidad: entero no negativo. Precios: no negativos con máximo dos decimales.',
            'Temporada alta: del 15 de noviembre al 1 de enero, ambos inclusive.',
            'Precio temporada alta vacío usa el precio normal; cero es un precio válido.',
            'Las filas completamente vacías se ignoran. No se elimina ningún producto.',
            'Revisa la vista previa antes de confirmar. La confirmación vence en 15 minutos.',
        ]: instructions.append([text])
        instructions.column_dimensions['A'].width = 120
        stream = BytesIO(); workbook.save(stream)
        response = HttpResponse(stream.getvalue(), content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        response['Content-Disposition'] = 'attachment; filename="inventario.xlsx"'
        return response


def numeric(value, label, errors, *, nullable=False, integer=False):
    if value in (None, '') and nullable: return None
    try:
        if isinstance(value, bool): raise ValueError
        number = Decimal(str(value))
        if not number.is_finite() or number < 0: raise ValueError
        if integer:
            if number != number.to_integral_value() or number > 2147483647: raise ValueError
            return int(number)
        if number > Decimal('9999999999.99') or number != number.quantize(Decimal('.01')): raise ValueError
        return str(number.quantize(Decimal('.01')))
    except (InvalidOperation, ValueError, TypeError):
        errors.append(f'{label}: introduce un número no negativo' + (' entero.' if integer else ' con máximo dos decimales.'))
        return None


def preview_workbook(upload, user_id):
    if not upload or not upload.name.lower().endswith('.xlsx'):
        raise ValidationError({'errors': ['Selecciona un archivo .xlsx.']})
    if upload.size > MAX_BYTES:
        raise ValidationError({'errors': ['El archivo supera el límite de 5 MB.']})
    try:
        data = upload.read()
        with ZipFile(BytesIO(data)) as archive:
            if sum(entry.file_size for entry in archive.infolist()) > 30 * 1024 * 1024:
                raise ValueError
        workbook = load_workbook(BytesIO(data), read_only=True, data_only=False)
        sheet = workbook['Inventario'] if 'Inventario' in workbook.sheetnames else workbook.active
        if sheet.max_row > MAX_ROWS + 1 or sheet.max_column > 7:
            raise ValidationError({'errors': ['El archivo admite hasta 5000 filas y siete columnas.']})
        source = sheet.iter_rows()
        if [cell.value for cell in next(source, [])] != HEADERS:
            raise ValidationError({'errors': ['Los encabezados no coinciden con la plantilla de inventario.']})
        records = list(InventoryProduct.objects.all())
        by_id = {item.pk: item for item in records}
        by_name = {item.name_key: item for item in records}
        categories = {normalize_text(value): code for code, label in InventoryProduct.Category.choices for value in (code, label)}
        rows, validated, seen_names, seen_ids = [], [], set(), set()
        for row_number, source_cells in enumerate(source, 2):
            has_formula = any(cell.data_type == "f" for cell in source_cells)
            cells = [cell.value for cell in source_cells] + [None] * (7 - len(source_cells))
            if all(value in (None, '') for value in cells): continue
            errors = []
            raw_id, name, category, quantity, normal, high, replacement = cells
            name = str(name or '').strip()
            key = normalize_text(name)
            if not name or len(name) > 120: errors.append('Equipo: el nombre es requerido y admite hasta 120 caracteres.')
            if has_formula: errors.append('No se permiten fórmulas.')
            product = None
            if raw_id not in (None, ''):
                identity = numeric(raw_id, 'ID', errors, integer=True)
                product = by_id.get(identity)
                if product is None: errors.append('El ID no corresponde a un producto existente.')
            else: product = by_name.get(key)
            if key in seen_names or (product and product.pk in seen_ids): errors.append('Producto duplicado en el archivo.')
            seen_names.add(key)
            if product: seen_ids.add(product.pk)
            conflict = by_name.get(key)
            if conflict and product and conflict.pk != product.pk: errors.append('El nombre pertenece a otro ID.')
            category_code = categories.get(normalize_text(str(category or '')))
            if not category_code: errors.append('Categoría inválida.')
            row = dict(rowNumber=row_number, action='update' if product else 'create', name=name,
                       category=category_code or str(category or ''), quantity=numeric(quantity, 'Cantidad', errors, integer=True),
                       unitPrice=numeric(normal, 'Precio normal', errors), highSeasonPrice=numeric(high, 'Precio temporada alta', errors, nullable=True),
                       replacementCost=numeric(replacement, 'Costo de reposición', errors), errors=errors)
            rows.append(row)
            validated.append({'id': product.pk if product else None, 'before': fingerprint(product) if product else None, 'row': row})
        if not rows: raise ValidationError({'errors': ['El archivo no contiene productos.']})
        valid = not any(row['errors'] for row in rows)
        return dict(rows=rows, errors=[], createCount=sum(row['action'] == 'create' for row in rows),
                    updateCount=sum(row['action'] == 'update' for row in rows), canImport=valid,
                    token=signing.dumps({'user': user_id, 'items': validated}, salt=TOKEN_SALT, compress=True) if valid else None)
    except ValidationError: raise
    except Exception as exc:
        raise ValidationError({'errors': ['No se pudo leer el archivo .xlsx. Verifica que sea válido.']}) from exc
    finally:
        if 'workbook' in locals(): workbook.close()


class InventoryImportPreviewView(APIView):
    permission_classes = [IsAdminUser]
    def post(self, request):
        return Response(preview_workbook(request.FILES.get('file'), request.user.pk))


class InventoryImportConfirmView(APIView):
    permission_classes = [IsAdminUser]
    def post(self, request):
        try:
            payload = signing.loads(request.data.get('token', ''), salt=TOKEN_SALT, max_age=TOKEN_AGE)
            if payload['user'] != request.user.pk: raise ValueError
        except (signing.BadSignature, ValueError, TypeError, KeyError):
            raise ValidationError({'errors': ['La vista previa venció o no es válida. Genera una nueva.']})
        try:
            with transaction.atomic():
                current = list(InventoryProduct.objects.select_for_update().all())
                by_id = {item.pk: item for item in current}
                by_name = {item.name_key: item for item in current}
                for item in payload['items']:
                    product = by_id.get(item['id'])
                    conflict = by_name.get(normalize_text(item['row']['name']))
                    if (item['id'] and (not product or fingerprint(product) != item['before'])) or (conflict and conflict.pk != item['id']):
                        raise ValidationError({'errors': ['El inventario cambió desde la vista previa. Genera una nueva.']})
                created = updated = 0
                for item in payload['items']:
                    row = item['row']
                    product = by_id[item['id']] if item['id'] else InventoryProduct()
                    product.name = row['name']; product.category = row['category']; product.quantity = row['quantity']
                    product.unit_price = row['unitPrice']; product.high_season_price = row['highSeasonPrice']; product.replacement_cost = row['replacementCost']
                    product.save()
                    if item['id']: updated += 1
                    else: created += 1
        except IntegrityError as exc:
            raise ValidationError({'errors': ['El inventario cambió. Genera una nueva vista previa.']}) from exc
        return Response({'created': created, 'updated': updated})
