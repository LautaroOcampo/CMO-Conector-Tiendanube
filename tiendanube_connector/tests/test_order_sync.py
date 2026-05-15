# -*- coding: utf-8 -*-

from odoo.tests import TransactionCase
from odoo.exceptions import ValidationError


def _minimal_order_data(tn_order_id=99999, **overrides):
    """Payload mínimo de una orden TiendaNube para tests."""
    data = {
        'id': tn_order_id,
        'created_at': '2024-01-15T10:00:00-03:00',
        'status': 'open',
        'total': 100.0,
        'shipping_cost': 0,
        'contact_name': 'Cliente Test',
        'contact_email': 'test@test.com',
        'contact_phone': '',
        'contact_identification': '',
        'customer': {},
        'shipping_address': {
            'address': 'Calle Test',
            'number': '100',
            'city': 'Ciudad Test',
            'province': 'Buenos Aires',
            'country': 'AR',
            'zipcode': '1000',
        },
        'source': 'web',
        'note': '',
        'owner_note': '',
        'payment_status': 'pending',
        'fulfillment_status': 'unfulfilled',
    }
    data.update(overrides)
    return data


class TestOrderSyncIdempotency(TransactionCase):
    """Tests de idempotencia: mismo tn_order_id + company_id no debe duplicar órdenes."""

    def setUp(self):
        super().setUp()
        self.TNOrder = self.env['tn.sale.order'].with_context(tracking_disable=True)
        self.company = self.env.company
        self.tn_config = self.env['tn.config'].create({
            'name': 'Config Test',
            'store_id': '12345',
            'access_token': 'test-token',
            'company_id': self.company.id,
            'polling_enabled': True,
        })

    def test_idempotency_same_order_twice(self):
        """Crear la misma orden dos veces debe dar una sola tn.sale.order (actualización)."""
        order_data = _minimal_order_data(1001)
        ctx = {'tn_config_id': self.tn_config.id, 'allowed_company_ids': [self.company.id]}
        order1 = self.TNOrder.sudo().with_context(**ctx).create_order_from_webhook(order_data, webhook_secret=None)
        self.assertTrue(order1)
        count_before = self.TNOrder.search_count([('tn_order_id', '=', 1001), ('company_id', '=', self.company.id)])
        self.assertEqual(count_before, 1)
        order2 = self.TNOrder.sudo().with_context(**ctx).create_order_from_webhook(order_data, webhook_secret=None)
        self.assertEqual(order1.id, order2.id)
        count_after = self.TNOrder.search_count([('tn_order_id', '=', 1001), ('company_id', '=', self.company.id)])
        self.assertEqual(count_after, 1)

    def test_idempotency_update_fulfillment(self):
        """Segunda llamada con mismo ID actualiza fulfillment y no crea duplicado."""
        order_data = _minimal_order_data(1002, fulfillment_status='unfulfilled')
        ctx = {'tn_config_id': self.tn_config.id, 'allowed_company_ids': [self.company.id]}
        order1 = self.TNOrder.sudo().with_context(**ctx).create_order_from_webhook(order_data, webhook_secret=None)
        self.assertEqual(order1.fulfillment_status, 'to_pack')
        order_data_updated = _minimal_order_data(1002, fulfillment_status='shipped')
        order2 = self.TNOrder.sudo().with_context(**ctx).create_order_from_webhook(order_data_updated, webhook_secret=None)
        self.assertEqual(order1.id, order2.id)
        self.assertEqual(order2.fulfillment_status, 'shipped')


class TestOrderSyncCompany(TransactionCase):
    """Tests de compañía: órdenes creadas con tn_config deben tener company_id de la config."""

    def setUp(self):
        super().setUp()
        self.TNOrder = self.env['tn.sale.order'].with_context(tracking_disable=True)
        self.company = self.env.company
        self.tn_config = self.env['tn.config'].create({
            'name': 'Config Test Company',
            'store_id': '67890',
            'access_token': 'test-token',
            'company_id': self.company.id,
            'polling_enabled': False,
        })

    def test_order_has_config_company(self):
        """La orden creada desde una config debe tener company_id = config.company_id."""
        order_data = _minimal_order_data(2001)
        ctx = {'tn_config_id': self.tn_config.id, 'allowed_company_ids': [self.company.id]}
        order = self.TNOrder.sudo().with_context(**ctx).create_order_from_webhook(order_data, webhook_secret=None)
        self.assertEqual(order.company_id.id, self.tn_config.company_id.id)
        self.assertEqual(order.company_id.id, self.company.id)


class TestCronOnlyPollingEnabled(TransactionCase):
    """Tests del cron: solo se procesan configuraciones con polling_enabled=True."""

    def setUp(self):
        super().setUp()
        self.TNConfig = self.env['tn.config'].with_context(tracking_disable=True)
        self.company = self.env.company

    def test_cron_search_only_polling_enabled(self):
        """El cron debe buscar solo configs con access_token, store_id y polling_enabled=True."""
        config_on = self.TNConfig.create({
            'name': 'Config Polling ON',
            'store_id': '111',
            'access_token': 'token1',
            'company_id': self.company.id,
            'polling_enabled': True,
        })
        config_off = self.TNConfig.create({
            'name': 'Config Polling OFF',
            'store_id': '222',
            'access_token': 'token2',
            'company_id': self.company.id,
            'polling_enabled': False,
        })
        configs = self.TNConfig.search([
            ('access_token', '!=', False),
            ('store_id', '!=', False),
            ('polling_enabled', '=', True),
        ])
        self.assertIn(config_on, configs)
        self.assertNotIn(config_off, configs)
