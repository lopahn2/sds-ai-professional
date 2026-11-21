"""LiveProbe for Application 관제 화면 정적 파일의 순수 시험. 서버·네트워크·브라우저를 쓰지 않는다."""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / 'app' / 'application' / 'static'
CONTROL_SOURCES = [ROOT / 'app' / 'application' / name for name in ('control.py', 'releases.py', 'worker.py')]
PREFIX = '/api/application/v1'
REQUIRED_IDS = (
    'readiness', 'live-status', 'error-banner', 'create-form',
    'incident-list', 'incident-detail', 'stage-timeline', 'evidence-table', 'analysis-roles',
    'patch-diff', 'test-results', 'artifacts', 'env-status', 'release-explain',
    'user-test-form', 'approve-form', 'approve-digest',
)
API_PATHS = ('/incidents', '/catalog', '/readyz', '/healthz', '/summaries')


def read(name):
    return (STATIC / name).read_text(encoding='utf-8')


def without_comments(js):
    # 문자열 안의 '//'와 구분하기 위해 줄 시작 주석과 블록 주석만 제거한다.
    js = re.sub(r'/\*.*?\*/', '', js, flags=re.S)
    return re.sub(r'(?m)^\s*//.*$', '', js)


def control_states():
    states = set()
    for path in CONTROL_SOURCES:
        text = path.read_text(encoding='utf-8')
        states.update(re.findall(r"transition\(session, incident, '([a-z_]+)'", text))
        states.update(re.findall(r"stop\(session, incident, [^\n]*?, '([a-z_]+)'\)", text))
        states.update(re.findall(r"\bstate='([a-z_]+)'", text))
        # stage + '_x' 전이는 dev/stg/prd 세 환경 모두에서 일어날 수 있다.
        for suffix in re.findall(r"transition\(session, incident, stage \+ '(_[a-z_]+)'", text):
            states.update(stage + suffix for stage in ('dev', 'stg', 'prd'))
    return states


class StaticFilesExist(unittest.TestCase):
    def test_three_static_files_exist(self):
        for name in ('index.html', 'app.js', 'styles.css'):
            self.assertTrue((STATIC / name).is_file(), name)


class NoExternalResources(unittest.TestCase):
    def test_no_external_url_in_any_file(self):
        for name in ('index.html', 'app.js', 'styles.css'):
            text = read(name)
            # The other local product is user-initiated navigation, never a fetched subresource.
            if name == 'index.html':
                text = text.replace('<a href="http://liveprobe.localhost/" rel="noreferrer">', '<a>')
            self.assertIsNone(re.search(r'(?i)(?:https?:)?//[a-z0-9][a-z0-9.-]*\.[a-z]{2,}', text), name)
            self.assertNotRegex(text, r'(?i)https?:', name)

    def test_css_has_no_import_or_remote_font(self):
        css = read('styles.css')
        self.assertNotIn('@import', css)
        self.assertNotIn('@font-face', css)
        self.assertIsNone(re.search(r'url\(', css))

    def test_html_loads_only_local_script_and_style(self):
        html = read('index.html')
        self.assertEqual(re.findall(r'<script\b[^>]*\bsrc="([^"]+)"', html), ['app.js'])
        self.assertEqual(re.findall(r'<link\b[^>]*\bhref="([^"]+)"', html), ['styles.css'])
        self.assertIsNone(re.search(r'<script\b(?![^>]*\bsrc=)[^>]*>', html), 'inline script')
        self.assertIsNone(re.search(r'\son[a-z]+\s*=', html), 'inline handler')
        self.assertNotIn('javascript:', html)
        self.assertRegex(html, r'<html[^>]*\blang="ko"')
        self.assertRegex(html, r'<meta name="viewport" content="width=device-width, initial-scale=1"')


class NoBrowserPersistence(unittest.TestCase):
    def test_no_web_storage_cookie_or_url_state(self):
        js = read('app.js')
        for banned in ('localStorage', 'sessionStorage', 'indexedDB', 'serviceWorker',
                       'caches.', 'location.hash', 'location.search', 'pushState', 'replaceState',
                       'location.href', 'location.assign', 'window.name', 'BroadcastChannel', 'postMessage'):
            self.assertNotIn(banned, js, banned)

    def test_local_operator_needs_no_personal_role_registration(self):
        html, js = read('index.html'), read('app.js')
        self.assertNotRegex(html, r'id="[a-z]+-bearer"')
        self.assertNotIn('headers.Authorization', js)
        self.assertNotIn('document.cookie =', js)
        self.assertNotIn('human_login', js)
        for ident in ('session-scope', 'session-start', 'session-status', 'forget-auth'):
            self.assertNotIn(f'id="{ident}"', html)
        self.assertIn("credentials:'same-origin'", js)
        self.assertIn("headers['X-LiveProbe-Operator-CSRF']", js)


class FetchOnlyApplicationApi(unittest.TestCase):
    def test_single_fetch_uses_application_prefix(self):
        js = without_comments(read('app.js'))
        self.assertIn(f"const API_PREFIX = '{PREFIX}';", js)
        calls = re.findall(r'\bfetch\(([^,)]*)', js)
        self.assertEqual(calls, ['API_PREFIX + path'])
        for banned in ('XMLHttpRequest', 'WebSocket', 'EventSource', 'sendBeacon', 'import(', 'eval(', 'new Function'):
            self.assertNotIn(banned, js, banned)

    def test_every_path_literal_is_an_application_api_path(self):
        js = without_comments(read('app.js'))
        literals = re.findall(r"""['"`](/[A-Za-z][^'"`]*)""", js)
        self.assertTrue(literals)
        for literal in literals:
            ok = literal.startswith(PREFIX) or any(literal == p or literal.startswith(p + '/') or literal.startswith(p + '?')
                                                   for p in API_PATHS)
            self.assertTrue(ok, literal)


class RequiredRegions(unittest.TestCase):
    def test_required_ids_exist_once(self):
        html = read('index.html')
        for ident in REQUIRED_IDS:
            self.assertEqual(len(re.findall(rf'\bid="{ident}"', html)), 1, ident)

    def test_status_region_is_live_and_errors_are_alerts(self):
        html = read('index.html')
        self.assertRegex(html, r'id="live-status"[^>]*role="status"[^>]*aria-live="polite"')
        self.assertRegex(html, r'id="error-banner"[^>]*role="alert"')

    def test_controls_are_real_buttons(self):
        for name in ('index.html', 'app.js'):
            text = read(name)
            self.assertNotRegex(text, r'role=["\']button', name)
            self.assertNotRegex(text, r'tabindex=["\']?[1-9]', name)
            self.assertNotRegex(text, r"tabIndex\s*=\s*[1-9]", name)


class ServerTextIsNeverMarkup(unittest.TestCase):
    def test_no_html_injection_api(self):
        js = read('app.js')
        for banned in ('innerHTML', 'outerHTML', 'insertAdjacentHTML', 'document.write', 'createContextualFragment',
                       'DOMParser', 'srcdoc'):
            self.assertNotIn(banned, js, banned)
        self.assertIn('.textContent = ', js)

    def test_dom_helper_assigns_strings_as_text(self):
        js = read('app.js')
        helper = re.search(r'function el\(tag, attrs, \.\.\.children\) \{(.*?)\n\}', js, re.S)
        self.assertIsNotNone(helper)
        body = helper.group(1)
        self.assertIn('document.createTextNode', body)
        self.assertNotIn('setAttribute(\'on', body)


class StateLabelsCoverControlPlane(unittest.TestCase):
    def test_every_control_plane_state_has_korean_label(self):
        states = control_states()
        self.assertGreaterEqual(len(states), 20, sorted(states))
        for required in ('detected', 'collecting', 'source_pinning', 'analysis_queued', 'analyzing', 'patch_proposed',
                         'test_build_queued', 'dev_targets_collecting', 'awaiting_user_test', 'handoff', 'failed'):
            self.assertIn(required, states)
        js = read('app.js')
        block = re.search(r'const STATE_LABELS = \{(.*?)\};', js, re.S)
        self.assertIsNotNone(block)
        keys = set(re.findall(r"""['"]?([a-z_]+)['"]?\s*:""", block.group(1)))
        self.assertEqual(sorted(states - keys), [])

    def test_unknown_state_uses_generic_label_not_raw_value(self):
        js = read('app.js')
        self.assertIn("function stateLabel(state) { return label(STATE_LABELS,state,'상태 확인 필요'); }", js)
        self.assertIn("return Object.hasOwn(STATE_LABELS,state) ? 'automatic' : 'unknown';", js)


ENVIRONMENTS_PY = ROOT / 'app' / 'environments.py'


def py_constant(name):
    match = re.search(rf"^{name}\s*=\s*'([^']+)'", ENVIRONMENTS_PY.read_text(encoding='utf-8'), re.M)
    return match.group(1) if match else None


class ActiveEnvironmentsAndReleaseProtocol(unittest.TestCase):
    """2026-11-13 dev/prd 전환: 활성 환경 dev·prd, stg 퇴역(이력 전용), release protocol 인식형 화면."""

    def test_protocol_constants_match_shared_backend_module(self):
        js = read('app.js')
        self.assertEqual(py_constant('RELEASE_PROTOCOL'), 'dev-prd/v2')
        self.assertEqual(py_constant('LEGACY_RELEASE_PROTOCOL'), 'dev-stg-prd/v1')
        self.assertIn(f"const RELEASE_PROTOCOL = '{py_constant('RELEASE_PROTOCOL')}', "
                      f"LEGACY_RELEASE_PROTOCOL = '{py_constant('LEGACY_RELEASE_PROTOCOL')}';", js)
        self.assertIn("const ACTIVE_ENVIRONMENTS = ['dev','prd'], RETIRED_ENVIRONMENTS = ['stg'];", js)
        # v2는 dev, v1(protocol 필드 없음 포함)은 stg가 사용자 시험 환경이다(백엔드 USER_TEST_ENVIRONMENT와 같음).
        self.assertIn("const USER_TEST_ENVIRONMENT = {[RELEASE_PROTOCOL]:'dev', [LEGACY_RELEASE_PROTOCOL]:'stg'};", js)
        self.assertRegex(js, r"return r\.protocol === RELEASE_PROTOCOL \? RELEASE_PROTOCOL : LEGACY_RELEASE_PROTOCOL;")

    def test_user_test_gate_is_protocol_aware_and_legacy_is_read_only(self):
        js = without_comments(read('app.js'))
        # 예전 하드코딩(stg 고정)과 부모 임시 수정(dev 고정)을 모두 프로토콜별 계산으로 바꾼다.
        gate = re.search(r'function gates\(\) \{(.*?)\n\}', js, re.S).group(1)
        self.assertNotIn('stage_results?.stg?.verified', gate)
        self.assertNotIn('stage_results?.dev?.verified', gate)
        self.assertIn("const userTestReady = r => obj(obj(r.stage_results)[userTestEnv(r)]).verified === true;", js)
        self.assertIn("$('user-test-submit').disabled = !permits('tester') || !valid || current?.state!=='awaiting_user_test' || !userTestReady(r);", js)
        # 서버가 v1 기록을 읽기 전용(legacy_release_read_only)으로 거부하므로 화면도 제출을 열지 않는다.
        self.assertIn("valid=exactRelease(r) && !isLegacyRelease(r) && !busy", js)

    def test_no_hardcoded_stg_user_test_wording(self):
        html, js = read('index.html'), read('app.js')
        for banned in ('사용자 stg 시험', 'stg 시험자 인증 토큰', 'stg 사용자 시험 기록', 'stg 시험 기록', 'stg 시험 결과를 기록'):
            self.assertNotIn(banned, html, banned)
            self.assertNotIn(banned, js, banned)
        self.assertEqual(len(re.findall(r'class="user-test-env">dev</span>', html)), 2)
        block = re.search(r'const STATE_LABELS = \{(.*?)\};', js, re.S).group(1)
        self.assertIn("awaiting_user_test: '사용자 시험 대기'", block)
        # stg_* 상태 이름은 과거 기록 표시용으로 유지한다.
        for state in ('stg_targets_collecting', 'stg_deploy_queued', 'stg_verifying', 'stg_verified'):
            self.assertIn(state, block)

    def test_history_filter_keeps_retired_stg_marked(self):
        html = read('index.html')
        select = re.search(r'<select id="filter-environment">(.*?)</select>', html, re.S).group(1)
        self.assertEqual(re.findall(r'<option value="([a-z]*)"', select), ['', 'dev', 'stg', 'prd'])
        self.assertIn('<option value="stg">stg (퇴역·이력)</option>', select)

    def test_new_incident_environments_exclude_retired(self):
        js = read('app.js')
        self.assertIn("list(data.environments).filter(x=>!retired.includes(x))", js)

    def test_env_status_shows_active_and_retired_only_when_recorded(self):
        js = read('app.js')
        self.assertIn('const envs=[...ACTIVE_ENVIRONMENTS];', js)
        self.assertIn('for (const env of RETIRED_ENVIRONMENTS) if (Object.hasOwn(results,env)) envs.splice(1,0,env);', js)
        self.assertNotIn("['dev','stg','prd'].map(env=>", js)


class IncidentUxDisclosures(unittest.TestCase):
    def test_raw_disclosures_are_explicit_closed_and_lazy(self):
        html, js = read('index.html'), read('app.js')
        tags = re.findall(r'<details\b[^>]*data-raw[^>]*>\s*<summary>(.*?)</summary>', html)
        self.assertGreaterEqual(len(tags), 10)
        self.assertTrue(all('원문 보기' in title for title in tags))
        self.assertNotRegex(html, r'<details[^>]*data-raw[^>]*\bopen')
        self.assertIn('if (!$(id).open)', js)
        self.assertIn('entry.render(); entry.mounted=entry.key;', js)
        self.assertIn("resetRaw($('incident-detail'))", js)

    def test_actions_precede_evidence_and_creation_is_on_demand(self):
        html = read('index.html')
        self.assertLess(html.index('id="human-section"'), html.index('id="evidence-title"'))
        self.assertIn('<details id="create-panel">', html)
        self.assertIn('id="workspace" class="workspace" hidden', html)
        self.assertIn('id="user-test-form" autocomplete="off" hidden', html)
        self.assertIn('id="approve-form" autocomplete="off" hidden', html)

    def test_raw_fallbacks_are_not_used_in_error_or_environment_summary(self):
        js = read('app.js')
        self.assertNotIn("s.error?.message_ko||", js)
        self.assertNotIn("error.message ||", js)
        self.assertIn("label(CODE_LABELS,c,'상세 사유 확인 필요')", js)
        self.assertIn("rawJson('raw-incident','incident-raw',current)", js)


if __name__ == '__main__':
    unittest.main()
