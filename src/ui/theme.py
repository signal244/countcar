"""GUI 공통 스타일.

기존에는 app.py / db_session_viewer.py / trajectory_viewer2.py 가 각자
같은 다크 테마 문자열을 들고 있었다. 새 창은 여기 정의를 쓴다.
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
