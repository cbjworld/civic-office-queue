import ipaddress
import logging
import queue
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests
from PyQt6.QtCore import QThread, pyqtSignal

from queue_core import ALL_DEPTS
from app_common import local_ips


def manual_servers(text):
    addresses = []
    local = local_ips() | {'127.0.0.1'}
    for value in text.split(','):
        if value.strip():
            address = str(ipaddress.IPv4Address(value.strip()))
            if address in local:
                address = '127.0.0.1'
            addresses.append(f'http://{address}:8000')
    return set(addresses)


def discover_servers():
    found = set()
    try:
        local = local_ips() | {'127.0.0.1'}
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            for address in ('255.255.255.255', '127.0.0.1'):
                try:
                    sock.sendto(b'FIND_SERVER_REQUEST_V2', (address, 8001))
                except OSError:
                    continue
            deadline = time.monotonic() + .7
            while time.monotonic() < deadline:
                sock.settimeout(max(.01, deadline - time.monotonic()))
                try:
                    data, address = sock.recvfrom(1024)
                    prefix, port, departments = data.decode('utf-8').split(':')
                    if prefix != 'SERVER_INFO_V2' or port != '8000' or any(d not in ALL_DEPTS for d in departments.split(',')):
                        continue
                    ip = '127.0.0.1' if address[0] in local else str(ipaddress.IPv4Address(address[0]))
                    found.add(f'http://{ip}:8000')
                except (UnicodeError, ValueError):
                    continue
                except socket.timeout:
                    break
    except OSError:
        logging.exception('자동 서버 검색 실패')
    return found


def request_state(url, path, payload):
    # Do not route intranet queue requests through system HTTP proxies.
    with requests.Session() as session:
        session.trust_env = False
        response = session.post(url + path, json=payload, timeout=(1, 2))
        if not response.ok:
            try:
                detail = response.json().get('detail')
            except ValueError:
                detail = None
            raise ValueError(detail if isinstance(detail, str) else '요청을 처리하지 못했습니다. 입력값과 호출기를 확인하세요.')
        data = response.json()
    if not isinstance(data, dict) or data.get('protocol') != 2 or not isinstance(data.get('offices'), dict):
        raise ValueError('호출기와 직원용 프로그램을 모두 같은 버전으로 업데이트하세요.')
    for dept, state in data['offices'].items():
        if dept not in ALL_DEPTS or not isinstance(state, dict):
            raise ValueError('올바르지 않은 호출기 응답입니다.')
        if state.get('status') not in ('재실', '회의', '부재', '휴가') or not isinstance(state.get('detail'), str):
            raise ValueError('올바르지 않은 상태 응답입니다.')
        if any(type(state.get(k)) is not int or state[k] < 0 for k in ('count', 'position')):
            raise ValueError('올바르지 않은 대기 순번입니다.')
        ticket = state.get('ticket')
        if ticket is not None and (not isinstance(ticket, dict) or not isinstance(ticket.get('id'), str) or ticket.get('status') not in ('waiting', 'called', 'removed', 'cancelled')):
            raise ValueError('올바르지 않은 대기표입니다.')
        if state['position'] > state['count'] or (state['position'] > 0 and (not ticket or ticket['status'] != 'waiting')):
            raise ValueError('대기표와 순번이 일치하지 않습니다.')
    return data['offices']


class NetworkWorker(QThread):
    updated = pyqtSignal(dict, dict)
    failed = pyqtSignal(str)
    completed = pyqtSignal(str)

    def __init__(self, client_id, manual=''):
        super().__init__()
        self.client_id = client_id
        self.manual = manual_servers(manual)
        self.commands = queue.Queue()
        self.stop_event = threading.Event()

    def run(self):
        urls, routes, states = set(), {}, {}
        scan_at = poll_at = 0
        with ThreadPoolExecutor(max_workers=9) as pool:
            while not self.stop_event.is_set():
                try:
                    command = self.commands.get(timeout=.1)
                except queue.Empty:
                    command = None
                if command:
                    kind, dept, payload = command
                    if kind == 'servers':
                        self.manual = payload
                        scan_at = poll_at = 0
                    else:
                        try:
                            url = routes.get(dept)
                            if not url:
                                raise ValueError('호출기에 연결되지 않았습니다. 연결 후 다시 시도하세요.')
                            response = request_state(url, f'/v2/{kind}/{dept}', {'client_id': self.client_id, **payload})
                            states.update(response)
                            self.updated.emit(dict(states), dict(routes))
                        except (requests.RequestException, ValueError) as exc:
                            logging.warning('대기 요청 실패: %s', exc)
                            self.failed.emit('요청 결과를 확인하지 못했습니다. 재연결 후 표시되는 대기 상태를 확인하세요.\n' + str(exc))
                            states.pop(dept, None)
                            routes.pop(dept, None)
                            self.updated.emit(dict(states), dict(routes))
                        finally:
                            self.completed.emit(dept)
                            poll_at = 0
                now = time.monotonic()
                if now >= scan_at:
                    urls = discover_servers() | self.manual
                    scan_at = time.monotonic() + 10
                if now >= poll_at:
                    futures = {url: pool.submit(request_state, url, '/v2/state', {'client_id': self.client_id}) for url in urls}
                    states, routes, conflicts = {}, {}, set()
                    for url, future in futures.items():
                        try:
                            for dept, state in future.result().items():
                                if dept in routes:
                                    conflicts.add(dept)
                                states[dept], routes[dept] = state, url
                        except (requests.RequestException, ValueError):
                            continue
                    for dept in conflicts:
                        states.pop(dept, None)
                        routes.pop(dept, None)
                    self.updated.emit(dict(states), dict(routes))
                    poll_at = time.monotonic() + 1

    def stop(self):
        self.stop_event.set()
