"""Real Chromium uses a disposable local dev page. No cluster, model, registry or credentials are required."""

import copy
from contextlib import contextmanager
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'liveprobe'))
from application_runner.dev_ui_qa import (
    CATALOG_TEST,
    ORDER_TEST,
    _catalog_flows,
    _order_flows,
    run_dev_ui_qa,
    run_plan,
)
from application_runner.profiles import digest


def business(item):
    if item == 'a':
        return {'status': 200, 'body': {'item': 'a', 'available': True, 'quantity': 42}}
    return {'status': 404, 'body': {}}


def plan(origin):
    sha = sha256((ROOT / 'services/order' / ORDER_TEST).read_bytes()).hexdigest()
    refs = lambda name: [{'test_id': name, 'file': ORDER_TEST, 'file_sha256': sha}]
    return {
        'schema': 'qa-plan/v1',
        'environment': 'dev',
        'service': 'order',
        'ui_url': origin,
        'flows': [
            {
                'id': 'order-stock',
                'name_ko': '주문 가능과 재고 확인',
                'source_tests': refs('returnsInventoryData'),
                'input': {'selector': '#item', 'value': 'a'},
                'submit': {'selector': '#order button'},
                'result': {'selector': '#response'},
                'request': {'method': 'GET', 'path': '/api/orders/check', 'query': {'item': 'a'}},
                'expectations': [
                    {
                        'response_path': 'orderable',
                        'dom_label': '주문 가능 여부',
                        'map': {'true': '주문 가능', 'false': '주문 불가'},
                    },
                    {'response_path': 'inventory.quantity', 'dom_label': '재고.수량'},
                ],
                'independent_read': {
                    'service': 'inventory',
                    'path': '/api/stock/a',
                    'compare': [
                        {'response_path': 'orderable', 'read_path': 'available'},
                        {'response_path': 'inventory.quantity', 'read_path': 'quantity'},
                    ],
                },
            }
        ],
    }


@contextmanager
def server(js=None):
    static = ROOT / 'services/order/src/main/resources/static'
    reads = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            reads.append(self.path)
            path = urlsplit(self.path).path
            if path == '/':
                body = (static / 'index.html').read_bytes()
                content = 'text/html; charset=utf-8'
                status = 200
            elif path == '/app.js':
                body = (js or (static / 'app.js').read_text()).encode()
                content = 'text/javascript; charset=utf-8'
                status = 200
            elif path == '/app.css':
                body = b''
                content = 'text/css'
                status = 200
            else:
                content = 'application/json'
                if self.path.endswith('item=a'):
                    status = 200
                    body = json.dumps(
                        {'item': 'a', 'orderable': True, 'inventory': business('a')['body']}
                    ).encode()
                elif self.path.endswith('item=x'):
                    status = 404
                    body = b'{"item":"x","error":"unknown item"}'
                else:
                    status = 200
                    body = b'{}'
            self.send_response(status)
            self.send_header('content-type', content)
            self.end_headers()
            self.wfile.write(body)

    http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    t = threading.Thread(target=http.serve_forever, daemon=True)
    t.start()
    try:
        yield 'http://127.0.0.1:' + str(http.server_port), reads
    finally:
        http.shutdown()
        http.server_close()
        t.join()


class DevQA(unittest.TestCase):
    def read_inventory(self, service, path):
        assert service == 'inventory' and path in ('/api/stock/a', '/api/stock/x')
        return business(path.rsplit('/', 1)[1])

    def test_developer_source_drives_inputs_and_result_assertions(self):
        java = (ROOT / 'services/order' / ORDER_TEST).read_text()
        self.assertEqual([f['value'] for f in _order_flows(java)], ['a', 'x'])
        with self.assertRaisesRegex(Exception, 'controller_assertions_not_mapped'):
            _order_flows(java.replace('jsonPath("$.inventory.quantity")', 'jsonPath("$.inventory.price")'))
        self.assertEqual(_catalog_flows((ROOT / 'services/catalog' / CATALOG_TEST).read_text())[0]['price'], 79000)

    def test_real_browser_uses_dev_page_for_positive_and_negative_order(self):
        with server() as (origin, reads), tempfile.TemporaryDirectory() as evidence:
            source = ROOT / 'services/order'
            image = 'liveprobe/order@sha256:' + '1' * 64
            profile = {
                'service': 'order',
                'system': 'samsung-trade',
                'test_root': 'src/test',
                'id': 'unit-order',
                'image_repository': 'liveprobe/order',
                'qa_ui_origin': origin,
                'qa_inventory_read': business,
                'qa_source': {
                    'root': str(source),
                    'file_sha256': {ORDER_TEST: sha256((source / ORDER_TEST).read_bytes()).hexdigest()},
                },
            }
            p = {
                'service': 'order',
                'stage': 'dev',
                'release_id': 'isolated',
                'artifact_sha256': 'a' * 64,
                'plan_sha256': 'b' * 64,
                'image': image,
                'observed_image': image,
            }
            p['artifact'] = {'image': image, 'source_tree_sha256': digest(profile['qa_source']['file_sha256'])}
            qa = run_dev_ui_qa(job=p, profile=profile, evidence_dir=evidence)['qa']
            self.assertEqual(qa['outcome'], 'passed', qa)
            self.assertEqual([s['http_status'] for s in qa['steps'] if 'http_status' in s], [200, 404])
            self.assertEqual(len(qa['steps']), 12)
            self.assertEqual(len(list(Path(evidence).rglob('flow-*.png'))), 2)
            self.assertTrue(all('chaos' not in url for url in reads))
            wrong = run_dev_ui_qa(job={**p, 'observed_image': 'wrong'}, profile=profile, evidence_dir=evidence)
            self.assertFalse(wrong['ok'])
            self.assertEqual(wrong['write_outcome'], 'not_written')
            mismatch = run_dev_ui_qa(
                job=p,
                profile={**profile, 'qa_source': {'root': str(source), 'file_sha256': {ORDER_TEST: '0' * 64}}},
                evidence_dir=evidence,
            )
            self.assertEqual(mismatch['qa']['outcome'], 'unverifiable')
            absent = run_dev_ui_qa(
                job=p,
                profile={**profile, 'qa_source': {'root': str(source), 'file_sha256': {}}},
                evidence_dir=evidence,
            )
            self.assertEqual(absent['qa']['outcome'], 'unverifiable')
            hashes = {
                'src/main/resources/static/app.js': sha256(
                    (source / 'src/main/resources/static/app.js').read_bytes()
                ).hexdigest()
            }
            missing = run_dev_ui_qa(
                job={**p, 'artifact': {'image': image, 'source_tree_sha256': digest(hashes)}},
                profile={**profile, 'qa_source': {'root': str(source), 'file_sha256': hashes}},
                evidence_dir=evidence,
            )
            self.assertEqual(missing['qa']['outcome'], 'tests_missing')

            forged = plan(origin)
            forged['flows'][0]['source_tests'][0]['test_id'] = 'notFoundIsNotRetried'
            bad_ref = run_dev_ui_qa(job={**p, 'qa_plan': forged}, profile=profile, evidence_dir=evidence)
            self.assertEqual(bad_ref['error'], 'qa_test_scenario_not_grounded')

    def test_plan_verifies_independent_and_final_dom_when_http_is_200(self):
        with server() as (origin, _), tempfile.TemporaryDirectory() as evidence:
            args = {'evidence_dir': evidence, 'independent_read': self.read_inventory, 'registered_origin': origin}
            success = run_plan(plan(origin), **args)
            self.assertEqual(success['outcome'], 'passed', success)
            row = success['flows'][0]
            self.assertTrue(all(s['status'] == 'passed' for s in row['steps']))
            self.assertTrue(row['independent_read']['matched'])
            mismatch = run_plan(
                plan(origin),
                **{
                    **args,
                    'independent_read': lambda service, path: {
                        'status': 200,
                        'body': {'quantity': 41, 'available': True},
                    },
                }
            )
            self.assertEqual(mismatch['outcome'], 'failed', mismatch)
            self.assertFalse(mismatch['flows'][0]['independent_read']['matched'])
        js = (
            (ROOT / 'services/order/src/main/resources/static/app.js')
            .read_text()
            .replace("수량:data.inventory.quantity??'정보 없음'", "수량:123456")
        )
        with server(js) as (origin, _), tempfile.TemporaryDirectory() as evidence:
            mismatch = run_plan(
                plan(origin), evidence_dir=evidence, independent_read=self.read_inventory, registered_origin=origin
            )
            self.assertEqual(mismatch['outcome'], 'failed', mismatch)
            self.assertEqual(mismatch['flows'][0]['network']['status'], 200)
            self.assertEqual(mismatch['flows'][0]['steps'][4]['code'], 'qa_dom_field_mismatch')

    def test_browser_and_plan_cannot_contact_unregistered_routes(self):
        script = (
            ROOT / 'services/order/src/main/resources/static/app.js'
        ).read_text() + "\nfetch('/unsafe-probe',{method:'POST'}).catch(()=>{});"
        with server(script) as (origin, reads), tempfile.TemporaryDirectory() as evidence:
            p = plan(origin)
            result = run_plan(
                p, evidence_dir=evidence, independent_read=self.read_inventory, registered_origin=origin
            )
            self.assertNotEqual(result['outcome'], 'passed')
            self.assertNotIn('/unsafe-probe', reads)
            for mutate in [
                lambda v: v.update(environment='prd'),
                lambda v: v.update(ui_url='http://order.samsung-trade.prd.localhost'),
                lambda v: v['flows'][0]['submit'].update(selector='#clear-faults'),
                lambda v: v['flows'][0]['request'].update(method='POST'),
                lambda v: v['flows'][0]['expectations'][0]['map'].update(true='주문 불가'),
                lambda v: v['flows'][0]['independent_read'].update(path='/api/stock/a?debug=1'),
            ]:
                v = copy.deepcopy(p)
                mutate(v)

                def forbidden(*_):
                    raise AssertionError('invalid plan reached inventory transport')

                self.assertEqual(
                    run_plan(v, evidence_dir=evidence, independent_read=forbidden, registered_origin=origin)[
                        'outcome'
                    ],
                    'unverifiable',
                )

    def test_periodic_get_cannot_substitute_for_a_broken_submit_handler(self):
        script = """
        const form = document.getElementById('order');
        form.addEventListener('submit', event => event.preventDefault());
        setInterval(async () => {
            await fetch('/api/orders/check?item=a');
            document.getElementById('status').textContent = '응답 200';
            document.getElementById('response').textContent = JSON.stringify({
                '상품':'a','주문 가능 여부':'주문 가능','재고':{'수량':42}
            });
        },150);
        """
        with server(script) as (origin, reads), tempfile.TemporaryDirectory() as evidence:
            result = run_plan(
                plan(origin), evidence_dir=evidence, independent_read=self.read_inventory, registered_origin=origin
            )
            self.assertIn('/api/orders/check?item=a', reads)
            self.assertEqual(result['reason'], 'qa_no_request_from_registered_control')
            self.assertEqual(result['outcome'], 'unverifiable')


if __name__ == '__main__':
    unittest.main()
