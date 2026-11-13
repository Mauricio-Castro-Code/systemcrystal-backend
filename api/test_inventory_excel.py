from io import BytesIO
from decimal import Decimal
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APITestCase
from openpyxl import Workbook, load_workbook
from .models import InventoryProduct

HEADERS = ['ID', 'Equipo', 'Categoría', 'Cantidad', 'Precio normal de renta', 'Precio temporada alta', 'Costo de reposición']

class InventoryExcelTests(APITestCase):
    def setUp(self):
        self.client.force_authenticate(get_user_model().objects.create_user(username='excel', is_staff=True))
        self.product = InventoryProduct.objects.create(name='Silla', quantity=4, unit_price=10)

    def preview(self, rows):
        workbook = Workbook(); workbook.active.append(HEADERS)
        for row in rows: workbook.active.append(row)
        stream = BytesIO(); workbook.save(stream)
        return self.client.post('/api/inventory/import/preview/', {'file': SimpleUploadedFile('inventario.xlsx', stream.getvalue())}, format='multipart')

    def test_export_has_existing_records_and_blank_rows(self):
        response = self.client.get('/api/inventory/export/excel/')
        self.assertEqual(response.status_code, 200)
        workbook = load_workbook(BytesIO(response.content))
        self.assertEqual(list(next(workbook.active.values)), HEADERS)
        self.assertEqual(workbook.active.cell(2, 1).value, self.product.pk)
        self.assertGreater(workbook.active.max_row, 2)
        self.assertIn('Instrucciones', workbook.sheetnames)

    def test_preview_and_confirm_preserve_zero_and_null(self):
        response = self.preview([[self.product.pk, 'Silla', 'Mobiliario', 5, 12, 0, 99], [None, 'Mesa', 'MOBILIARIO', 2, 30, None, 100]])
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data['canImport'])
        self.assertEqual(InventoryProduct.objects.count(), 1)
        confirm = self.client.post('/api/inventory/import/confirm/', {'token': response.data['token']}, format='json')
        self.assertEqual(confirm.data, {'created': 1, 'updated': 1})
        self.product.refresh_from_db()
        self.assertEqual(self.product.high_season_price, Decimal('0'))
        self.assertIsNone(InventoryProduct.objects.get(name='Mesa').high_season_price)
        self.assertEqual(self.client.post('/api/inventory/import/confirm/', {'token': response.data['token']}, format='json').status_code, 400)

    def test_invalid_rows_cannot_import(self):
        response = self.preview([[None, 'Mesa', 'Otros', 1.5, -1, None, 0], [None, ' mesa ', 'Otros', 2, 1, None, 0]])
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data['canImport'])
        self.assertIsNone(response.data['token'])

    def test_stale_preview_rejected_atomically(self):
        response = self.preview([[self.product.pk, 'Silla', 'Otros', 2, 12, None, 0], [None, 'Mesa', 'Otros', 2, 12, None, 0]])
        self.product.quantity = 8; self.product.save()
        confirm = self.client.post('/api/inventory/import/confirm/', {'token': response.data['token']}, format='json')
        self.assertEqual(confirm.status_code, 400)
        self.assertFalse(InventoryProduct.objects.filter(name='Mesa').exists())

    def test_api_optional_prices_preserved(self):
        response = self.client.put(f'/api/inventory/{self.product.pk}/', {'name':'Silla','quantity':4,'unitPrice':'10','highSeasonPrice':'0','replacementCost':'50'}, format='json')
        self.assertEqual(response.status_code, 200)
        response = self.client.put(f'/api/inventory/{self.product.pk}/', {'name':'Silla','quantity':4,'unitPrice':'10'}, format='json')
        self.assertEqual(Decimal(response.data['replacementCost']), Decimal('50.00'))
        self.assertEqual(Decimal(response.data['highSeasonPrice']), Decimal('0.00'))

    def test_export_formula_text_and_round_trip(self):
        self.product.name = '=HYPERLINK("evil")'; self.product.save()
        response = self.client.get('/api/inventory/export/excel/')
        workbook = load_workbook(BytesIO(response.content))
        self.assertEqual(workbook.active.cell(2, 2).data_type, 's')
        preview = self.client.post('/api/inventory/import/preview/', {'file': SimpleUploadedFile('inventario.xlsx', response.content)}, format='multipart')
        self.assertTrue(preview.data['canImport'])
        self.assertEqual(preview.data['rows'][0]['name'], self.product.name)

    def test_rejects_formulas_unknown_ids_and_invalid_money(self):
        for row in [[None, '=1+1', 'Otros', 2, 1, None, 1],
                    [999, 'Mesa', 'Otros', 2, 1, None, 1],
                    [None, 'Mesa', 'Otros', 2, 'NaN', None, 1],
                    [None, 'Mesa', 'Otros', 2, 1.001, None, 1]]:
            with self.subTest(row=row):
                self.assertFalse(self.preview([row]).data['canImport'])

    def test_new_name_conflict_prevents_all_writes(self):
        preview = self.preview([[None, 'Mesa', 'Otros', 2, 12, None, 0]])
        InventoryProduct.objects.create(name=' MESA ', quantity=1)
        response = self.client.post('/api/inventory/import/confirm/', {'token':preview.data['token']}, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(InventoryProduct.objects.get(name_key='mesa').quantity, 1)

    def test_import_requires_admin_and_rejects_invalid_files_tokens(self):
        response = self.client.post('/api/inventory/import/preview/', {'file': SimpleUploadedFile('bad.xlsx', b'bad')}, format='multipart')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.post('/api/inventory/import/confirm/', {'token':'bad'}, format='json').status_code, 400)
        self.client.force_authenticate(get_user_model().objects.create_user(username='viewer'))
        self.assertEqual(self.preview([[None, 'Mesa', 'Otros', 2, 12, None, 0]]).status_code, 403)

    def test_expired_token_and_oversized_upload_are_rejected(self):
        from unittest.mock import patch
        from django.core import signing
        preview = self.preview([[None, 'Mesa', 'Otros', 2, 12, None, 0]])
        with patch('api.inventory_excel.signing.loads', side_effect=signing.SignatureExpired):
            self.assertEqual(self.client.post('/api/inventory/import/confirm/', {'token':preview.data['token']}, format='json').status_code, 400)
        with patch('api.inventory_excel.MAX_BYTES', 2):
            self.assertEqual(self.preview([[None, 'Mesa', 'Otros', 2, 12, None, 0]]).status_code, 400)

    def test_name_matching_and_duplicate_identity(self):
        preview = self.preview([[None, ' SILLA ', 'Otros', 2, 12, None, 0]])
        self.assertEqual(preview.data['rows'][0]['action'], 'update')
        self.assertTrue(preview.data['canImport'])
        duplicate = self.preview([[self.product.pk, 'Silla nueva', 'Otros', 2, 12, None, 0], [None, 'Silla', 'Otros', 2, 12, None, 0]])
        self.assertFalse(duplicate.data['canImport'])

    def test_export_caps_blank_rows_to_import_capacity(self):
        with patch('api.inventory_excel.MAX_ROWS', 2):
            response = self.client.get('/api/inventory/export/excel/')
            workbook = load_workbook(BytesIO(response.content))
            self.assertEqual(workbook.active.max_row, 3)
            preview = self.client.post('/api/inventory/import/preview/', {'file': SimpleUploadedFile('inventario.xlsx', response.content)}, format='multipart')
            self.assertTrue(preview.data['canImport'])

    def test_export_rejects_inventory_above_import_capacity(self):
        InventoryProduct.objects.create(name='Mesa')
        with patch('api.inventory_excel.MAX_ROWS', 1):
            response = self.client.get('/api/inventory/export/excel/')
            self.assertEqual(response.status_code, 400)
            self.assertIn('errors', response.data)

    def test_literal_apostrophe_name_round_trips_exactly(self):
        self.product.name = "'=Silla"
        self.product.save()
        response = self.client.get('/api/inventory/export/excel/')
        workbook = load_workbook(BytesIO(response.content))
        self.assertEqual(workbook.active.cell(2, 2).value, self.product.name)
        preview = self.client.post('/api/inventory/import/preview/', {'file': SimpleUploadedFile('inventario.xlsx', response.content)}, format='multipart')
        self.assertTrue(preview.data['canImport'])
        self.assertEqual(preview.data['rows'][0]['name'], self.product.name)
