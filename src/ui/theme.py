"""GUI 공통 스타일.

기존에는 app.py / db_session_viewer.py / trajectory_viewer2.py 가 각자
같은 다크 테마 문자열을 들고 있었다. 새 창은 여기 정의를 쓴다.

MAIN_WINDOW_STYLE — 메인 윈도우(QApplication)에 적용하는 전체 스타일
DARK_DIALOG_STYLE — 다이얼로그 전용 스타일
"""

MAIN_WINDOW_STYLE = """
QMainWindow { background-color: #1f2733; }
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
QGroupBox {
    border: 1px solid #3a4556;
    border-radius: 6px;
    margin-top: 10px;
    color: #cfd8e6;
    font-weight: bold;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 4px;
    font-weight: 700;
}
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QTextEdit {
    background: #2b3342;
    color: #e8f0ff;
    border: 1px solid #3d4656;
    border-radius: 4px;
    padding: 3px 8px;
}
QPushButton {
    background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 #5f8fc6, stop:0.5 #3d6fa7, stop:1 #335f8f);
    color: #eaf3ff;
    border: 1px solid #2e5077;
    border-bottom: 2px solid #213a56;
    border-radius: 4px;
    padding: 3px 10px;
    min-height: 30px;
    font-weight: 700;
}
QPushButton:disabled {
    background-color: #2c3444;
    color: #6c7a92;
    border-color: #2c3444;
}
QPushButton:hover {
    background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 #6fa1d8, stop:0.5 #4b8cd0, stop:1 #3c76b4);
}
QPushButton:pressed {
    background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 #2f5e90, stop:0.5 #3d6fa7, stop:1 #5f8fc6);
    border: 1px solid #2a4668;
    border-top: 2px solid #213a56;
    border-bottom: 1px solid #2e5077;
    padding-top: 4px;
}
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
QDialog QPushButton, QDialog QAbstractButton {
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
    min-width: 88px;
    min-height: 32px;
    color: #f4f7ff;
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
