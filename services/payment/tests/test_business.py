import threading
import unittest
from test_contract import HttpCase

class PaymentTests(HttpCase):
    def create(self, **overrides):
        data = {'order_id': '회계-2026', 'amount': 12500}
        data.update(overrides)
        return self.request('/api/payments', 'POST', data)

    def test_create_cancel_and_normal_fee(self):
        status, result, _ = self.create()
        self.assertEqual(status, 201)
        payment = result['payment']
        self.assertEqual(payment['fee'], 375)
        self.assertEqual(payment['charged_amount'], 12500)
        path = '/api/payments/'+payment['id']+'/cancel'
        self.assertEqual(self.request(path, 'POST', {})[1]['payment']['status'], 'cancelled')
        self.assertEqual(self.request(path, 'POST', {})[0], 409)
        self.assertEqual(self.request('/api/payments/99999/cancel', 'POST', {})[0], 404)
        metrics = self.request('/metrics')[1]
        self.assertIn('uri="/api/payments/{id}/cancel"', metrics)
        self.assertNotIn('uri="'+path+'"', metrics)

    def test_invalid_payment_inputs(self):
        for amount in (0, -1, 100000001, True, 12.5, '100', None):
            with self.subTest(amount=amount):
                self.assertEqual(self.create(amount=amount)[0], 400)
        for order in ('', '  ', 123, '가'*81):
            with self.subTest(order=order):
                self.assertEqual(self.create(order_id=order)[0], 400)

    def test_duplicate_charge_warning_and_recovery(self):
        self.chaos('duplicate_charge')
        self.assertEqual(self.create()[1]['payment']['charged_amount'], 25000)
        self.assertIn('"event": "duplicate_charge"', self.output.getvalue())
        self.chaos('duplicate_charge', False)
        self.assertEqual(self.create()[1]['payment']['charged_amount'], 12500)

    def test_history_has_fixed_retention_and_monotonic_ids(self):
        business = self.server.runtime.business
        before = business.sequence
        for i in range(110):
            business.create_payment({'order_id': '보관-'+str(i), 'amount': 100})
        rows = self.request('/api/payments')[1]['payments']
        self.assertEqual(len(rows), 100)
        self.assertEqual(business.sequence, before+110)
        self.assertEqual(len({row['id'] for row in rows}), 100)
