"""상품 카탈로그 시스템: 한국어 상품 검색·상세·가격 제공."""
import re
import os
from urllib.parse import urlencode
from runtime import Runtime, ServiceServer, RequestError, serve

class IndexRebuildError(Exception):
    """검색 색인 재생성에 실패했다."""


class PriceConsistencyError(Exception):
    """노출 가격과 상품 원장의 가격이 다르다."""


class Catalog:
    api_prefix = '/api/products'

    def __init__(self, runtime):
        self.runtime = runtime
        self.searches = 0
        self.views = 0
        self.traffic_index = 0
        self.settings = self.load_settings()
        self.products = [
            dict(id='1', name='무선 키보드', category='주변기기', price=79000, stock=42, description='조용한 사무실을 위한 저소음 무선 키보드입니다.'),
            dict(id='2', name='휴대용 모니터', category='화면 장치', price=249000, stock=18, description='외근과 재택근무에 맞춘 가벼운 보조 화면입니다.'),
            dict(id='3', name='접이식 노트북 거치대', category='사무용품', price=32000, stock=65, description='눈높이를 편안하게 맞추는 알루미늄 거치대입니다.'),
            dict(id='4', name='무선 마우스', category='주변기기', price=45000, stock=90, description='정밀한 조작과 긴 배터리 수명을 제공합니다.'),
            dict(id='5', name='업무용 헤드셋', category='음향 기기', price=119000, stock=24, description='화상 회의에 맞춘 소음 억제 마이크를 갖췄습니다.'),
            dict(id='6', name='다용도 연결 허브', category='주변기기', price=59000, stock=37, description='화면과 저장 장치를 한 번에 연결합니다.'),
        ]
        self.price_cache = {p['id']: p['price'] for p in self.products}
        self.search_index = {}

    def load_settings(self):
        settings = {'index_language': 'ko'}
        if self.runtime.enabled('startup_config_error'):
            # 정상 수정: 색인 언어가 없으면 기본 ko를 유지한다.
            settings['index_language'] = os.environ['CATALOG_INDEX_LANGUAGE']
        return settings

    def display_price(self, product):
        with self.runtime.lock:
            if self.runtime.enabled('business_invariant'):
                self.price_cache[product['id']] = product['price'] // 10
            price = self.price_cache[product['id']]
        if self.runtime.enabled('stale_price'):
            self.runtime.log('WARN', 'stale_price', product_id=product['id'])
            price = product['price'] // 10
        return price

    def reconcile(self):
        with self.runtime.lock:
            fixed = sum(self.price_cache[p['id']] != p['price'] for p in self.products)
            self.price_cache = {p['id']: p['price'] for p in self.products}
        return {'fixed': fixed}

    def search_products(self, query):
        term = query.get('q', [''])[0].strip()
        if len(term) > 100:
            raise RequestError(400, '검색어는 100자 이하여야 합니다')
        with self.runtime.lock:
            self.searches += 1
        if self.runtime.enabled('code_bug') and term:
            # Intentional defect: request query uses q, not search_keyword.
            term = query['search_keyword'][0]
        rows = [dict(row) for row in self.products if not term or term.casefold() in (row['name'] + row['category']).casefold()]
        for row in rows:
            row['price'] = self.display_price(row)
        return rows

    def product_detail(self, product):
        self.runtime.call_dependency('INVENTORY_URL', '/api/stock/' + product['id'], '재고')
        if self.runtime.enabled('code_bug_validation'):
            discount = None
            # 정상 수정: 할인 정보가 없으면 할인율 0을 사용한다.
            return dict(product, discount=discount['rate'])
        return dict(product)

    def price_response(self, product):
        if self.runtime.enabled('code_bug_serialization'):
            # 정상 수정: 문자열로 바꾸지 말고 정수 가격에 천 단위 서식을 적용한다.
            display = '{:,}원'.format(str(product['price']))
        else:
            display = '{:,}원'.format(product['price'])
        return {'product': dict(product), 'price_text': display}

    def handle(self, method, path, query, data):
        if method == 'POST' and path == self.api_prefix + '/reconcile':
            return 200, self.reconcile()
        match = re.fullmatch(r'/api/products/([0-9]+)/price', path)
        if method == 'POST' and match:
            price = data.get('price')
            if type(price) is not int or not 1 <= price <= 100000000:
                raise RequestError(400, '가격은 1원 이상 1억 원 이하의 정수여야 합니다')
            with self.runtime.lock:
                product = next((p for p in self.products if p['id'] == match[1]), None)
                if product is None:
                    raise RequestError(404, '상품을 찾을 수 없습니다')
                product['price'] = price
                self.price_cache[product['id']] = price
                return 200, self.price_response(product)
        if method == 'GET' and path == self.api_prefix:
            rows = self.search_products(query)
            return 200, {'products': rows, 'total': len(rows), 'searches': self.searches}
        match = re.fullmatch(r'/api/products/([0-9]+)', path)
        if method == 'GET' and match:
            for product in self.products:
                if product['id'] == match[1]:
                    with self.runtime.lock:
                        self.views += 1
                    result = self.product_detail(product)
                    result['price'] = self.display_price(product)
                    return 200, {'product': result}
            raise RequestError(404, '상품을 찾을 수 없습니다')
        raise RequestError(404, '요청한 상품 업무를 찾을 수 없습니다')

    def template(self, path):
        if path == self.api_prefix + '/reconcile':
            return path
        if re.fullmatch(r'/api/products/[^/]+/price', path):
            return '/api/products/{id}/price'
        if re.fullmatch(r'/api/products/[^/]+', path):
            return '/api/products/{id}'
        return path if path in ('/', '/chaos', '/api/config', '/api/chaos', '/healthz/live', '/healthz/ready', '/metrics', '/static/styles.css', '/static/app.js', self.api_prefix) else '/unknown'

    def metrics(self):
        with self.runtime.lock:
            return [('catalog_searches_total', 'counter', self.searches), ('catalog_product_views_total', 'counter', self.views)]

    def probes(self):
        return {'code_bug': ('GET', self.api_prefix + '?' + urlencode({'q': '키보드'}), None),
                'code_bug_validation': ('GET', self.api_prefix + '/1', None),
                'code_bug_serialization': ('POST', self.api_prefix + '/1/price', {'price': 79000}),
                'dependency_failure': ('GET', self.api_prefix + '/1', None)}

    def traffic(self):
        calls = list(self.probes().values())[:3] + [('GET', self.api_prefix, None)]
        with self.runtime.lock:
            call = calls[self.traffic_index % len(calls)]
            self.traffic_index += 1
        return call

    def background_job(self):
        with self.runtime.lock:
            rows = [dict(p) for p in self.products]
        try:
            # 정상 수정: 원장의 name 키로 검색 색인을 생성한다.
            key = 'search_title' if self.runtime.enabled('background_job_failure') else 'name'
            index = {p['id']: p[key].casefold() for p in rows}
        except KeyError as exc:
            raise IndexRebuildError('상품 검색 색인을 재생성하지 못했습니다') from exc
        with self.runtime.lock:
            self.search_index = index

    def check_invariants(self):
        with self.runtime.lock:
            bad = [p['id'] for p in self.products if self.display_price(p) != p['price']]
        if bad:
            raise PriceConsistencyError(f'상품 {bad[0]}의 노출 가격이 원장과 다릅니다')

    def tick(self):
        return

def create_server(host='0.0.0.0', port=8080, background=True):
    runtime = Runtime('catalog', '상품 카탈로그', {'stale_price': ('가격 정보 오류', '표시 가격 10분의 1과 경고 로그')})
    runtime.business = Catalog(runtime)
    server = ServiceServer((host, port), runtime)
    if background:
        runtime.start(server.server_port)
    return server

if __name__ == '__main__':
    serve(create_server)
