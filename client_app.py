import logging
import sys
import uuid
from PyQt6.QtCore import QSettings, QLockFile, Qt
from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QGridLayout,
    QSystemTrayIcon, QStyle, QDialog, QCheckBox, QLabel, QInputDialog, QMessageBox, QMenu,
)
from app_common import data_dir
from organization import ORGANIZATION
from queue_core import ALL_DEPTS, clean_text, QueueError
from client_network import NetworkWorker, manual_servers

class CallAlertDialog(QDialog):
    def __init__(self, dept_name, parent=None):
        super().__init__(parent)
        self.setWindowTitle("🚨 입장 호출 알림")
        self.resize(350, 180)
        self.setWindowFlags(Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Dialog)
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        
        msg_label = QLabel(f"[{dept_name}]\n순번이 되었습니다!\n지금 바로 입장해 주세요.")
        msg_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        msg_label.setStyleSheet("font-size: 16px; font-weight: bold; color: #e53e3e; line-height: 1.4;")
        layout.addWidget(msg_label)
        
        ok_btn = QPushButton("확인 (닫기)")
        ok_btn.setStyleSheet("background: #3182ce; color: white; font-size: 14px; font-weight: bold; border-radius: 5px; padding: 8px;")
        ok_btn.clicked.connect(self.accept)
        layout.addWidget(ok_btn)


class ClientApp(QWidget):
    def __init__(self, settings=None, start_network=True):
        super().__init__()
        self.settings = settings or QSettings(str(data_dir() / 'employee.ini'), QSettings.Format.IniFormat)
        saved_id = self.settings.value('client_id', '')
        try:
            self.client_id = str(uuid.UUID(saved_id)) if saved_id else str(uuid.uuid4())
        except (ValueError, TypeError, AttributeError):
            self.client_id = str(uuid.uuid4())
        self.settings.setValue('client_id', self.client_id)
        self.settings.sync()
        self.states = {}
        self.pending = set()
        self.alerts = {}
        self.client_buttons = {}
        self.client_positions = {d: 0 for d in ALL_DEPTS}
        self.setWindowTitle(f'{ORGANIZATION} · 직원 번호표')
        self.setMinimumSize(490, 360)
        layout = QVBoxLayout(self)
        self.connection_label = QLabel('호출기를 찾고 있습니다…')
        layout.addWidget(self.connection_label)
        grid = QGridLayout()
        for i, dept in enumerate(ALL_DEPTS):
            button = QPushButton(f'{dept}\n연결 확인 중')
            button.setMinimumHeight(88)
            button.setEnabled(False)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _, d=dept: self.toggle_queue(d))
            grid.addWidget(button, i // 3, i % 3)
            self.client_buttons[dept] = button
        layout.addLayout(grid)
        bottom = QHBoxLayout()
        servers = QPushButton('서버 연결 설정')
        servers.clicked.connect(self.configure_servers)
        bottom.addWidget(servers)
        bottom.addStretch()
        on_top = QCheckBox('항상 위에')
        on_top.toggled.connect(self.toggle_always_on_top)
        bottom.addWidget(on_top)
        layout.addLayout(bottom)
        self.tray_icon = QSystemTrayIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MessageBoxInformation), self)
        menu = QMenu(self)
        menu.addAction('열기', self.bring_to_front)
        menu.addAction('종료', QApplication.quit)
        self.tray_icon.setContextMenu(menu)
        self.tray_icon.activated.connect(lambda reason: self.bring_to_front() if reason == QSystemTrayIcon.ActivationReason.DoubleClick else None)
        self.tray_icon.show()
        try:
            self.worker = NetworkWorker(self.client_id, self.settings.value('servers', ''))
        except (ValueError, OSError):
            self.settings.setValue('servers', '')
            self.worker = NetworkWorker(self.client_id)
        self.worker.updated.connect(self.apply_state)
        self.worker.failed.connect(self.show_error)
        self.worker.completed.connect(self.operation_completed)
        if start_network:
            self.worker.start()

    def show_error(self, message):
        QMessageBox.warning(self, '연결 또는 처리 오류', message)

    def operation_completed(self, dept):
        self.pending.discard(dept)
        state = self.states.get(dept)
        self.client_buttons[dept].setEnabled(bool(state and state['status'] != '휴가'))

    def toggle_always_on_top(self, enabled):
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, enabled)
        self.show()

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

    def configure_servers(self):
        text, ok = QInputDialog.getText(self, '서버 연결 설정', '자동 검색이 안 되는 호출기 PC의 IPv4 주소를 입력하세요.\n여러 주소는 쉼표로 구분합니다. 비워두면 자동 검색만 사용합니다.', text=self.settings.value('servers', ''))
        if not ok:
            return
        try:
            urls = manual_servers(text)
        except ValueError:
            self.show_error('올바른 IPv4 주소를 입력하세요. 예: 192.168.0.10')
            return
        self.settings.setValue('servers', text)
        self.worker.commands.put(('servers', '', urls))

    def toggle_queue(self, dept):
        state = self.states.get(dept)
        if not state or dept in self.pending:
            return
        if state['position']:
            if QMessageBox.question(self, '대기 취소', f'[{dept}] 대기를 취소할까요?') != QMessageBox.StandardButton.Yes:
                return
            command, payload = 'leave', {'ticket_id': state['ticket']['id']}
        else:
            text, ok = QInputDialog.getText(self, '대기 등록', '이름과 소속을 쉼표로 구분하세요. (예: 홍길동,기획예산과)', text=self.settings.value('last_entry', ''))
            if not ok:
                return
            parts = text.split(',', 1)
            try:
                name = clean_text(parts[0], '이름', 50, True)
                department = clean_text(parts[1] if len(parts) > 1 else '', '소속', 80)
            except QueueError as exc:
                self.show_error(str(exc))
                return
            self.settings.setValue('last_entry', text)
            command, payload = 'join', {'name': name, 'department': department}
        self.pending.add(dept)
        self.client_buttons[dept].setEnabled(False)
        self.worker.commands.put((command, dept, payload))

    def apply_state(self, states, routes):
        self.states = states
        self.connection_label.setText(f'{len(states)}개 간부실 연결됨 · 창을 닫아도 호출 알림을 받습니다.' if states else '연결된 호출기가 없습니다. 실행 상태와 서버 연결 설정을 확인하세요.')
        for dept, button in self.client_buttons.items():
            state = states.get(dept)
            if state is None:
                button.setEnabled(False)
                button.setText(f'{dept}\n서버 연결 안 됨')
                button.setStyleSheet('background:#e2e8f0; color:#64748b; border-radius:5px; padding:8px;')
                continue
            pos, previous = state['position'], self.client_positions[dept]
            self.client_positions[dept] = pos
            ticket = state['ticket']
            if 0 < pos < previous:
                self.tray_icon.showMessage('대기 알림', f'[{dept}] 내 앞에 {pos - 1}명 남았습니다.')
            if ticket and ticket['status'] == 'called':
                key = 'seen/' + dept
                if self.settings.value(key) != ticket['id'] and ticket['id'] not in self.alerts:
                    dialog = CallAlertDialog(dept, self)
                    ticket_id = ticket['id']
                    self.alerts[ticket_id] = dialog
                    dialog.finished.connect(lambda _, k=key, t=ticket_id: self.acknowledge(k, t))
                    QApplication.alert(self, 0)
                    self.tray_icon.showMessage('입장 호출', f'[{dept}] 지금 입장해 주세요.')
                    dialog.open()
            elif ticket and ticket['status'] == 'removed' and self.settings.value('seen/' + dept) != ticket['id']:
                self.settings.setValue('seen/' + dept, ticket['id'])
                self.tray_icon.showMessage('대기 종료', f'[{dept}] 관리자에 의해 대기가 취소되었습니다.')
            self.update_button_ui(dept, state['status'], state['detail'], state['count'], pos)

    def acknowledge(self, key, ticket_id):
        self.settings.setValue(key, ticket_id)
        self.settings.sync()
        dialog = self.alerts.pop(ticket_id, None)
        if dialog:
            dialog.deleteLater()

    def stop(self):
        self.worker.stop()
        self.worker.wait(10000)
        self.settings.sync()

    def update_button_ui(self, dept, stat, det, q_len, pos):
        btn = self.client_buttons[dept]
        
        if stat == "휴가":
            btn.setEnabled(False)
            btn.setText(f"{dept}\n휴가 중")
            btn.setStyleSheet("""
                QPushButton {
                    background-color: #fed7d7;
                    color: #e53e3e;
                    border: 1px solid #feb2b2;
                    border-radius: 5px;
                    font-weight: bold;
                    font-size: 12px;
                }
            """)
            return

        btn.setEnabled(dept not in self.pending)
# 2. 상태(회의/부재)와 관계없이 대기 인원 표시
        status_text = ""
        if pos > 0:
            ahead = pos - 1
            status_text = f"대기 중 (내 앞 {ahead}명)"
        else:
            status_text = f"{q_len}명 대기 중"

        # 상태가 부재/회의인 경우 상태 정보 병기
        if stat == "회의":
            btn.setText(f"{dept}\n{status_text}\n[회의 중]")
        elif stat == "부재":
            return_time = det if det else "시간 미정"
            btn.setToolTip(f"복귀 예정: {return_time}")
            btn.setText(f"{dept}\n{status_text}\n[부재: {return_time}]")
        else:
            btn.setText(f"{dept}\n{status_text}")
        
        if pos > 0:
            btn.setStyleSheet("""
                QPushButton {
                    background-color: #c6f6d5;
                    color: #22543d;
                    border: 2px solid #38a169;
                    border-radius: 5px;
                    font-weight: bold;
                    font-size: 12px;
                }
                QPushButton:hover {
                    background-color: #9ae6b4;
                }
            """)
        else:
            btn.setStyleSheet("""
                QPushButton {
                    background-color: #f0fff4;
                    color: #2f855a;
                    border: 1px solid #38a169;
                    border-radius: 5px;
                    font-weight: bold;
                    font-size: 12px;
                }
                QPushButton:hover {
                    background-color: #c6f6d5;
                }
            """)


def main():
    app = QApplication(sys.argv)
    root = data_dir()
    logging.basicConfig(filename=root / 'employee.log', level=logging.WARNING, encoding='utf-8')
    lock = QLockFile(str(root / 'employee.lock'))
    if not lock.tryLock(0):
        QMessageBox.information(None, '실행 중', '직원용 프로그램이 이미 실행 중입니다. 알림 영역에서 열어주세요.')
        return 0
    window = ClientApp()
    app.aboutToQuit.connect(window.stop)
    window.show()
    result = app.exec()
    lock.unlock()
    return result


if __name__ == '__main__':
    sys.exit(main())
