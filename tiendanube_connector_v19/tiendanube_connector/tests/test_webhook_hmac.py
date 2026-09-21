# -*- coding: utf-8 -*-
import hashlib
import hmac

from odoo.tests import TransactionCase


class TestLinkedstoreWebhookHmac(TransactionCase):
    """HMAC-SHA256 según documentación Nuvemshop (x-linkedstore-hmac-sha256)."""

    def test_verify_linkedstore_webhook_hmac_valid(self):
        body = b'{"store_id":1,"event":"order/created","id":99}'
        secret = 'app-secret-test'
        expected = hmac.new(secret.encode('utf-8'), body, hashlib.sha256).hexdigest()
        TN = self.env['tn.config']
        self.assertTrue(TN.verify_linkedstore_webhook_hmac(body, expected, secret))
        self.assertTrue(TN.verify_linkedstore_webhook_hmac(body, expected.upper(), secret))

    def test_verify_linkedstore_webhook_hmac_invalid(self):
        body = b'{"store_id":1}'
        secret = 'secret'
        TN = self.env['tn.config']
        good = hmac.new(secret.encode('utf-8'), body, hashlib.sha256).hexdigest()
        self.assertFalse(TN.verify_linkedstore_webhook_hmac(body, 'a' * 64, secret))
        self.assertFalse(TN.verify_linkedstore_webhook_hmac(b'', good, secret))
        self.assertFalse(TN.verify_linkedstore_webhook_hmac(body, good, ''))
