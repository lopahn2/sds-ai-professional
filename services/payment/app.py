"""결제 시스템: 외부 결제망을 호출하지 않는 메모리 기반 실험 앱."""
from collections import deque
from datetime import datetime, timedelta, timezone
import os
from urllib.parse import urlencode
import re
from runtime import Runtime, ServiceServer, RequestError, text, now, serve


class SettlementError(Exception):
    """정산 배치가 결제 원장을 집계하지 못했다."""


class DuplicateChargeError(Exception):
    """결제 원장에 청구 금액이 결제 금액과 다른 건이 있다."""


class Payment:
    api_prefix = '/api/payments'

    def __init__(self, runtime):
        self.runtime = runtime
        self.payments = deque(maxlen=100)
        self.sequence = 0
        self.processed = 0
        self.charged = 0
        self.overcharge_total = 0
        self.settled_total = 0
        self.traffic_index = 0
        self.settings = self.load_settings()
        self.create_payment({'order_id': '주문-1001', 'amount': 18000}, verify_customer=False)
        self.create_payment({'order_id': '주문-1002', 'amount': 32000}, verify_customer=False)

    def load_settings(self):
        settings = {'currency': 'KRW', 'fee_percent': 3}
        if self.runtime.enabled('startup_config_error'):
            # Intentional defect: this release requires PAYMENT_FEE_PERCENT, which no environment defines.
            # Fix: keep the default fee_percent when the variable is absent.
            settings['fee_percent'] = int(os.environ['PAYMENT_FEE_PERCENT'])
        return settings

    def verify_customer(self, order):
        # Same-environment member service: the buyer directory must answer before a charge is taken.
        self.runtime.call_dependency('MEMBER_URL', '/api/members?' + urlencode({'search': order}), '회원')

    def create_payment(self, data, verify_customer=True):
        order = text(data.get('order_id'), '주문번호', 80)
        amount = data.get('amount')
        if type(amount) is not int or not 1 <= amount <= 100000000:
            raise RequestError(400, '금액은 1원 이상 1억 원 이하의 정수여야 합니다')
        if verify_customer:
            self.verify_customer(order)
        fee = self.calculate_fee(amount) if verify_customer else amount * self.settings['fee_percent'] // 100
        charged = amount
        if verify_customer and (self.runtime.enabled('duplicate_charge') or self.runtime.enabled('business_invariant')):
            charged *= 2
            self.runtime.log('WARN', 'duplicate_charge', order_id=order, amount=charged)
        with self.runtime.lock:
            self.sequence += 1
            payment = dict(id=str(self.sequence), order_id=order, amount=amount, charged_amount=charged,
                           fee=fee, status='paid', created_at=now())
            self.payments.appendleft(payment)
            self.processed += 1
            self.charged += charged
            self.overcharge_total += charged - amount
            return dict(payment)

    def calculate_fee(self, amount):
        if self.runtime.enabled('code_bug') and amount >= 10000:
            installments = 1
            # Intentional defect: a single installment must not produce a zero divisor.
            return amount // (installments - 1)
        return amount * self.settings['fee_percent'] // 100

    def find(self, payment_id):
        with self.runtime.lock:
            for payment in self.payments:
                if payment['id'] == payment_id:
                    return payment
        raise RequestError(404, '결제를 찾을 수 없습니다')

    def refund_quote(self, payment):
        if self.runtime.enabled('code_bug_validation'):
            # Intentional defect: the refund policy reads a key that payments never carry.
            # Fix: use payment['charged_amount'] for a paid payment.
            refundable = payment['refundable_amount']
        else:
            refundable = payment['charged_amount'] if payment['status'] == 'paid' else 0
        return {'payment_id': payment['id'], 'refundable_amount': refundable,
                'fee_refund': payment['fee'] if refundable else 0}

    def receipt(self, payment):
        if self.runtime.enabled('code_bug_serialization'):
            # Intentional defect: the amount is converted to text before a numeric thousands format.
            # Fix: format the integer itself ('{:,}원'.format(amount)).
            amount_text = '{:,}원'.format(str(payment['charged_amount']))
        else:
            amount_text = '{:,}원'.format(payment['charged_amount'])
        return {'payment_id': payment['id'], 'order_id': payment['order_id'], 'amount_text': amount_text,
                'status': payment['status'], 'issued_at': now()}

    def reconcile(self):
        fixed = 0
        with self.runtime.lock:
            corrected_amount = self.overcharge_total
            self.charged -= corrected_amount
            self.overcharge_total = 0
            for payment in self.payments:
                if payment['status'] == 'paid' and payment['charged_amount'] != payment['amount']:
                    payment['charged_amount'] = payment['amount']
                    fixed += 1
        self.runtime.log('INFO', 'payments_reconciled', fixed=fixed)
        return {'fixed': fixed, 'corrected_amount': corrected_amount}

    def handle(self, method, path, query, data):
        if path == self.api_prefix:
            if method == 'GET':
                with self.runtime.lock:
                    return 200, {'payments': [dict(row) for row in self.payments], 'processed': self.processed,
                                 'charged_amount': self.charged, 'settled_amount': self.settled_total}
            if method == 'POST':
                return 201, {'payment': self.create_payment(data)}
        if method == 'POST' and path == self.api_prefix + '/reconcile':
            return 200, self.reconcile()
        match = re.fullmatch(r'/api/payments/([0-9]+)/(cancel|refund|receipt)', path)
        if match and match[2] == 'cancel' and method == 'POST':
            with self.runtime.lock:
                payment = self.find(match[1])
                if payment['status'] == 'cancelled':
                    raise RequestError(409, '이미 취소된 결제입니다')
                self.refund_quote(payment)  # Validate the refund before changing the ledger.
                self.overcharge_total -= payment['charged_amount'] - payment['amount']
                payment['status'] = 'cancelled'
                self.charged -= payment['charged_amount']
                return 200, {'payment': dict(payment)}
        if match and match[2] == 'refund' and method == 'GET':
            return 200, {'refund': self.refund_quote(dict(self.find(match[1])))}
        if match and match[2] == 'receipt' and method == 'GET':
            return 200, {'receipt': self.receipt(dict(self.find(match[1])))}
        if match:
            raise RequestError(404, '결제를 찾을 수 없습니다')
        raise RequestError(404, '요청한 결제 업무를 찾을 수 없습니다')

    def template(self, path):
        match = re.fullmatch(r'/api/payments/[^/]+/(cancel|refund|receipt)', path)
        if match:
            return '/api/payments/{id}/' + match[1]
        return path if path in ('/', '/chaos', '/api/config', '/api/chaos', '/healthz/live', '/healthz/ready', '/metrics',
                                '/static/styles.css', '/static/app.js', self.api_prefix,
                                self.api_prefix + '/reconcile') else '/unknown'

    def metrics(self):
        with self.runtime.lock:
            return [('payment_processed_total', 'counter', self.processed),
                    ('payment_charged_amount', 'gauge', self.charged),
                    ('payment_settled_amount', 'gauge', self.settled_total)]

    def latest_id(self):
        with self.runtime.lock:
            return self.payments[0]['id'] if self.payments else '1'

    def probes(self):
        latest = self.latest_id()
        return {'code_bug': ('POST', self.api_prefix, {'order_id': '점검-결함', 'amount': 12000}),
                'code_bug_validation': ('GET', f'/api/payments/{latest}/refund', None),
                'code_bug_serialization': ('GET', f'/api/payments/{latest}/receipt', None),
                'dependency_failure': ('POST', self.api_prefix, {'order_id': '점검-연동', 'amount': 9000})}

    def traffic(self):
        latest = self.latest_id()
        calls = [('POST', self.api_prefix, {'order_id': '자동-관측', 'amount': 12000}),
                 ('GET', f'/api/payments/{latest}/refund', None),
                 ('GET', f'/api/payments/{latest}/receipt', None),
                 ('POST', f'/api/payments/{latest}/cancel', {}),
                 ('GET', self.api_prefix, None)]
        call = calls[self.traffic_index % len(calls)]
        self.traffic_index += 1
        return call

    def background_job(self):
        """Settlement batch: totals the paid ledger."""
        with self.runtime.lock:
            rows = [dict(p) for p in self.payments]
        if self.runtime.enabled('background_job_failure'):
            # Intentional defect: the batch averages over already-settled rows, which the ledger never has.
            # Fix: average over paid rows and skip an empty batch.
            batch = [p for p in rows if p['status'] == 'settled']
            try:
                average = sum(p['charged_amount'] for p in batch) // len(batch)
            except ZeroDivisionError as exc:
                raise SettlementError('정산 배치가 대상 결제의 평균 금액을 계산하지 못했습니다') from exc
            self.settled_total = average * len(batch)
            return
        batch = [p for p in rows if p['status'] == 'paid']
        if batch:
            self.settled_total = sum(p['charged_amount'] for p in batch)

    def check_invariants(self):
        """Ledger consistency: a paid payment must charge exactly its amount; overcharge debt survives history eviction."""
        with self.runtime.lock:
            bad = [p['id'] for p in self.payments if p['status'] == 'paid' and p['charged_amount'] != p['amount']]
            overcharge = self.overcharge_total
        if bad or overcharge:
            first = bad[0] if bad else '이력 보관 범위 밖'
            raise DuplicateChargeError(f'중복 청구 잔액 {overcharge}원, 보관 원장 위반 {len(bad)}건 (결제 {first})')

    def tick(self):
        return


def create_server(host='0.0.0.0', port=8080, background=True):
    runtime = Runtime('payment', '결제 운영', {'duplicate_charge': ('중복 결제', '청구 금액 2배와 경고 로그')})
    # Seed data are normal even when the environment enables the code defect at boot.
    with runtime.lock:
        bug = runtime.faults.pop('code_bug', None)
        runtime.business = Payment(runtime)
        if bug is not None:
            runtime.faults['code_bug'] = bug
    server = ServiceServer((host, port), runtime)
    if background:
        runtime.start(server.server_port)
    return server


if __name__ == '__main__':
    serve(create_server)
