"""Run registered, read-only dev UI workflows against the tests of the released source tree."""

import ast
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
from urllib.parse import urlsplit

from .profiles import digest, manifest_image
from .source import safe_path

ORDER_TEST = 'src/test/java/io/liveprobe/target/order/OrderControllerTest.java'
CATALOG_TEST = 'tests/test_business.py'
DELIVERY_TEST = 'test/index.js'
DELIVERY_CASE = '송장 등록·조회·순차 단계 변경·지연 목록'
DELIVERY_TEST_ID = 'node:' + hashlib.sha256(DELIVERY_CASE.encode()).hexdigest()[:20]
HEX = re.compile(r'[0-9a-f]{64}')
ITEM = r'([A-Za-z0-9_-]{1,32})'


class QAError(Exception):
    def __init__(self, code, outcome='unverifiable'):
        self.code = code
        self.outcome = outcome


def _origin(value, service, system):
    parsed = urlsplit(value)
    dev_host = f'{service}.{system}.dev.localhost'
    if (
        parsed.scheme not in ('https', 'http')
        or parsed.hostname not in (dev_host, '127.0.0.1', '::1', 'localhost')
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
        or parsed.path not in ('', '/')
        or (parsed.hostname != dev_host and not parsed.port)
    ):
        raise QAError('unregistered_dev_origin')
    return value.rstrip('/')


def _pinned_test(profile, path, artifact):
    proof = profile.get('qa_source')
    if not isinstance(proof, dict) or not isinstance(proof.get('file_sha256'), dict) or not proof['file_sha256']:
        raise QAError('developer_test_proof_unavailable')
    hashes = proof['file_sha256']
    root = Path(proof.get('root', ''))
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise QAError('developer_test_proof_unavailable')
    expected_tree = artifact.get('source_tree_sha256')
    if not isinstance(expected_tree, str) or not HEX.fullmatch(expected_tree):
        raise QAError('released_source_tree_unobserved')
    if digest(hashes) != expected_tree:
        raise QAError('released_source_tree_mismatch')
    if path not in hashes:
        # The registered controller test is a required file for these workflows. Other tests do
        # not replace it. A user-supplied plan referencing a different missing file is unverifiable.
        registered_tests = {
            'order': ORDER_TEST,
            'catalog': CATALOG_TEST,
            'notification': 'tests/test_business.py',
            'payment': 'tests/test_business.py',
            'delivery': DELIVERY_TEST,
        }
        if registered_tests.get(profile['service']) == path:
            raise QAError('required_controller_test_missing', 'tests_missing')
        raise QAError('qa_test_ref_mismatch')
    try:
        file = safe_path(root, path)
        if file.stat().st_size > 512000:
            raise QAError('developer_test_proof_invalid')
        raw = file.read_bytes()
        sha = hashlib.sha256(raw).hexdigest()
        if sha != hashes[path]:
            raise QAError('developer_test_proof_mismatch')
        return raw.decode('utf-8'), sha
    except (OSError, UnicodeError, ValueError):
        raise QAError('developer_test_proof_unavailable') from None


def _order_flows(source):
    # Accept only the supported, unconditional MockMvc fixture / request / result assertions.
    # Test sources are parsed, never imported or executed with the runner identity.
    source = re.sub(r'/\*.*?\*/|^\s*//[^\n]*', '', source, flags=re.M | re.S)
    if '@Disabled' in source:
        raise QAError('controller_assertions_not_mapped')
    methods = re.finditer(
        r'@Test\s+(?:public\s+)?void\s+(\w+)\s*\(\s*\)\s*(?:throws\s+Exception\s*)?\{(.*?)\n\s*\}', source, re.S
    )
    flows = {}
    for match in methods:
        name, body = match.groups()
        body = re.sub(r'^\s*//[^\n]*', '', body, flags=re.M).strip()
        compact = re.sub(r'\s+', '', body)
        perform = r'mvc\.perform\(get\("/api/orders/check"\)\.param\("item","' + ITEM + r'"\)\)'
        good = re.fullmatch(
            r'server\.expect\(requestTo\("http://inventory/api/stock/'
            + ITEM
            + r'"\)\)\.andRespond\(withSuccess\("((?:\\.|[^"\\])*)",MediaType\.APPLICATION_JSON\)\);'
            + perform
            + r'\.andExpect\(status\(\)\.isOk\(\)\)\.andExpect\(jsonPath\("\$\.orderable"\)\.value\((true|false)\)\)'
            r'\.andExpect\(jsonPath\("\$\.inventory\.quantity"\)\.value\((\d+)\)\);',
            compact,
        )
        missing = re.fullmatch(
            r'server\.expect\(ExpectedCount\.once\(\),requestTo\("http://inventory/api/stock/'
            + ITEM
            + r'"\)\)\.andRespond\(withResourceNotFound\(\)\);'
            + perform
            + r'\.andExpect\(status\(\)\.isNotFound\(\)\);server\.verify\(\);',
            compact,
        )
        if good:
            try:
                fixture = json.loads(json.loads('"' + good[2] + '"'))
            except ValueError:
                continue
            if (
                good[1] == good[3] == fixture.get('item')
                and type(fixture.get('quantity')) is int
                and fixture['quantity'] == int(good[5])
                and fixture.get('available') is (good[4] == 'true')
            ):
                flows['stock'] = {'test_id': name, 'value': good[3], 'status': 200, 'scenario': 'stock'}
        if missing and missing[1] == missing[2]:
            flows['unknown'] = {'test_id': name, 'value': missing[2], 'status': 404, 'scenario': 'unknown'}
    if set(flows) != {'stock', 'unknown'}:
        raise QAError('controller_assertions_not_mapped')
    return list(flows.values())


def _catalog_flows(source):
    try:
        tree = ast.parse(source)
    except SyntaxError:
        raise QAError('controller_assertions_not_mapped') from None
    method = next(
        (
            n
            for cls in tree.body
            if isinstance(cls, ast.ClassDef) and cls.name == 'CatalogTests'
            for n in cls.body
            if isinstance(n, ast.FunctionDef) and n.name == 'test_search_detail_and_prices'
        ),
        None,
    )
    if not method or method.decorator_list:
        raise QAError('controller_assertions_not_mapped')
    lines = [ast.unparse(n) for n in method.body]
    # Literal values derive from developer assertions. No variables, callbacks or arbitrary paths are evaluated.
    if len(lines) < 8:
        raise QAError('controller_assertions_not_mapped')
    query = re.fullmatch(
        r"status, result, _ = self\.request\('/api/products\?q=' \+ quote\('([^']{1,32})'\)\)", lines[0]
    )
    total = re.fullmatch(r"self\.assertEqual\(result\['total'\], (\d+)\)", lines[2])
    price = re.fullmatch(r"self\.assertEqual\(result\['products'\]\[0\]\['price'\], (\d+)\)", lines[3])
    detail = re.fullmatch(r"status, result, _ = self\.request\('/api/products/(\d+)'\)", lines[4])
    description = re.fullmatch(r"self\.assertIn\('([^']{1,32})', result\['product'\]\['description'\]\)", lines[6])
    stock = re.fullmatch(r"self\.assertEqual\(result\['product'\]\['stock'\], (\d+)\)", lines[7])
    if (
        not all([query, total, price, detail, description, stock])
        or lines[1] != lines[5]
        or lines[1] != 'self.assertEqual(status, 200)'
    ):
        raise QAError('controller_assertions_not_mapped')
    return [
        {
            'scenario': 'product',
            'test_id': method.name,
            'value': query[1],
            'total': int(total[1]),
            'price': int(price[1]),
            'id': detail[1],
            'description': description[1],
            'stock': int(stock[1]),
            'status': 200,
        }
    ]


def _order_plan(profile, artifact, origin):
    source, sha = _pinned_test(profile, ORDER_TEST, artifact)
    flows = []
    for case in _order_flows(source):
        value = case['value']
        ref = {'test_id': case['test_id'], 'file': ORDER_TEST, 'file_sha256': sha}
        checks = [{'response_path': 'item', 'dom_label': '상품'}]
        compare = []
        if case['status'] == 200:
            checks.extend(
                [
                    {
                        'response_path': 'orderable',
                        'dom_label': '주문 가능 여부',
                        'map': {'true': '주문 가능', 'false': '주문 불가'},
                    },
                    {'response_path': 'inventory.quantity', 'dom_label': '재고.수량'},
                    {'response_path': 'inventory.item', 'dom_label': '재고.상품'},
                    {
                        'response_path': 'inventory.available',
                        'dom_label': '재고.판매가능',
                        'map': {'true': '가능', 'false': '불가'},
                    },
                ]
            )
            compare = [
                {'response_path': left, 'read_path': right}
                for left, right in (
                    ('item', 'item'),
                    ('orderable', 'available'),
                    ('inventory.quantity', 'quantity'),
                    ('inventory.item', 'item'),
                    ('inventory.available', 'available'),
                )
            ]
        else:
            checks.append(
                {
                    'response_path': 'error',
                    'dom_label': '오류',
                    'map': {'unknown item': '등록되지 않은 상품입니다.'},
                }
            )
        flows.append(
            {
                'id': case['test_id'],
                'source_tests': [ref],
                'input': {'selector': '#item', 'value': value},
                'submit': {'selector': '#order button'},
                'result': {'selector': '#response'},
                'expectations': checks,
                'request': {
                    'method': 'GET',
                    'path': '/api/orders/check',
                    'query': {'item': value},
                    'status': case['status'],
                },
                'independent_read': {'service': 'inventory', 'path': '/api/stock/' + value, 'compare': compare},
            }
        )
    return {'schema': 'qa-plan/v1', 'environment': 'dev', 'service': 'order', 'ui_url': origin, 'flows': flows}


def _delivery_case(source):
    source = re.sub(r'/\*.*?\*/|^\s*//[^\n]*', '', source, flags=re.M | re.S)
    method = re.search(
        r"^test\('" + re.escape(DELIVERY_CASE) + r"',\s*async t\s*=>\s*\{(.*?)^\}\);", source, re.M | re.S
    )
    if (
        not method
        or "constdelayed=await(awaitrequest('/api/deliveries?delayed=true')).json();"
        not in re.sub(r'\s+', '', method[1])
        or 'assert.ok(delayed.deliveries.length>0)' not in re.sub(r'\s+', '', method[1])
    ):
        raise QAError('controller_assertions_not_mapped')
    return method[1]


def _read_plan(profile, artifact, origin):
    """Extract one read assertion from the controller test. No test setup writes are replayed on dev."""
    service = profile['service']
    file = DELIVERY_TEST if service == 'delivery' else 'tests/test_business.py'
    source, sha = _pinned_test(profile, file, artifact)
    selector = '#refresh'
    query = {}
    if service == 'notification':
        name, route, field, result = (
            'test_enqueue_and_complete_all_channels',
            '/api/notifications',
            'queue_depth',
            '#stat-one',
        )
        expression = "self.assertEqual(self.request('/api/notifications')[1]['queue_depth'], 3)"
    elif service == 'payment':
        name, route, field, result = (
            'test_history_has_fixed_retention_and_monotonic_ids',
            '/api/payments',
            'payments.length',
            '#records',
        )
        expression = "rows = self.request('/api/payments')[1]['payments']"
    else:
        _delivery_case(source)
        name, route, field, result = DELIVERY_TEST_ID, '/api/deliveries', 'deliveries.length', '#count'
        selector = '#delayed'
        query = {'search': '', 'delayed': 'true'}
    if service != 'delivery':
        try:
            method = next(
                n
                for cls in ast.parse(source).body
                if isinstance(cls, ast.ClassDef)
                for n in cls.body
                if isinstance(n, ast.FunctionDef) and n.name == name and not n.decorator_list
            )
        except (SyntaxError, StopIteration):
            raise QAError('controller_assertions_not_mapped') from None
        lines = {ast.unparse(node) for node in method.body}
        if expression not in lines or (
            service == 'payment'
            and not {
                'self.assertEqual(len(rows), 100)',
                "self.assertEqual(len({row['id'] for row in rows}), 100)",
            }.issubset(lines)
        ):
            raise QAError('controller_assertions_not_mapped')
    return {
        'schema': 'qa-plan/v1',
        'environment': 'dev',
        'service': service,
        'ui_url': origin,
        'flows': [
            {
                'id': 'read-business-status',
                'name_ko': '업무 목록 조회',
                'source_tests': [{'test_id': name, 'file': file, 'file_sha256': sha}],
                'input': None,
                'submit': {'selector': selector},
                'result': {'selector': result},
                'request': {'method': 'GET', 'path': route, 'query': query},
                'expectations': [
                    {
                        'response_path': field,
                        'dom_label': {
                            'payment': '결제 내역 수',
                            'notification': '알림 대기 수',
                            'delivery': '지연 배송 수',
                        }[service],
                    }
                ],
                'independent_read': {
                    'service': service,
                    'path': route + ('?delayed=true' if service == 'delivery' else ''),
                    'compare': [{'response_path': field, 'read_path': field}],
                },
            }
        ],
    }


def _validate_plan_tests(plan, profile, artifact):
    refs = []
    for flow in plan['flows']:
        for ref in flow['source_tests']:
            file = ref['file']
            if not file.startswith(profile['test_root'] + '/'):
                raise QAError('qa_test_ref_outside_test_tree')
            source, sha = _pinned_test(profile, file, artifact)
            test = ref['test_id'].split('::')[-1]
            delivery_test = file == DELIVERY_TEST and profile['service'] == 'delivery' and test == DELIVERY_TEST_ID
            if (
                not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:#/-]{0,199}', ref['test_id'])
                or sha != ref['file_sha256']
                or (not delivery_test and not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,120}', test))
            ):
                raise QAError('qa_test_ref_mismatch')
            if file.endswith('.py'):
                try:
                    method = next(
                        n
                        for n in ast.walk(ast.parse(source))
                        if isinstance(n, ast.FunctionDef) and n.name == test and not n.decorator_list
                    )
                except (StopIteration, SyntaxError):
                    raise QAError('qa_test_ref_mismatch') from None
                body = ast.unparse(method)
            elif delivery_test:
                body = _delivery_case(source)
            else:
                matched = re.search(
                    r'@Test\s+(?:public\s+)?void\s+' + re.escape(test) + r'\s*\([^)]*\).*?\{(.*?)\n\s*\}',
                    source,
                    re.S,
                )
                if not matched:
                    raise QAError('qa_test_ref_mismatch')
                body = matched[1]
            if flow['request']['path'] not in body or not any(v in body for v in ('assert', 'andExpect')):
                raise QAError('qa_test_route_not_grounded')
            checks = {c['response_path'].removeprefix('$.') for c in flow['expectations']}
            service = profile['service']
            if service == 'order':
                # Bind both the input and expected status to the named controller scenario.
                status = flow['request'].get('status', flow.get('expected_status', 200))
                known = _order_flows(source)
                if not any(
                    c['test_id'] == test and c['value'] == flow['input']['value'] and c['status'] == status
                    for c in known
                ):
                    raise QAError('qa_test_scenario_not_grounded')
            else:
                # A test that only visits an endpoint does not attest to every response field.
                # Keep the field name and assertion in the same pinned method.
                for field in checks:
                    if field.split('.')[0] not in body or (
                        '.length' in field and 'len(' not in body and '.length' not in body
                    ):
                        raise QAError('qa_test_assertion_not_grounded')
            validated = {'test_id': ref['test_id'], 'file': file, 'file_sha256': sha}
            if validated not in refs:
                refs.append(validated)
    return refs


def run_dev_ui_qa(*, job, profile, evidence_dir):
    """Return a qa-browser/v1 receipt. Profile paths, origins and inventory reader are trusted runner configuration."""
    p = job.get('payload', job)
    artifact = p.get('artifact') or {}
    image = p.get('image') or artifact.get('image')
    observed = p.get('observed_image') or (p.get('dev_verification') or {}).get('image')
    qa = {
        'protocol': 'qa-browser/v1',
        'release_id': p.get('release_id'),
        'artifact_sha256': p.get('artifact_sha256'),
        'plan_sha256': p.get('plan_sha256'),
        'image': image,
        'observed_image': observed,
        'ui_url': None,
        'developer_tests': [],
        'steps': [],
        'observed_at': time.time(),
    }
    try:
        service, system = profile['service'], profile['system']
        if (
            p.get('stage', p.get('environment')) != 'dev'
            or p.get('service') != service
            or p.get('profile_id', profile['id']) != profile['id']
        ):
            raise QAError('qa_target_mismatch')
        if (
            not isinstance(qa['release_id'], str)
            or not qa['release_id']
            or not all(isinstance(qa[k], str) and HEX.fullmatch(qa[k]) for k in ('artifact_sha256', 'plan_sha256'))
            or not image
            or image != observed
        ):
            raise QAError('qa_release_fingerprint_missing')
        manifest_image(image, profile['image_repository'])
        if artifact.get('image') != image:
            raise QAError('qa_artifact_image_mismatch')
        source = profile.get('qa_source') or {}
        if source.get('image', image) != image or source.get('profile_id', profile['id']) != profile['id']:
            raise QAError('qa_source_provenance_mismatch')
        origin = _origin(
            profile.get('qa_ui_origin') or f'http://{service}.{system}.dev.localhost', service, system
        )
        qa['ui_url'] = origin + '/'
        supplied_plan = p.get('qa_plan')
        if supplied_plan or service in ('order', 'notification', 'payment', 'delivery'):
            plan = supplied_plan or (_order_plan if service == 'order' else _read_plan)(profile, artifact, origin)
            if plan.get('service') != service or plan.get('ui_url', '').rstrip('/') != origin:
                raise QAError('qa_plan_target_mismatch')
            qa['developer_tests'] = _validate_plan_tests(plan, profile, artifact)
            if p.get('required_tests') and not set(p['required_tests']).issubset(
                r['test_id'] for r in qa['developer_tests']
            ):
                raise QAError('required_controller_workflow_unmapped')
            reader = profile.get('qa_independent_read')
            if not callable(reader) and service == 'order' and callable(profile.get('qa_inventory_read')):
                reader = lambda dep, path: profile['qa_inventory_read'](path.rsplit('/', 1)[-1])
            if not callable(reader):
                # Only the service's registered read endpoint can be visited for an independent observation.
                from urllib.request import build_opener, ProxyHandler
                from .worker import NoRedirect

                def reader(dep, path):
                    registered = _UI[service].get('independent_path', _UI[service]['path'])
                    if dep != service or (
                        path != registered
                        and not (service == 'catalog' and re.fullmatch(r'/api/products/[1-9][0-9]{0,7}', path))
                    ):
                        raise QAError('independent_dev_reader_required')
                    with build_opener(ProxyHandler({}), NoRedirect()).open(origin + path, timeout=8) as response:
                        raw = response.read(1000001)
                        if len(raw) > 1000000:
                            raise QAError('qa_independent_response_too_large')
                        return {'status': response.status, 'body': json.loads(raw)}

            result = run_plan(plan, evidence_dir=evidence_dir, independent_read=reader, registered_origin=origin)
            qa.update(outcome=result['outcome'], reason=result.get('reason'), flows=result['flows'])
            if result.get('qa_plan'):
                qa['qa_plan'] = result['qa_plan']
            return _result(qa)
        adapter = {'catalog': (CATALOG_TEST, _catalog_flows)}.get(service)
        if not adapter:
            if service in ('business-api', 'trade-member', 'delivery-member'):
                raise QAError('domain_status_screen_has_no_verified_controller_scenario')
            if service == 'inventory':
                raise QAError('inventory_api_has_no_registered_business_screen')
            raise QAError('registered_readonly_workflow_unavailable')
        file, compile_flows = adapter
        source, sha = _pinned_test(profile, file, artifact)
        flows = compile_flows(source)
        qa['developer_tests'] = [{'test_id': f['test_id'], 'file': file, 'file_sha256': sha} for f in flows]
        qa['qa_plan'] = {
            'schema': 'qa-plan/v1',
            'environment': 'dev',
            'service': service,
            'ui_url': origin,
            'flows': [
                {
                    'id': f['test_id'],
                    'name_ko': '상품 검색·상세 확인',
                    'source_tests': [ref],
                    'input': {'selector': '#query', 'value': f['value']},
                    'submit': {'selector': '#submit-business'},
                    'request': {
                        'method': 'GET',
                        'path': '/api/products',
                        'query': {'q': f['value']},
                        'status': f['status'],
                    },
                    'result': {'selector': '#product-detail'},
                    'expectations': [
                        {'dom_label': '가격', 'expected': f['price']},
                        {'dom_label': '재고', 'expected': f['stock']},
                    ],
                    'search_count': f['total'],
                    'detail_path': '/api/products/' + f['id'],
                    'independent_read': {'service': 'catalog', 'path': '/api/products/' + f['id']},
                }
                for f, ref in zip(flows, qa['developer_tests'])
            ],
        }
        if p.get('required_tests') and not set(p['required_tests']).issubset({f['test_id'] for f in flows}):
            raise QAError('required_controller_workflow_unmapped')
        directory = Path(evidence_dir)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if directory.is_symlink():
            raise QAError('evidence_directory_invalid')
        from tempfile import mkdtemp

        run_directory = Path(mkdtemp(prefix='dev-qa-', dir=directory))
        observation = _browser(
            {'service': service, 'origin': origin, 'flows': flows, 'evidenceDir': str(run_directory.resolve())}
        )
        qa.update(observation)
    except QAError as exc:
        qa.update(outcome=exc.outcome, reason=exc.code)
    except (OSError, ValueError, TypeError, KeyError):
        qa.update(outcome='unverifiable', reason='qa_inputs_or_runtime_unavailable')
    return _result(qa)


def _result(qa):
    plan = qa.get('qa_plan')
    if plan:
        plan.update(
            {k: qa[k] for k in ('release_id', 'artifact_sha256', 'plan_sha256', 'image', 'observed_image')}
        )
        flat = []
        for flow in qa.get('flows') or []:
            contract = next(row for row in plan['flows'] if row['id'] == flow['id'])
            _describe_steps(flow, contract)
            flat.extend(dict(step, id=flow['id'] + '/' + step['id']) for step in flow['steps'])
        qa['steps'] = flat
    if not qa['steps']:
        qa['steps'] = [
            {'id': 'dev_ui', 'status': 'not_run', 'summary_ko': '브라우저 업무 흐름을 실행할 근거가 부족합니다.'}
        ]
    qa['observed_at'] = time.time()
    qa['receipt_id'] = digest(qa)
    return {
        'ok': qa['outcome'] == 'passed',
        'write_outcome': 'not_written',
        'qa': qa,
        'error': None if qa['outcome'] == 'passed' else qa.get('reason', qa['outcome']),
    }


def _describe_steps(flow, contract):
    ref = contract['source_tests'][0]
    network = flow.get('network') or {}
    dom = (flow.get('dom') or {}).get('values', {})
    independent = flow.get('independent_read') or {}
    request = contract['request']
    input_value = (contract.get('input') or {}).get('value')
    labels = ' · '.join(c['dom_label'] for c in contract['expectations'])
    names = {
        'open': ('등록된 dev 업무 화면을 엽니다.', '등록된 dev 화면을 열었습니다.'),
        'input': (
            f'개발자 시험의 입력값 {input_value}를 입력합니다.',
            f'입력 항목의 값 {input_value}를 확인했습니다.',
        ),
        'submit': (
            '선택한 조회가 업무 요청을 발생시켜야 합니다.',
            '선택한 조회에서 발생한 GET 요청을 확인했습니다.',
        ),
        'network': (
            f"업무 요청은 HTTP {request['status']}로 응답해야 합니다.",
            f"HTTP {network.get('status')} 응답을 받았습니다.",
        ),
        'search_dom': (
            f"검색 결과 {contract.get('search_count')}건을 표시해야 합니다.",
            '검색 결과 건수와 첫 상품 이름을 대조했습니다.',
        ),
        'detail_submit': ('검색한 상품의 상세를 열어야 합니다.', '선택한 상품의 상세 요청을 확인했습니다.'),
        'detail_network': (
            '상세 응답의 상품 ID·설명·가격·재고가 개발자 시험과 일치해야 합니다.',
            '상품 상세 응답이 개발자 시험과 일치합니다.',
        ),
        'dom': ('화면값을 업무 응답과 대조합니다. ' + labels, ' · '.join(f'{k} {v}' for k, v in dom.items())),
        'independent_read': (
            contract['independent_read']['service'] + '의 별도 조회 결과와 대조합니다.',
            f"독립 조회 HTTP {independent.get('status')} · 업무 응답과 일치합니다.",
        ),
    }
    for step in flow['steps']:
        expected, actual = names[step['id']]
        step.update(ref, expected_ko=expected)
        step['summary_ko'] = (
            actual
            if step['status'] == 'passed'
            else {
                'failed': '업무 값이 기대 결과와 일치하지 않았습니다.',
                'unverifiable': '업무 결과를 확인할 수 없습니다.',
                'not_run': '앞 단계에서 중단되어 실행하지 않았습니다.',
            }[step['status']]
        )


def _browser(plan):
    script = Path(__file__).with_name('dev_ui_qa.mjs')
    # No cluster, repository, model or machine-token environment is inherited by the web browser.
    env = {
        k: os.environ[k] for k in ('PATH', 'HOME', 'TMPDIR', 'LANG', 'PLAYWRIGHT_BROWSERS_PATH') if k in os.environ
    }
    try:
        result = subprocess.run(
            ['node', str(script)], input=json.dumps(plan), text=True, capture_output=True, timeout=80, env=env
        )
        if result.returncode or len(result.stdout) > 160000:
            raise QAError('qa_browser_runtime_unavailable')
        data = json.loads(result.stdout)
        if data.get('outcome') not in ('passed', 'failed', 'unverifiable') or not isinstance(
            data.get('steps'), list
        ):
            raise QAError('qa_browser_receipt_invalid')
        return data
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        raise QAError('qa_browser_runtime_unavailable') from None


# These capabilities are operator-owned. Model plans cannot grant a new origin, click or HTTP method.
_UI = {
    'order': {
        'system': 'samsung-trade',
        'input': {'#item'},
        'submit': {
            'button[type="submit"]',
            'button[type=submit]',
            '#order button',
            '#order button[type="submit"]',
            '#order button[type=submit]',
        },
        'result': {'#response'},
        'path': '/api/orders/check',
        'query': 'item',
    },
    'notification': {
        'system': 'samsung-delivery',
        'input': set(),
        'submit': {'#refresh'},
        'result': {'#stat-one', '#stat-two'},
        'path': '/api/notifications',
        'query': None,
    },
    'payment': {
        'system': 'samsung-trade',
        'input': set(),
        'submit': {'#refresh'},
        'result': {'#stat-one', '#stat-two', '#records'},
        'path': '/api/payments',
        'query': None,
    },
    'delivery': {
        'system': 'samsung-delivery',
        'input': set(),
        'submit': {'#delayed'},
        'result': {'#count'},
        'path': '/api/deliveries',
        'query': None,
        'fixed_query': {'search': '', 'delayed': 'true'},
        'independent_path': '/api/deliveries?delayed=true',
    },
    'catalog': {
        'system': 'samsung-trade',
        'input': {'#query'},
        'submit': {'#submit-business'},
        'result': {'#records', '#stat-one'},
        'path': '/api/products',
        'query': 'q',
    },
}


def _field(value):
    if (
        not isinstance(value, str)
        or len(value) > 120
        or not re.fullmatch(r'(?:\$\.)?[\w가-힣 -]+(?:\.[\w가-힣 -]+){0,5}', value)
    ):
        raise QAError('qa_field_not_registered')
    fields = value.removeprefix('$.').split('.')
    if any(k.startswith('_') or k in ('prototype', 'constructor') for k in fields):
        raise QAError('qa_field_not_registered')
    return fields


def _get_path(doc, keys):
    for key in keys:
        if isinstance(doc, list) and key == 'length':
            doc = len(doc)
        elif isinstance(doc, list) and key.isdigit() and int(key) < len(doc):
            doc = doc[int(key)]
        elif isinstance(doc, dict) and key in doc:
            doc = doc[key]
        else:
            raise QAError('qa_independent_field_unobserved')
    if type(doc) not in (bool, int, float, str) or isinstance(doc, str) and len(doc) > 128:
        raise QAError('qa_independent_field_unobserved')
    return doc


def _flow(plan, f, independent_read):
    from urllib.parse import urlencode

    service = plan['service']
    caps = _UI[service]
    identifier = f['id']
    if not re.fullmatch(r'[A-Za-z0-9._-]{1,100}', identifier):
        raise QAError('qa_flow_id_invalid')
    refs = f.get('source_tests')
    if (
        not isinstance(refs, list)
        or not 1 <= len(refs) <= 16
        or any(
            not isinstance(r, dict)
            or not isinstance(r.get('test_id'), str)
            or not r['test_id']
            or not isinstance(r.get('file'), str)
            or not r['file']
            or '..' in Path(r['file']).parts
            or Path(r['file']).is_absolute()
            or not HEX.fullmatch(str(r.get('file_sha256', '')))
            for r in refs
        )
    ):
        raise QAError('qa_test_ref_required')
    request = f['request']
    key = caps['query']
    input_action = f.get('input')
    if key:
        value = input_action['value']
        if (
            not isinstance(value, str)
            or not re.fullmatch(r'[A-Za-z0-9가-힣 _-]{1,40}', value)
            or input_action['selector'] not in caps['input']
        ):
            raise QAError('qa_input_unregistered')
    else:
        if input_action not in (None, {}):
            raise QAError('qa_input_unregistered')
        value = None
    query = {key: value} if key else caps.get('fixed_query', {})
    if (
        f['submit']['selector'] not in caps['submit']
        or f['result']['selector'] not in caps['result']
        or request.get('method') != 'GET'
        or request.get('path') != caps['path']
        or request.get('query', {}) != query
    ):
        raise QAError('qa_action_not_registered')
    expected = request.get('status', f.get('expected_status', 200))
    if expected not in ((200, 404) if service == 'order' else (200,)):
        raise QAError('qa_status_not_registered')
    checks = f['expectations']
    if not isinstance(checks, list) or not 1 <= len(checks) <= 16:
        raise QAError('qa_dom_checks_required')
    checks = [
        {'path': _field(c['response_path']), 'label': _field(c['dom_label']), 'map': c.get('map')} for c in checks
    ]
    for c in checks:
        mapping = c['map']
        if mapping is not None and (
            not isinstance(mapping, dict)
            or len(mapping) > 16
            or any(
                type(k) != str or type(v) not in (str, int, bool) or len(str(v)) > 120 for k, v in mapping.items()
            )
        ):
            raise QAError('qa_dom_checks_invalid')
    independent = f['independent_read']
    dep = independent['service']
    route = independent['path']
    if service == 'order':
        if dep != 'inventory' or route != '/api/stock/' + value:
            raise QAError('qa_independent_route_unregistered')
    elif service == 'catalog' and (dep != 'catalog' or not re.fullmatch(r'/api/products/[1-9][0-9]{0,7}', route)):
        raise QAError('qa_independent_route_unregistered')
    elif service in ('payment', 'notification', 'delivery') and (
        dep != service or route != caps.get('independent_path', caps['path'])
    ):
        raise QAError('qa_independent_route_unregistered')
    comparisons = independent.get('compare') or []
    if not isinstance(comparisons, list) or len(comparisons) > 16 or (not comparisons and expected != 404):
        raise QAError('qa_independent_checks_required')
    bindings = [(_field(c['response_path']), _field(c['read_path'])) for c in comparisons]
    joined = {('.'.join(left), '.'.join(right)) for left, right in bindings}
    if service != 'order':
        # Never send account identifiers or business row bodies into an evidence job.
        permitted = {
            'payment': {
                ('payments.length', 'payments.length'),
                ('processed', 'processed'),
                ('charged_amount', 'charged_amount'),
            },
            'notification': {('queue_depth', 'queue_depth'), ('sent', 'sent')},
            'delivery': {('deliveries.length', 'deliveries.length')},
            'catalog': {
                ('products.0.id', 'product.id'),
                ('products.0.price', 'product.price'),
                ('products.0.stock', 'product.stock'),
            },
        }[service]
        if not joined.issubset(permitted) or any(c['map'] for c in checks):
            raise QAError('qa_business_binding_unregistered')
        if service != 'catalog' and not {('.'.join(c['path']), '.'.join(c['path'])) for c in checks}.issubset(
            joined
        ):
            raise QAError('qa_independent_checks_incomplete')
    else:
        permitted = {
            'item': ('상품', None),
            'inventory.item': ('재고.상품', None),
            'inventory.quantity': ('재고.수량', None),
            'orderable': ('주문 가능 여부', {'true': '주문 가능', 'false': '주문 불가'}),
            'inventory.available': ('재고.판매가능', {'true': '가능', 'false': '불가'}),
            'error': ('오류', {'unknown item': '등록되지 않은 상품입니다.'}),
        }
        if any(permitted.get('.'.join(c['path'])) != ('.'.join(c['label']), c['map']) for c in checks):
            raise QAError('qa_business_binding_unregistered')
        if not joined.issubset(
            {
                ('item', 'item'),
                ('inventory.item', 'item'),
                ('inventory.available', 'available'),
                ('orderable', 'available'),
                ('inventory.quantity', 'quantity'),
            }
        ):
            raise QAError('qa_business_binding_unregistered')
        if expected == 404 and 'error' not in {'.'.join(c['path']) for c in checks}:
            raise QAError('qa_order_business_checks_incomplete')
    if service == 'order' and expected == 200:
        # The order display includes BOTH orderability and the real quantity. Checking only the item ID is insufficient.
        if not {('orderable', 'available'), ('inventory.quantity', 'quantity')}.issubset(joined) or not {
            'orderable',
            'inventory.quantity',
        }.issubset({'.'.join(c['path']) for c in checks}):
            raise QAError('qa_order_business_checks_incomplete')
    try:
        response = independent_read(dep, route)
    except Exception:
        raise QAError('qa_independent_read_unavailable') from None
    if not isinstance(response, dict) or response.get('status') != expected:
        raise QAError('qa_independent_read_unavailable')
    pairs = [{'path': a, 'expected': _get_path(response.get('body'), b)} for a, b in bindings]
    return {
        'scenario': 'planned',
        'id': identifier,
        'test_id': refs[0]['test_id'],
        'service': service,
        'source_tests': refs,
        'value': value,
        'input': input_action['selector'] if key else None,
        'submit': f['submit']['selector'],
        'result': f['result']['selector'],
        'path': request['path'] + ('?' + urlencode(query) if query else ''),
        'status': expected,
        'checks': checks,
        'bindings': pairs,
        'independent_status': response['status'],
        'independent_sha256': digest(response.get('body')),
    }


def run_plan(plan, *, evidence_dir, independent_read, registered_origin=None):
    """Execute an already source-validated qa-plan/v1 within narrower local browser capabilities."""
    result = {'schema': 'qa-browser/v1', 'ui_url': None, 'observed_at': time.time(), 'flows': []}
    try:
        if (
            plan.get('schema') != 'qa-plan/v1'
            or plan.get('environment') != 'dev'
            or plan.get('service') not in _UI
        ):
            raise QAError('qa_dev_workflow_not_registered')
        caps = _UI[plan['service']]
        origin = _origin(plan['ui_url'], plan['service'], caps['system'])
        registered = registered_origin or f"http://{plan['service']}.{caps['system']}.dev.localhost"
        if origin != registered.rstrip('/'):
            raise QAError('qa_dev_origin_not_registered')
        rows = plan['flows']
        if not isinstance(rows, list) or not 1 <= len(rows) <= 8 or len({f['id'] for f in rows}) != len(rows):
            raise QAError('qa_flows_required')
        result['ui_url'] = origin + '/'
        flows = [_flow(plan, f, independent_read) for f in rows]
        # Persist only the registered controls and validated test references. A client-supplied
        # display name or arbitrary extra plan field is never copied into the release history.
        names = {
            'order': '주문 가능·재고 확인',
            'catalog': '상품 검색 확인',
            'payment': '결제 내역 조회',
            'notification': '알림 대기 조회',
            'delivery': '지연 배송 조회',
        }
        safe_flows = []
        for f, compiled in zip(rows, flows):
            independent = f['independent_read']
            refs = [{k: t[k] for k in ('file', 'file_sha256', 'test_id')} for t in f['source_tests']]
            safe_flows.append(
                {
                    'id': compiled['id'],
                    'name_ko': '미등록 상품 조회' if compiled['status'] == 404 else names[plan['service']],
                    'source_tests': refs,
                    'input': (
                        {'selector': compiled['input'], 'value': compiled['value']} if compiled['input'] else None
                    ),
                    'submit': {'selector': compiled['submit']},
                    'result': {'selector': compiled['result']},
                    'request': {
                        'method': 'GET',
                        'path': f['request']['path'],
                        'query': f['request'].get('query', {}),
                        'status': compiled['status'],
                    },
                    'expectations': [
                        {'response_path': '.'.join(c['path']), 'dom_label': '.'.join(c['label'])}
                        for c in compiled['checks']
                    ],
                    'independent_read': {'service': independent['service'], 'path': independent['path']},
                }
            )
        result['qa_plan'] = {
            'schema': 'qa-plan/v1',
            'ui_url': origin,
            'environment': 'dev',
            'service': plan['service'],
            'flows': safe_flows,
        }
        from tempfile import mkdtemp

        directory = Path(evidence_dir)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if directory.is_symlink():
            raise QAError('evidence_directory_invalid')
        run_directory = Path(mkdtemp(prefix='dev-qa-', dir=directory))
        observed = _browser(
            {
                'service': plan['service'],
                'origin': origin,
                'flows': flows,
                'evidenceDir': str(run_directory.resolve()),
            }
        )
        result.update(outcome=observed['outcome'], flows=observed['flows'], reason=observed.get('reason'))
    except QAError as exc:
        result.update(outcome=exc.outcome, reason=exc.code)
    except (ValueError, OSError, KeyError, TypeError):
        result.update(outcome='unverifiable', reason='qa_plan_unverifiable')
    return result
