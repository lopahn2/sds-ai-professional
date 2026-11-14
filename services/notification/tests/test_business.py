from test_contract import HttpCase

class NotificationTests(HttpCase):
    def send(self, **overrides):
        data = {'recipient': '운영팀', 'channel': 'email', 'message': '발송 확인'}
        data.update(overrides)
        return self.request('/api/notifications', 'POST', data)

    def test_enqueue_and_complete_all_channels(self):
        for channel in ('email', 'sms', 'push'):
            status, response, _ = self.send(channel=channel)
            self.assertEqual(status, 202)
            self.assertEqual(response['notification']['status'], 'queued')
        self.assertEqual(self.request('/api/notifications')[1]['queue_depth'], 3)
        for _ in range(3):
            self.server.runtime.business.tick()
        result = self.request('/api/notifications')[1]
        self.assertEqual(result['queue_depth'], 0)
        self.assertEqual(result['sent'], 4)
        self.assertTrue(all(row['status'] == 'sent' for row in result['notifications']))

    def test_invalid_notification_inputs(self):
        for field, value in [('recipient', ''), ('recipient', 5), ('message', ''), ('message', '가'*2001), ('channel', 'fax'), ('channel', None)]:
            with self.subTest(field=field, value=str(value)[:30]):
                self.assertEqual(self.send(**{field: value})[0], 400)

    def test_queue_stuck_then_drain(self):
        self.chaos('queue_stuck')
        self.send()
        self.server.runtime.business.tick()
        self.assertEqual(self.request('/api/notifications')[1]['queue_depth'], 1)
        self.assertIn('notification_queue_depth 1', self.request('/metrics')[1])
        self.assertIn('"event": "queue_stuck"', self.output.getvalue())
        self.chaos('queue_stuck', False)
        self.server.runtime.business.tick()
        self.assertEqual(self.request('/api/notifications')[1]['queue_depth'], 0)

    def test_completed_history_is_bounded(self):
        business = self.server.runtime.business
        for i in range(110):
            business.enqueue({'recipient': '관측', 'channel': 'sms', 'message': str(i)})
            business.tick()
        self.assertEqual(len(business.history), 100)
        self.assertEqual(len(business.queue), 0)
