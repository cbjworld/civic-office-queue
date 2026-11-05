import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid
from concurrent.futures import ThreadPoolExecutor

os.environ['QT_QPA_PLATFORM'] = 'offscreen'

import requests
import uvicorn
from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QApplication

from queue_core import QueueStore, QueueError, ALL_DEPTS
from queue_api import create_api
from client_network import request_state, manual_servers
from client_app import ClientApp
import server_office

DEPT = ALL_DEPTS[0]


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'queue.db'
        self.store = QueueStore(self.path, [DEPT])
        self.a, self.b = str(uuid.uuid4()), str(uuid.uuid4())

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def join(self, client):
        return self.store.join(DEPT, client, '홍길동', '기획예산과')['offices'][DEPT]

    def own(self, client):
        return self.store.state(client)['offices'][DEPT]

    def test_same_name_and_retry(self):
        first = self.join(self.a)
        self.join(self.b)
        self.assertEqual(self.join(self.a)['ticket'], first['ticket'])
        self.assertEqual(self.own(self.b)['position'], 2)
        self.assertEqual(self.own(self.a)['count'], 2)
        self.store.call(DEPT)
        self.assertEqual(self.own(self.a)['ticket']['status'], 'called')
        self.assertEqual(self.own(self.b)['position'], 1)

    def test_cancel_delete_vacation_are_not_calls(self):
        ticket = self.join(self.a)['ticket']['id']
        self.store.leave(DEPT, self.b, ticket)
        self.assertEqual(self.own(self.a)['position'], 1)
        self.store.leave(DEPT, self.a, ticket)
        self.assertEqual(self.own(self.a)['ticket']['status'], 'cancelled')
        ticket = self.join(self.a)['ticket']['id']
        self.store.delete(DEPT, ticket)
        self.assertEqual(self.own(self.a)['ticket']['status'], 'removed')
        self.join(self.a)
        self.store.set_status(DEPT, '휴가')
        self.assertEqual(self.own(self.a)['ticket']['status'], 'removed')
        with self.assertRaises(QueueError):
            self.join(self.b)

    def test_stale_reorder_preserves_new_join(self):
        self.join(self.a)
        snapshot = self.store.snapshot()
        ids = [r['id'] for r in snapshot['queues'][DEPT]]
        self.join(self.b)
        with self.assertRaises(QueueError):
            self.store.reorder(DEPT, ids, snapshot['versions'][DEPT])
        self.assertEqual(self.own(self.b)['position'], 2)
        fresh = self.store.snapshot()
        ids = [r['id'] for r in fresh['queues'][DEPT]][::-1]
        self.store.reorder(DEPT, ids, fresh['versions'][DEPT])
        self.assertEqual(self.own(self.b)['position'], 1)
        with self.assertRaises(QueueError):
            self.store.reorder(DEPT, [ids[0], ids[0]], self.store.snapshot()['versions'][DEPT])

    def test_old_cancel_does_not_remove_new_ticket(self):
        old = self.join(self.a)['ticket']['id']
        self.store.call(DEPT)
        new = self.join(self.a)['ticket']['id']
        self.store.leave(DEPT, self.a, old)
        self.assertNotEqual(old, new)
        self.assertEqual(self.own(self.a)['position'], 1)

    def test_persistence_and_multiple_calls(self):
        self.join(self.a)
        self.join(self.b)
        self.store.call(DEPT)
        self.store.call(DEPT)
        self.store.set_status(DEPT, '부재', '14:30 / 회의실')
        self.store.close()
        self.store = QueueStore(self.path, [DEPT])
        for client in (self.a, self.b):
            self.assertEqual(self.own(client)['ticket']['status'], 'called')
        self.assertEqual(self.own(self.a)['detail'], '14:30 / 회의실')

    def test_concurrent_registration(self):
        clients = [str(uuid.uuid4()) for _ in range(30)]
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(self.join, clients))
        self.assertEqual({self.own(c)['position'] for c in clients}, set(range(1, 31)))

    def test_validation_and_privacy(self):
        for name in ('', '  ', 'x' * 51, 'name\nnext'):
            with self.assertRaises(QueueError):
                self.store.join(DEPT, self.a, name, '')
        with self.assertRaises(QueueError):
            self.store.join('unknown', self.a, 'name', '')
        self.join(self.a)
        public = str(self.store.state(self.b))
        self.assertNotIn('홍길동', public)
        self.assertNotIn(self.a, public)


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store = QueueStore(':memory:', [DEPT])
        cls.sock = socket.socket()
        cls.sock.bind(('127.0.0.1', 0))
        cls.sock.listen(16)
        cls.url = f'http://127.0.0.1:{cls.sock.getsockname()[1]}'
        cls.server = uvicorn.Server(uvicorn.Config(create_api(cls.store), log_config=None, log_level='error', access_log=False))
        cls.thread = threading.Thread(target=lambda: cls.server.run(sockets=[cls.sock]), daemon=True)
        cls.thread.start()
        deadline = time.monotonic() + 5
        while not cls.server.started and time.monotonic() < deadline:
            time.sleep(.02)
        assert cls.server.started

    @classmethod
    def tearDownClass(cls):
        cls.server.should_exit = True
        cls.thread.join(5)
        cls.sock.close()
        cls.store.close()

    def test_real_http_join_call_cancel(self):
        client = str(uuid.uuid4())
        state = request_state(self.url, '/v2/join/' + DEPT, {'client_id': client, 'name': '테스트', 'department': ''})
        self.assertEqual(state[DEPT]['position'], 1)
        self.store.call(DEPT)
        state = request_state(self.url, '/v2/state', {'client_id': client})
        self.assertEqual(state[DEPT]['ticket']['status'], 'called')
        state = request_state(self.url, '/v2/join/' + DEPT, {'client_id': client, 'name': '테스트', 'department': ''})
        state = request_state(self.url, '/v2/leave/' + DEPT, {'client_id': client, 'ticket_id': state[DEPT]['ticket']['id']})
        self.assertEqual(state[DEPT]['ticket']['status'], 'cancelled')

    def test_remote_admin_endpoints_removed(self):
        for path in ('/shutdown', '/call/' + DEPT, '/set_queue/' + DEPT, '/queue_add/' + DEPT, '/state'):
            self.assertEqual(requests.get(self.url + path, timeout=2).status_code, 404)
        self.assertEqual(requests.post(self.url + '/v2/state', json={'client_id': 'bad'}, timeout=2).status_code, 422)
        response = requests.post(self.url + '/v2/join/' + DEPT, json={'client_id': str(uuid.uuid4()), 'name': ' '}, timeout=2)
        self.assertEqual(response.status_code, 409)


class DepartmentTests(unittest.TestCase):
    def test_all_named_offices_without_license(self):
        with patch('pathlib.Path.read_text', side_effect=AssertionError('No license or configuration read expected')):
            for dept in ALL_DEPTS:
                with self.subTest(department=dept):
                    self.assertEqual(server_office.select_departments(dept + '호출기.exe'), [dept])

    def test_generic_or_renamed_office_requires_department_selection(self):
        self.assertEqual(server_office.select_departments('간부방호출기.exe'), [])
        self.assertEqual(server_office.select_departments('새이름.exe'), [])


class UiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        settings = QSettings(str(Path(self.temp.name) / 'settings.ini'), QSettings.Format.IniFormat)
        self.window = ClientApp(settings=settings, start_network=False)
        self.store = QueueStore(':memory:', [DEPT])

    def tearDown(self):
        for dialog in list(self.window.alerts.values()):
            dialog.accept()
        self.window.stop()
        self.window.tray_icon.hide()
        self.window.deleteLater()
        self.app.processEvents()
        self.store.close()
        self.temp.cleanup()

    def refresh(self):
        self.window.apply_state(self.store.state(self.window.client_id)['offices'], {DEPT: 'local'})

    def test_fast_call_alert_only_once_and_restart_ack(self):
        self.store.join(DEPT, self.window.client_id, '테스트', '')
        self.store.call(DEPT)
        self.refresh()
        self.refresh()
        self.assertEqual(len(self.window.alerts), 1)
        list(self.window.alerts.values())[0].accept()
        self.refresh()
        self.assertEqual(len(self.window.alerts), 0)
        reopened = ClientApp(settings=self.window.settings, start_network=False)
        reopened.apply_state(self.store.state(reopened.client_id)['offices'], {})
        self.assertEqual(len(reopened.alerts), 0)
        reopened.stop()
        reopened.tray_icon.hide()
        reopened.deleteLater()

    def test_deletion_vacation_and_offline(self):
        self.store.join(DEPT, self.window.client_id, '테스트', '')
        self.refresh()
        self.store.set_status(DEPT, '휴가')
        self.refresh()
        self.assertEqual(len(self.window.alerts), 0)
        self.assertFalse(self.window.client_buttons[DEPT].isEnabled())
        self.window.apply_state({}, {})
        self.assertIn('연결 안 됨', self.window.client_buttons[DEPT].text())

    def test_office_ui_and_queue_ids(self):
        server_office.store = self.store
        server_office.MANAGED_DEPTS = [DEPT]
        self.store.join(DEPT, self.window.client_id, '동명이인', '')
        self.store.join(DEPT, str(uuid.uuid4()), '동명이인', '')
        office = server_office.MainWindow()
        dialog = server_office.QueueListDialog(DEPT, office)
        self.assertEqual(dialog.list_widget.count(), 2)
        office.tray.hide()
        dialog.deleteLater()
        office.deleteLater()

    def test_manual_server_validation(self):
        self.assertEqual(manual_servers('127.0.0.1'), {'http://127.0.0.1:8000'})
        with self.assertRaises(ValueError):
            manual_servers('https://example.com')


if __name__ == '__main__':
    unittest.main(verbosity=2)
