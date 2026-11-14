"""알림 발송 시스템: 메모리 대기열과 초당 한 건 처리기."""
from collections import deque
import re
import os
from urllib.parse import urlencode
from runtime import Runtime, ServiceServer, RequestError, text, now, serve

class DispatchJobError(Exception):
    """발송 배치가 발송 계획을 만들지 못했다."""


class QueueStalledError(Exception):
    """대기 중인 알림의 발송 처리가 막혔다."""


class Notification:
    api_prefix = '/api/notifications'

    def __init__(self, runtime):
        self.runtime = runtime
        self.queue = deque()
        self.history = deque(maxlen=100)
        self.sequence = 0
        self.sent = 0
        self.traffic_index = 0
        self.settings = self.load_settings()
        self.enqueue({'recipient': '운영팀', 'channel': 'email', 'message': '알림 시스템이 준비되었습니다.'}, verify_recipient=False)
        self.tick()

    def load_settings(self):
        settings = {'sender': '운영 알림'}
        if self.runtime.enabled('startup_config_error'):
            # 정상 수정: 발신자 환경변수가 없으면 기본 발신자를 사용한다.
            settings['sender'] = os.environ['NOTIFICATION_SENDER_NAME']
        return settings

    def normalize_channel(self, channel):
        channel = text(channel, '발송 채널', 20)
        if channel not in ('email', 'sms', 'push'):
            raise RequestError(400, '지원하지 않는 발송 채널입니다')
        if self.runtime.enabled('code_bug') and channel == 'email':
            # Intentional defect: str has lower(), not lowercase().
            return channel.lowercase()
        return channel.lower()

    def enqueue(self, data, verify_recipient=True):
        recipient = text(data.get('recipient'), '수신자', 120)
        message = text(data.get('message'), '내용', 2000)
        if verify_recipient:
            self.runtime.call_dependency('MEMBER_URL', '/api/members?' + urlencode({'search': recipient}), '회원')
        channel = self.normalize_channel(data.get('channel')) if verify_recipient else data['channel']
        with self.runtime.lock:
            if len(self.queue) >= 10000:
                raise RequestError(429, '대기열이 가득 찼습니다')
            self.sequence += 1
            notification = dict(id=str(self.sequence), recipient=recipient, channel=channel, message=message,
                                status='queued', created_at=now(),
                                stalled=self.runtime.enabled('business_invariant'))
            self.queue.append(notification)
            return dict(notification)

    def retry_notification(self, notification_id):
        with self.runtime.lock:
            original = next((dict(row) for row in list(self.queue) + list(self.history)
                             if row['id'] == notification_id), None)
        if original is None:
            raise RequestError(404, '알림을 찾을 수 없습니다')
        if self.runtime.enabled('code_bug_validation'):
            target = None
            # 정상 수정: 조회한 original에서 수신자를 읽는다.
            original['recipient'] = target['recipient']
        return self.enqueue(original)

    def list_notifications(self):
        with self.runtime.lock:
            rows = [dict(row) for row in list(self.queue)[-100:][::-1]] + [dict(row) for row in self.history]
            if self.runtime.enabled('code_bug_serialization'):
                # 정상 수정: 실제 원장 키인 recipient를 템플릿에서 사용한다.
                for row in rows:
                    row['summary'] = '{recipient_name}: {message}'.format(**row)
            return {'notifications': rows, 'queue_depth': len(self.queue), 'sent': self.sent}

    def reconcile(self):
        with self.runtime.lock:
            fixed = sum(bool(row.get('stalled')) for row in self.queue)
            for row in self.queue:
                row['stalled'] = False
        return {'fixed': fixed}

    def handle(self, method, path, query, data):
        if method == 'POST' and path == self.api_prefix + '/reconcile':
            return 200, self.reconcile()
        match = re.fullmatch(r'/api/notifications/([0-9]+)/retry', path)
        if method == 'POST' and match:
            return 202, {'notification': self.retry_notification(match[1])}
        if path == self.api_prefix:
            if method == 'POST':
                return 202, {'notification': self.enqueue(data)}
            if method == 'GET':
                return 200, self.list_notifications()
        raise RequestError(404, '요청한 알림 업무를 찾을 수 없습니다')

    def template(self, path):
        if path == self.api_prefix + '/reconcile':
            return path
        if re.fullmatch(r'/api/notifications/[^/]+/retry', path):
            return '/api/notifications/{id}/retry'
        return path if path in ('/', '/chaos', '/api/config', '/api/chaos', '/healthz/live', '/healthz/ready', '/metrics', '/static/styles.css', '/static/app.js', self.api_prefix) else '/unknown'

    def tick(self):
        with self.runtime.lock:
            if self.runtime.enabled('business_invariant'):
                for row in self.queue:
                    row['stalled'] = True
            blocked = self.runtime.enabled('queue_stuck') or any(row.get('stalled') for row in self.queue)
            if self.queue and not blocked:
                row = self.queue.popleft()
                row.update(status='sent', sent_at=now())
                self.history.appendleft(row)
                self.sent += 1
            elif self.queue and blocked:
                self.runtime.log('WARN', 'queue_stuck', queue_depth=len(self.queue))

    def background_job(self):
        with self.runtime.lock:
            rows = [dict(row) for row in self.queue] or [dict(self.history[0])]
        try:
            # 정상 수정: 발송 채널이 담긴 channel 키를 사용한다.
            key = 'dispatch_channel' if self.runtime.enabled('background_job_failure') else 'channel'
            self.dispatch_plan = [row[key].lower() for row in rows]
        except KeyError as exc:
            raise DispatchJobError('알림 발송 배치가 채널 계획을 만들지 못했습니다') from exc

    def check_invariants(self):
        with self.runtime.lock:
            if self.queue and (self.runtime.enabled('queue_stuck') or self.runtime.enabled('business_invariant')
                               or any(row.get('stalled') for row in self.queue)):
                raise QueueStalledError(f'알림 {len(self.queue)}건의 발송 대기열이 정체되었습니다')

    def metrics(self):
        with self.runtime.lock:
            return [('notification_queue_depth', 'gauge', len(self.queue)), ('notification_sent_total', 'counter', self.sent)]

    def probes(self):
        with self.runtime.lock:
            rows = list(self.queue) + list(self.history)
            latest = rows[0]['id'] if rows else '1'
        return {'code_bug': ('POST', self.api_prefix, {'recipient': '관측 담당자', 'channel': 'email', 'message': '자체 트래픽 상태 확인'}),
                'code_bug_validation': ('POST', f'{self.api_prefix}/{latest}/retry', {}),
                'code_bug_serialization': ('GET', self.api_prefix, None),
                'dependency_failure': ('POST', self.api_prefix, {'recipient': '관측 담당자', 'channel': 'sms', 'message': '회원 연동 확인'})}

    def traffic(self):
        calls = list(self.probes().values())[:3]
        with self.runtime.lock:
            call = calls[self.traffic_index % len(calls)]
            self.traffic_index += 1
        return call

def create_server(host='0.0.0.0', port=8080, background=True):
    runtime = Runtime('notification', '알림 발송', {'queue_stuck': ('대기열 정체', '대기열 길이 증가와 경고 로그')})
    with runtime.lock:
        bug = runtime.faults.pop('code_bug', None)
        runtime.business = Notification(runtime)
        if bug is not None:
            runtime.faults['code_bug'] = bug
    server = ServiceServer((host, port), runtime)
    if background:
        runtime.start(server.server_port)
    return server

if __name__ == '__main__':
    serve(create_server)
