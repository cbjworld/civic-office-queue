"""Persistent queue state shared by the office UI and HTTP service."""

import sqlite3
import threading
import uuid

from organization import ALL_DEPTS

STATUSES = ("재실", "회의", "부재", "휴가")


class QueueError(ValueError):
    pass


def clean_text(value, label, limit, required=False):
    if not isinstance(value, str):
        raise QueueError(f"{label} 형식이 올바르지 않습니다.")
    value = value.strip()
    if (required and not value) or len(value) > limit or any(ord(c) < 32 for c in value):
        raise QueueError(f"{label}: {'1' if required else '0'}~{limit}자 이내로 입력하세요.")
    return value


def client_key(value):
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        raise QueueError("사용자 식별 정보가 올바르지 않습니다.") from None


class QueueStore:
    def __init__(self, path, departments):
        if not departments or any(d not in ALL_DEPTS for d in departments):
            raise QueueError("선택한 부서 정보를 확인하세요.")
        self.departments = list(dict.fromkeys(departments))
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(path), check_same_thread=False, timeout=5)
        self.db.row_factory = sqlite3.Row
        with self.db:
            self.db.executescript('''
                CREATE TABLE IF NOT EXISTS offices (
                    dept TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT '재실',
                    detail TEXT NOT NULL DEFAULT '', version INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS tickets (
                    id TEXT PRIMARY KEY, client TEXT NOT NULL, dept TEXT NOT NULL,
                    name TEXT NOT NULL, department TEXT NOT NULL, status TEXT NOT NULL,
                    rank INTEGER NOT NULL, UNIQUE(dept, client));
            ''')
            self.db.executemany("INSERT OR IGNORE INTO offices(dept) VALUES (?)",
                                [(d,) for d in self.departments])

    def _office(self, dept):
        if dept not in self.departments:
            raise QueueError("이 호출기가 담당하는 부서가 아닙니다.")
        return self.db.execute("SELECT * FROM offices WHERE dept=?", (dept,)).fetchone()

    def _waiting(self, dept):
        return [dict(r) for r in self.db.execute(
            "SELECT id,name,department,rank FROM tickets WHERE dept=? AND status='waiting' ORDER BY rank,id",
            (dept,))]

    def _bump(self, dept):
        self.db.execute("UPDATE offices SET version=version+1 WHERE dept=?", (dept,))

    def join(self, dept, client, name, department):
        client = client_key(client)
        name = clean_text(name, "이름", 50, True)
        department = clean_text(department, "소속", 80)
        with self.lock, self.db:
            office = self._office(dept)
            if office['status'] == '휴가':
                raise QueueError("휴가 중인 부서에는 대기를 등록할 수 없습니다.")
            existing = self.db.execute("SELECT * FROM tickets WHERE dept=? AND client=?", (dept, client)).fetchone()
            if not existing or existing['status'] != 'waiting':
                rank = self.db.execute("SELECT COALESCE(MAX(rank),0)+1 FROM tickets WHERE dept=? AND status='waiting'", (dept,)).fetchone()[0]
                self.db.execute('''INSERT INTO tickets VALUES (?,?,?,?,?,'waiting',?)
                    ON CONFLICT(dept,client) DO UPDATE SET id=excluded.id,name=excluded.name,
                    department=excluded.department,status='waiting',rank=excluded.rank''',
                    (str(uuid.uuid4()), client, dept, name, department, rank))
                self._bump(dept)
            return self.state(client)

    def leave(self, dept, client, ticket_id):
        client = client_key(client)
        with self.lock, self.db:
            self._office(dept)
            changed = self.db.execute("UPDATE tickets SET status='cancelled' WHERE dept=? AND client=? AND id=? AND status='waiting'",
                                      (dept, client, ticket_id)).rowcount
            if changed:
                self._bump(dept)
            return self.state(client)

    def call(self, dept):
        with self.lock, self.db:
            self._office(dept)
            waiting = self._waiting(dept)
            if not waiting:
                raise QueueError("호출할 대기자가 없습니다.")
            self.db.execute("UPDATE tickets SET status='called' WHERE id=?", (waiting[0]['id'],))
            self._bump(dept)
            return waiting[0]

    def delete(self, dept, ticket_id):
        with self.lock, self.db:
            self._office(dept)
            if not self.db.execute("UPDATE tickets SET status='removed' WHERE dept=? AND id=? AND status='waiting'", (dept, ticket_id)).rowcount:
                raise QueueError("이미 변경된 대기자입니다. 명단을 새로고침하세요.")
            self._bump(dept)

    def set_status(self, dept, status, detail=''):
        if status not in STATUSES:
            raise QueueError("올바른 재실 상태를 선택하세요.")
        detail = clean_text(detail, "복귀 예정", 100)
        with self.lock, self.db:
            self._office(dept)
            self.db.execute("UPDATE offices SET status=?,detail=?,version=version+1 WHERE dept=?", (status, detail, dept))
            if status == '휴가':
                self.db.execute("UPDATE tickets SET status='removed' WHERE dept=? AND status='waiting'", (dept,))

    def reorder(self, dept, ids, version):
        with self.lock, self.db:
            office = self._office(dept)
            current = [r['id'] for r in self._waiting(dept)]
            if office['version'] != version or len(ids) != len(current) or set(ids) != set(current):
                raise QueueError("대기 명단이 변경되었습니다. 새 명단에서 다시 정렬하세요.")
            self.db.executemany("UPDATE tickets SET rank=? WHERE id=?", enumerate(ids, 1))
            self._bump(dept)

    def snapshot(self):
        with self.lock:
            offices = {d: dict(self._office(d)) for d in self.departments}
            return {'dept_statuses': {d: r['status'] for d, r in offices.items()},
                    'dept_details': {d: r['detail'] for d, r in offices.items()},
                    'versions': {d: r['version'] for d, r in offices.items()},
                    'queues': {d: self._waiting(d) for d in offices}}

    def state(self, client):
        client = client_key(client)
        with self.lock:
            snapshot = self.snapshot()
            result = {'protocol': 2, 'offices': {}}
            for dept, queue in snapshot['queues'].items():
                own = self.db.execute("SELECT id,status FROM tickets WHERE dept=? AND client=?", (dept, client)).fetchone()
                ticket = dict(own) if own else None
                position = next((i for i, r in enumerate(queue, 1) if ticket and r['id'] == ticket['id']), 0)
                result['offices'][dept] = {'status': snapshot['dept_statuses'][dept],
                    'detail': snapshot['dept_details'][dept], 'count': len(queue),
                    'position': position, 'ticket': ticket}
            return result

    def close(self):
        with self.lock:
            self.db.close()
