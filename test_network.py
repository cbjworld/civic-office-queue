import os
import socket
import time
import unittest
from unittest.mock import patch

os.environ['QT_QPA_PLATFORM'] = 'offscreen'
from PyQt6.QtWidgets import QApplication
from queue_core import ALL_DEPTS
from client_network import NetworkWorker


class NetworkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def until(self, condition, timeout=4):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if condition():
                return
            time.sleep(.01)
        self.fail('Timed out waiting for local authentication exchange')

    def test_worker_keeps_gui_responsive_during_network_wait(self):
        def slow_discovery():
            time.sleep(.35)
            return set()
        updates = []
        with patch('client_network.discover_servers', slow_discovery):
            worker = NetworkWorker('11111111-1111-4111-8111-111111111111')
            worker.updated.connect(lambda states, routes: updates.append(states))
            worker.start()
            ticks = 0
            deadline = time.monotonic() + .2
            while time.monotonic() < deadline:
                self.app.processEvents()
                ticks += 1
                time.sleep(.01)
            self.assertGreater(ticks, 8)
            self.until(lambda: bool(updates))
            worker.stop()
            self.assertTrue(worker.wait(2000))


if __name__ == '__main__':
    unittest.main(verbosity=2)
