import sys
import logging
import socket
import sqlite3
import threading
import uuid
from pathlib import Path
import uvicorn
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QGridLayout, QFrame, QCheckBox, QInputDialog,
    QSystemTrayIcon, QMenu, QStyle, QDialog, QListWidget, QListWidgetItem,
    QMessageBox, QAbstractItemView,
)
from PyQt6.QtCore import QTimer, Qt, QLockFile, pyqtSignal, QSettings
from app_common import data_dir
from organization import ORGANIZATION
from queue_core import QueueStore, QueueError, ALL_DEPTS
from queue_api import HTTP_PORT, create_api, run_discovery

store = None
MANAGED_DEPTS = []

class DraggableListWidget(QListWidget):
    def __init__(self, on_changed, parent=None):
        super().__init__(parent)
        self.on_changed = on_changed
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)

    def dropEvent(self, event):
        super().dropEvent(event)
        if self.on_changed:
            self.on_changed()

class QueueListDialog(QDialog):
    def __init__(self, dept, parent=None):
        super().__init__(parent)
        self.dept = dept
        self.setWindowTitle(f"[{dept}] 대기자 명단 · 드래그하여 순서 변경")
        self.resize(460, 400)
        
        layout = QVBoxLayout(self)
        
        # 순서 변경 시 자동 저장되도록 DraggableListWidget 사용
        self.list_widget = DraggableListWidget(on_changed=self.auto_save_queue_order)
        layout.addWidget(self.list_widget)
        
        btn_layout = QHBoxLayout()
        btn_refresh = QPushButton("새로고침")
        btn_delete = QPushButton("선택 대기 삭제")
        btn_close = QPushButton("닫기")
        
        btn_refresh.clicked.connect(self.load_queue)
        btn_delete.clicked.connect(self.delete_selected)
        btn_close.clicked.connect(self.close)
        
        btn_layout.addWidget(btn_refresh)
        btn_layout.addWidget(btn_delete)
        btn_layout.addWidget(btn_close)
        layout.addLayout(btn_layout)
        
        self.load_queue()

    def load_queue(self):
        state = store.snapshot()
        self.version = state['versions'][self.dept]
        self.list_widget.clear()
        for entry in state['queues'][self.dept]:
            label = entry['name'] + (f" ({entry['department']})" if entry['department'] else '')
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, entry['id'])
            self.list_widget.addItem(item)

    def auto_save_queue_order(self):
        ids = [self.list_widget.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.list_widget.count())]
        try:
            store.reorder(self.dept, ids, self.version)
        except (QueueError, sqlite3.Error) as exc:
            QMessageBox.warning(self, '순서 변경 실패', str(exc))
        self.load_queue()

    def delete_selected(self):
        items = self.list_widget.selectedItems()
        if not items:
            return
        if QMessageBox.question(self, '대기 삭제', f'{len(items)}명의 대기를 삭제할까요?') != QMessageBox.StandardButton.Yes:
            return
        try:
            for item in items:
                store.delete(self.dept, item.data(Qt.ItemDataRole.UserRole))
        except (QueueError, sqlite3.Error) as exc:
            QMessageBox.warning(self, '삭제 실패', str(exc))
        self.load_queue()

class AdminWidget(QWidget):
    def __init__(self, main_window):
        super().__init__()
        layout = QVBoxLayout(self)
        
        self.chk_ontop = QCheckBox("항상 위에 고정")
        self.chk_ontop.stateChanged.connect(main_window.toggle_always_on_top)
        layout.addWidget(self.chk_ontop)
        
        self.grid = QGridLayout()
        layout.addLayout(self.grid)
        self.dept_widgets = {}

        for i, dept in enumerate(MANAGED_DEPTS):
            frame = QFrame()
            frame.setStyleSheet("background: #e2e8f0; border-radius: 20px; padding: 10px;")
            v = QVBoxLayout(frame)
            
            name_lbl = QLabel(dept)
            name_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            name_lbl.setStyleSheet("font-size: 15px; font-weight: bold; color: #1a202c;")
            
            self.count_lbl = QLabel("0명 대기 중")
            self.count_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.count_lbl.setStyleSheet("font-size: 13px; font-weight: 600; color: #2d3748;")
            
            h_ctrl = QHBoxLayout()
            btn_call = QPushButton("호출")
            btn_add = QPushButton("대기 추가")
            btn_list = QPushButton("명단")
            
            btn_call.setStyleSheet("background: #2b6cb0; color: white; border-radius: 5px; padding: 5px; font-weight: bold;")
            btn_add.setStyleSheet("background: #4c51bf; color: white; border-radius: 5px; padding: 5px; font-weight: bold;")
            btn_list.setStyleSheet("background: #718096; color: white; border-radius: 5px; padding: 5px; font-weight: bold;")
            
            btn_call.clicked.connect(lambda _, d=dept: self.perform(lambda: store.call(d)))
            btn_add.clicked.connect(lambda _, d=dept: self.perform(lambda: store.join(d, str(uuid.uuid4()), "현장 대기", "")))
            btn_list.clicked.connect(lambda _, d=dept: self.show_queue_list(d))
            
            h_ctrl.addWidget(btn_call)
            h_ctrl.addWidget(btn_add)
            h_ctrl.addWidget(btn_list)

            h_state = QHBoxLayout()
            for s, color in [("재실", "#38a169"), ("회의", "#d69e2e"), ("부재", "#718096"), ("휴가", "#e53e3e")]:
                btn = QPushButton(s)
                btn.setStyleSheet(f"background: {color}; color: white; border-radius: 5px; padding: 6px; font-weight: bold;")
                if s == "부재": 
                    btn.clicked.connect(lambda _, d=dept: self.ask_time(d))
                else: 
                    btn.clicked.connect(lambda _, d=dept, stat=s: self.change_status(d, stat))
                h_state.addWidget(btn)

            v.addWidget(name_lbl)
            v.addWidget(self.count_lbl)
            v.addLayout(h_ctrl)
            v.addLayout(h_state)
            
            self.grid.addWidget(frame, i // 3, i % 3)
            self.dept_widgets[dept] = {"count": self.count_lbl, "frame": frame}
            
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.fetch_data)
        self.timer.start(1000)
        self.fetch_data()

    def perform(self, action):
        try:
            result = action()
            if isinstance(result, dict) and 'name' in result:
                self.window().statusBar().showMessage(f"호출: {result['name']} {result['department']}", 8000)
        except (QueueError, sqlite3.Error) as exc:
            QMessageBox.warning(self, '처리 실패', str(exc))

    def change_status(self, dept, status):
        if status == '휴가' and store.snapshot()['queues'][dept]:
            if QMessageBox.question(self, '휴가 설정', '대기자를 모두 취소하고 휴가로 변경할까요?') != QMessageBox.StandardButton.Yes:
                return
        self.perform(lambda: store.set_status(dept, status))

    def show_queue_list(self, dept):
        dlg = QueueListDialog(dept, self)
        dlg.exec()

    def ask_time(self, dept):
        t, ok = QInputDialog.getText(self, "부재 설정", "복귀 예정 시간을 입력하세요 (예: 14:30):")
        if ok: 
            val = t.strip()
            if val:
                if "복귀 예정" not in val:
                    detail_text = f"{val} 복귀 예정"
                else:
                    detail_text = val.replace("~", "").strip()
            else:
                detail_text = "시간 미정"
            self.perform(lambda: store.set_status(dept, "부재", detail_text))

    def fetch_data(self):
        try:
            res = store.snapshot()
            queue_lengths = {d: len(q) for d, q in res["queues"].items()}
            statuses = res.get("dept_statuses", {})
            details = res.get("dept_details", {})
            
            for d, w in self.dept_widgets.items():
                cnt = queue_lengths.get(d, 0)
                stat = statuses.get(d, "재실")
                detail = details.get(d, "")
                
                if stat == "부재" and detail and detail != "없음":
                    w["count"].setText(f"{cnt}명 대기 중 ({stat}: {detail})")
                else:
                    w["count"].setText(f"{cnt}명 대기 중 ({stat})")
                
                colors = {
                    "재실": "#c6f6d5", 
                    "회의": "#fef3c7", 
                    "부재": "#e2e8f0", 
                    "휴가": "#fed7d7"
                }
                w["frame"].setStyleSheet(f"background: {colors.get(stat, '#e2e8f0')}; border-radius: 20px; padding: 10px;")
        except sqlite3.Error:
            logging.exception("명단 조회 실패")

class MainWindow(QMainWindow):
    focus_requested = pyqtSignal()
    def __init__(self):
        super().__init__()
        self.focus_requested.connect(self.bring_to_front)

        self.setWindowTitle(f"{ORGANIZATION} · 호출기 · " + ", ".join(MANAGED_DEPTS))
        self.setMinimumWidth(360)
        self.setWindowFlags(Qt.WindowType.WindowCloseButtonHint)
        self.setCentralWidget(AdminWidget(self))

        self.tray = QSystemTrayIcon(self)
        self.tray.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon))
        
        tray_menu = QMenu(self)
        open_act = tray_menu.addAction("열기")
        open_act.triggered.connect(self.showNormal)
        quit_act = tray_menu.addAction("종료")
        quit_act.triggered.connect(QApplication.quit)
        
        self.tray.setContextMenu(tray_menu)
        self.tray.activated.connect(lambda reason: self.bring_to_front() if reason == QSystemTrayIcon.ActivationReason.DoubleClick else None)
        self.tray.show()
        self.statusBar().showMessage('대기 자동 저장 · 닫으면 알림 영역에서 계속 실행')

    def bring_to_front(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, event):
        if QSystemTrayIcon.isSystemTrayAvailable():
            event.ignore()
            self.hide()
        else:
            event.accept()

    def toggle_always_on_top(self, state):
        flags = self.windowFlags()
        if state == 2: 
            self.setWindowFlags(flags | Qt.WindowType.WindowStaysOnTopHint)
        else: 
            self.setWindowFlags(flags & ~Qt.WindowType.WindowStaysOnTopHint)
        self.show()


def select_departments(executable):
    stem = Path(executable).stem
    return [dept for dept in ALL_DEPTS if stem == dept + '호출기']


def main():
    global store, MANAGED_DEPTS
    app = QApplication(sys.argv)
    root = data_dir()
    logging.basicConfig(filename=root / 'office.log', level=logging.WARNING, encoding='utf-8')
    executable = sys.executable if getattr(sys, 'frozen', False) else __file__
    MANAGED_DEPTS = select_departments(executable)
    settings = QSettings(str(root / 'office.ini'), QSettings.Format.IniFormat)
    if not MANAGED_DEPTS:
        department = settings.value('department', '')
        if department not in ALL_DEPTS:
            department, ok = QInputDialog.getItem(None, '간부실 선택', '이 PC에서 운영할 간부실을 선택하세요.', ALL_DEPTS, 0, False)
            if not ok:
                return 0
        MANAGED_DEPTS = [department]
    settings.setValue('department', MANAGED_DEPTS[0])
    settings.sync()
    lock = QLockFile(str(root / 'office.lock'))
    if not lock.tryLock(0):
        try:
            import requests
            requests.get(f'http://127.0.0.1:{HTTP_PORT}/focus_window', timeout=1)
        except requests.RequestException:
            QMessageBox.information(None, '실행 중', '호출기가 이미 실행 중입니다. 알림 영역에서 열어주세요.')
        return 0
    sockets = []
    server = thread = discovery = None
    stop = threading.Event()
    try:
        for kind, port in [(socket.SOCK_STREAM, HTTP_PORT), (socket.SOCK_DGRAM, 8001)]:
            sock = socket.socket(socket.AF_INET, kind)
            sockets.append(sock)
            sock.bind(('0.0.0.0', port))
        sockets[0].listen(128)
        store = QueueStore(root / 'queue.sqlite3', MANAGED_DEPTS)
        window = MainWindow()
        server = uvicorn.Server(uvicorn.Config(create_api(store, window.focus_requested.emit), log_config=None, log_level='warning', access_log=False))
        thread = threading.Thread(target=lambda: server.run(sockets=[sockets[0]]), daemon=True)
        thread.start()
        discovery = threading.Thread(target=run_discovery, args=(MANAGED_DEPTS, stop, sockets[1]), daemon=True)
        discovery.start()
        window.show()
        return app.exec()
    except Exception as exc:
        logging.exception('호출기 시작 실패 (%s)', ', '.join(MANAGED_DEPTS))
        QMessageBox.critical(None, '호출기 실행 실패', str(exc) + '\n\n기존 호출기 실행 여부와 8000·8001 포트 사용 상태를 확인하세요.')
        return 1
    finally:
        stop.set()
        if server:
            server.should_exit = True
        if thread:
            thread.join(5)
        if discovery:
            discovery.join(2)
        for sock in sockets:
            sock.close()
        if store and (not thread or not thread.is_alive()):
            store.close()
        lock.unlock()


if __name__ == '__main__':
    sys.exit(main())
