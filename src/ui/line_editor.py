import json
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)


class LineEditorWindow(QMainWindow):
    """Simple JSON editor for line/ROI settings."""

    def __init__(self, initial_path: Optional[Path] = None):
        super().__init__()
        self.setWindowTitle("Line / ROI Settings")
        self.resize(900, 700)
        self.current_path: Optional[Path] = initial_path

        self.text = QPlainTextEdit()
        self.text.setFont(QFont("Consolas", 10))

        self.status_label = QLabel("Open a line settings JSON to edit.")

        # Controls for adding a line with id/bound placeholders
        self.line_id_input = QLineEdit()
        self.line_id_input.setPlaceholderText("line_1")
        self.bound_input = QLineEdit()
        self.bound_input.setPlaceholderText("north_bound")

        add_line_btn = QPushButton("라인 추가 (id/bound)")
        add_line_btn.clicked.connect(self.add_line_entry)

        load_btn = QPushButton("Open JSON")
        save_btn = QPushButton("Save")
        save_as_btn = QPushButton("Save As")

        load_btn.clicked.connect(self.load_json)
        save_btn.clicked.connect(self.save_json)
        save_as_btn.clicked.connect(lambda: self.save_json(save_as=True))

        top = QHBoxLayout()
        top.addWidget(load_btn)
        top.addWidget(save_btn)
        top.addWidget(save_as_btn)
        top.addStretch()

        add_line_layout = QHBoxLayout()
        add_line_layout.addWidget(QLabel("라인 ID"))
        add_line_layout.addWidget(self.line_id_input)
        add_line_layout.addWidget(QLabel("bound"))
        add_line_layout.addWidget(self.bound_input)
        add_line_layout.addWidget(add_line_btn)
        add_line_layout.addStretch()

        layout = QVBoxLayout()
        layout.addLayout(top)
        layout.addLayout(add_line_layout)
        layout.addWidget(self.text)
        layout.addWidget(self.status_label)

        container = QWidget()
        container.setLayout(layout)
        self.setCentralWidget(container)

        if self.current_path:
            self.load_from_path(self.current_path)

    def load_json(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Open line settings JSON",
            "",
            "JSON Files (*.json);;All Files (*)",
        )
        if path:
            self.current_path = Path(path)
            self.load_from_path(self.current_path)

    def load_from_path(self, path: Path) -> None:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.text.setPlainText(json.dumps(data, indent=2, ensure_ascii=False))
            self.status_label.setText(f"Loaded: {path}")
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Error", f"Failed to load {path}:\n{exc}")

    def save_json(self, save_as: bool = False) -> None:
        if save_as or not self.current_path:
            path, _ = QFileDialog.getSaveFileName(
                self,
                "Save line settings JSON",
                str(self.current_path) if self.current_path else "",
                "JSON Files (*.json);;All Files (*)",
            )
            if not path:
                return
            self.current_path = Path(path)

        try:
            parsed = json.loads(self.text.toPlainText())
            with open(self.current_path, "w", encoding="utf-8") as f:
                json.dump(parsed, f, indent=2, ensure_ascii=False)
            self.status_label.setText(f"Saved: {self.current_path}")
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Error", f"Failed to save:\n{exc}")

    def add_line_entry(self) -> None:
        """Append a line entry with id/bound placeholders."""
        line_id = self.line_id_input.text().strip() or "line_new"
        bound = self.bound_input.text().strip() or "north_bound"
        try:
            data = json.loads(self.text.toPlainText() or "{}")
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Error", f"JSON 파싱 실패: {exc}")
            return

        if "lines" not in data or not isinstance(data.get("lines"), list):
            data["lines"] = []
        new_line = {
            "id": line_id,
            "points": [[0, 0], [100, 100]],
            "bound": bound,
        }
        data["lines"].append(new_line)
        self.text.setPlainText(json.dumps(data, indent=2, ensure_ascii=False))
        self.status_label.setText(f"라인 추가: id={line_id}, bound={bound}")
