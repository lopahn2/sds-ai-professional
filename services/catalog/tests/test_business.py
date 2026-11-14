from test_contract import HttpCase
from urllib.parse import quote

class CatalogTests(HttpCase):
    def test_search_detail_and_prices(self):
        status, result, _ = self.request('/api/products?q='+quote('키보드'))
        self.assertEqual(status, 200)
        self.assertEqual(result['total'], 1)
        self.assertEqual(result['products'][0]['price'], 79000)
        status, result, _ = self.request('/api/products/1')
        self.assertEqual(status, 200)
        self.assertIn('저소음', result['product']['description'])
        self.assertEqual(result['product']['stock'], 42)
        self.assertIn('uri="/api/products/{id}"', self.request('/metrics')[1])

    def test_empty_search_missing_detail_and_long_query(self):
        self.assertEqual(self.request('/api/products?q='+quote('없는상품'))[1]['total'], 0)
        self.assertEqual(self.request('/api/products/999')[0], 404)
        self.assertEqual(self.request('/api/products?q='+'a'*101)[0], 400)
        self.assertEqual(self.request('/api/products', 'POST', {})[0], 404)

    def test_search_matches_category(self):
        rows = self.request('/api/products?q='+quote('주변기기'))[1]['products']
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(row['category'] == '주변기기' for row in rows))

    def test_stale_price_signal_and_recovery(self):
        self.chaos('stale_price')
        self.assertEqual(self.request('/api/products/1')[1]['product']['price'], 7900)
        self.assertEqual(self.request('/api/products')[1]['products'][0]['price'], 7900)
        self.assertIn('"event": "stale_price"', self.output.getvalue())
        self.chaos('stale_price', False)
        self.assertEqual(self.request('/api/products/1')[1]['product']['price'], 79000)
