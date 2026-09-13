"""GUI 공통 스타일.

기존에는 app.py / db_session_viewer.py / trajectory_viewer2.py 가 각자
같은 다크 테마 문자열을 들고 있었다. 새 창은 여기 정의를 쓴다.

MAIN_WINDOW_STYLE — 메인 윈도우(QApplication)에 적용하는 전체 스타일
DARK_DIALOG_STYLE — 다이얼로그 전용 스타일
"""

MAIN_WINDOW_STYLE = """
/* ─── 전체 배경 (카드 대비용 더 어두운 톤) ─── */
QMainWindow { background-color: #1a2230; }
QLabel { color: #dfe7f3; }
QLabel[role="caption"] {
    background: #2b3342;
    color: #e8f0ff;
    border: 1px solid #3d4656;
    border-radius: 6px;
    padding: 3px 10px;
    font-weight: 700;
    min-height: 30px;
}
QCheckBox {
    color: #dfe7f3;
    spacing: 8px;
}
QCheckBox::indicator {
    width: 26px;
    height: 26px;
}
QCheckBox[role="caption"] {
    background: #2b3342;
    color: #e8f0ff;
    border: 1px solid #3d4656;
    border-radius: 6px;
    padding: 3px 10px;
    font-weight: 700;
    min-height: 30px;
}
QCheckBox[role="caption"]::indicator {
    width: 26px;
    height: 26px;
}

/* ─── QGroupBox 카드 스타일 + 상단 악센트 바 ─── */
QGroupBox {
    background: #1e2838;
    border: 1px solid #2d3a50;
    border-radius: 8px;
    margin-top: 10px;
    padding-top: 14px;
    color: #cfd8e6;
    font-weight: bold;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 4px;
    font-weight: 700;
}
/* 악센트 바: teal (설정/영상 그룹) */
QGroupBox#groupTeal {
    border-top: 3px solid #2ec4b6;
}
/* 악센트 바: blue (카운팅 그룹) */
QGroupBox#groupBlue {
    border-top: 3px solid #4a90d9;
}
/* 악센트 바: gray (로컬실행 / 보조 그룹) */
QGroupBox#groupGray {
    border-top: 3px solid #5a6a80;
}

/* ─── 입력 필드 ─── */
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QTextEdit {
    background: #2b3342;
    color: #e8f0ff;
    border: 1px solid #3d4656;
    border-radius: 4px;
    padding: 3px 8px;
}

/* ─── QPushButton: 기본은 뮤트된 도구 버튼 ─── */
QPushButton {
    background-color: #293548;
    color: #c8d4e6;
    border: 1px solid #3a4a60;
    border-radius: 4px;
    padding: 3px 10px;
    min-height: 30px;
    font-weight: 700;
}
QPushButton:disabled {
    background-color: #232d3c;
    color: #5a6878;
    border-color: #2a3444;
}
QPushButton:hover {
    background-color: #324058;
    color: #dde6f4;
}
QPushButton:pressed {
    background-color: #1e2a3c;
    border: 1px solid #3a4a60;
}

/* Primary (teal) — 주요 실행 버튼 */
QPushButton[btnType="primary"] {
    background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 #34d0b8, stop:0.5 #2ab8a2, stop:1 #22a08c);
    color: #ffffff;
    border: 1px solid #1d8f7b;
    border-bottom: 2px solid #187a68;
}
QPushButton[btnType="primary"]:hover {
    background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 #40dcc4, stop:0.5 #34c8ae, stop:1 #2ab498);
}
QPushButton[btnType="primary"]:pressed {
    background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 #1d9080, stop:0.5 #2ab8a2, stop:1 #34d0b8);
    border-top: 2px solid #187a68;
    border-bottom: 1px solid #1d8f7b;
}
QPushButton[btnType="primary"]:disabled {
    background-color: #232d3c;
    color: #5a6878;
    border-color: #2a3444;
}

/* Secondary (blue) — 보조 실행 버튼 */
QPushButton[btnType="secondary"] {
    background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 #5f8fc6, stop:0.5 #3d6fa7, stop:1 #335f8f);
    color: #eaf3ff;
    border: 1px solid #2e5077;
    border-bottom: 2px solid #213a56;
}
QPushButton[btnType="secondary"]:hover {
    background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 #6fa1d8, stop:0.5 #4b8cd0, stop:1 #3c76b4);
}
QPushButton[btnType="secondary"]:pressed {
    background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 #2f5e90, stop:0.5 #3d6fa7, stop:1 #5f8fc6);
    border-top: 2px solid #213a56;
    border-bottom: 1px solid #2e5077;
}
QPushButton[btnType="secondary"]:disabled {
    background-color: #232d3c;
    color: #5a6878;
    border-color: #2a3444;
}

/* Danger (red) — 종료/삭제 */
QPushButton[btnType="danger"] {
    background-color: #8b2030;
    color: #ffd0d0;
    border: 1px solid #6b1828;
    border-bottom: 2px solid #551420;
}
QPushButton[btnType="danger"]:hover {
    background-color: #a52838;
}
QPushButton[btnType="danger"]:pressed {
    background-color: #6b1828;
    border-top: 2px solid #551420;
    border-bottom: 1px solid #6b1828;
}

/* ─── 입력 크기 제한 ─── */
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {
    min-height: 30px;
}
QSpinBox, QDoubleSpinBox {
    min-width: 96px;
}
QComboBox {
    min-width: 120px;
}
QSpinBox::up-button, QDoubleSpinBox::up-button {
    width: 22px;
}
QSpinBox::down-button, QDoubleSpinBox::down-button {
    width: 22px;
}
QGroupBox QLineEdit[readonly="true"] {
    background: #252d3a;
    color: #9fb3d2;
}

/* ─── WorkflowStepper ─── */
#stepper {
    background: #1e2838;
    border: 1px solid #2d3a50;
    border-radius: 8px;
}
#stepCircle {
    border-radius: 12px;
    font-weight: 800;
    font-size: 11px;
}
#stepCircle[stepState="done"] {
    background: #2ec4b6;
    color: #ffffff;
    border: 2px solid #2ec4b6;
}
#stepCircle[stepState="active"] {
    background: #1a2230;
    color: #2ec4b6;
    border: 2px solid #2ec4b6;
}
#stepCircle[stepState="pending"] {
    background: #2a3444;
    color: #6c7a92;
    border: 2px solid #3a4656;
}
#stepLabel[stepState="done"]   { color: #8ee8db; font-weight: 700; }
#stepLabel[stepState="active"] { color: #ffffff; font-weight: 700; }
#stepLabel[stepState="pending"]{ color: #6c7a92; }
#stepLine[stepState="done"]    { background: #2ec4b6; }
#stepLine[stepState="pending"] { background: #3a4656; }

/* ─── StatusBar (하단) ─── */
#statusBar {
    background: #1e2838;
    border: 1px solid #2d3a50;
    border-radius: 6px;
}
#statusDot { font-size: 10px; }
#statusDot[dotState="ok"]   { color: #4ade80; }
#statusDot[dotState="warn"] { color: #f59e0b; }
#statusDot[dotState="off"]  { color: #5a6a80; }
#statusText { color: #a0b0c8; font-size: 11px; }

/* ─── QProgressBar ─── */
QProgressBar {
    background: #2a3444;
    border: 1px solid #3a4a60;
    border-radius: 4px;
    text-align: center;
    color: #dfe7f3;
    min-height: 18px;
    font-size: 11px;
}
QProgressBar::chunk {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #2ec4b6, stop:1 #4a90d9);
    border-radius: 3px;
}

/* ─── 구분선(section_divider) ─── */
#dividerLine {
    color: #3a4a60;
}
#dividerText {
    color: #8898b0;
    font-weight: 600;
    padding: 0 8px;
    font-size: 12px;
}

/* ─── Dialog / MessageBox ─── */
QDialog {
    background-color: #232b37;
}
QDialog QLabel {
    color: #eef4ff;
    background: transparent;
    font-weight: 600;
}
QDialog QLineEdit, QDialog QSpinBox, QDialog QDoubleSpinBox, QDialog QComboBox, QDialog QListWidget, QDialog QTextEdit {
    background: #2f3948;
    color: #f4f7ff;
    border: 1px solid #49566b;
    border-radius: 4px;
    padding: 4px 8px;
}
QDialog QPushButton {
    background-color: #446b9e;
    color: #f4f7ff;
    border: 1px solid #31547e;
    border-radius: 4px;
    padding: 5px 12px;
    font-weight: 700;
    min-height: 28px;
}
QDialog QPushButton:hover {
    background-color: #5380bb;
}
QDialog QPushButton:pressed {
    background-color: #35597f;
}
QDialog QAbstractButton {
    color: #f4f7ff;
}
QMessageBox {
    background-color: #202734;
}
QMessageBox QLabel {
    color: #f4f7ff;
    background: transparent;
    min-width: 360px;
    font-size: 10pt;
}
QMessageBox QPushButton {
    background-color: #446b9e;
    color: #f4f7ff;
    border: 1px solid #31547e;
    border-radius: 4px;
    min-width: 88px;
    min-height: 32px;
}
QMessageBox QPushButton:hover {
    background-color: #5380bb;
}
QMessageBox QAbstractButton {
    color: #f4f7ff;
}
"""

DARK_DIALOG_STYLE = """
QDialog {
    background-color: #232b37;
}
QDialog QLabel {
    color: #eef4ff;
    background: transparent;
    font-weight: 600;
}
QDialog QLineEdit,
QDialog QSpinBox,
QDialog QDoubleSpinBox,
QDialog QComboBox,
QDialog QListWidget,
QDialog QPlainTextEdit,
QDialog QTextEdit {
    background: #2f3948;
    color: #f4f7ff;
    border: 1px solid #49566b;
    border-radius: 4px;
    padding: 4px 8px;
}
QDialog QTableWidget {
    background: #2f3948;
    color: #f4f7ff;
    border: 1px solid #49566b;
    border-radius: 4px;
    gridline-color: #49566b;
}
QDialog QHeaderView::section {
    background-color: #35405230;
    background: #384355;
    color: #eef4ff;
    border: 0px;
    border-right: 1px solid #49566b;
    border-bottom: 1px solid #49566b;
    padding: 5px 6px;
    font-weight: 700;
}
QDialog QTableWidget::item:selected {
    background: #446b9e;
}
QDialog QCheckBox {
    color: #eef4ff;
    background: transparent;
    spacing: 8px;
}
QDialog QPushButton {
    background-color: #446b9e;
    color: #f4f7ff;
    border: 1px solid #31547e;
    border-radius: 4px;
    padding: 5px 12px;
    font-weight: 700;
    min-height: 28px;
}
QDialog QPushButton:hover {
    background-color: #5380bb;
}
QDialog QPushButton:pressed {
    background-color: #35597f;
}
"""
