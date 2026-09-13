import json
import sqlite3
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.ui.widgets import fit_to_screen


TABLE_CANDIDATES: Sequence[str] = (
    "tracks",
    "track_trajs",
    "track_merge_runs",
    "track_merge_map",
    "track_merge_map_auto",
    "track_merge_map_manual",
    "track_merge_exclude_manual",
    "track_exclusion_runs",
    "track_exclusion_map",
    "track_virtual_events",
    "track_line_events",
    "track_line_summary",
    "merged_tracks",
)


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    try:
        row = conn.execute(
            "select 1 from sqlite_master where type='table' and name=? limit 1",
            (table_name,),
        ).fetchone()
        return row is not None
    except Exception:
        return False


def _table_columns(conn: sqlite3.Connection, table_name: str) -> List[str]:
    try:
        rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        return [str(r[1]) for r in rows if len(r) >= 2]
    except Exception:
        return []


def _has_session_rows(conn: sqlite3.Connection, table_name: str, session_id: Optional[str]) -> Tuple[bool, int]:
    cols = _table_columns(conn, table_name)
    if "session_id" not in cols:
        return False, 0
    try:
        if session_id:
            row = conn.execute(
                f"select count(*) from {table_name} where session_id = ?",
                (session_id,),
            ).fetchone()
        else:
            row = conn.execute(
                f"select count(*) from {table_name} where session_id is not null and session_id != ''"
            ).fetchone()
        count = int(row[0] or 0) if row else 0
        return count > 0, count
    except Exception:
        return False, 0


def _format_cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return f"<BLOB {len(value)} bytes>"
    if isinstance(value, (dict, list, tuple)):
        try:
            text = json.dumps(value, ensure_ascii=False)
        except Exception:
            text = str(value)
    else:
        text = str(value)
    text = text.replace("\r", " ").replace("\n", " ")
    if len(text) > 160:
        return text[:157] + "..."
    return text


class DbSessionViewerDialog(QDialog):
    def __init__(self, db_path: Path, session_id: Optional[str], parent=None, table_candidates: Optional[Sequence[str]] = None, title: Optional[str] = None):
        super().__init__(parent)
        self.db_path = Path(db_path)
        self.session_id = str(session_id).strip() if session_id else None
        self.table_candidates: Sequence[str] = tuple(table_candidates or TABLE_CANDIDATES)
        self.row_limit = 500
        self.setWindowTitle(str(title or "DB Viewer"))
        fit_to_screen(self, 1300, 760)
        self._build_ui()
        self._apply_style()
        self._load_table_list()

    def _build_ui(self) -> None:
        root = QVBoxLayout()

        header = QLabel(f"DB: {self.db_path} | session_id: {self.session_id or '(전체)'}")
        header.setWordWrap(True)
        self.status_label = QLabel("")
        self.refresh_btn = QPushButton("새로고침")
        self.refresh_btn.clicked.connect(self._load_table_list)

        top_row = QHBoxLayout()
        top_row.addWidget(header, stretch=1)
        top_row.addWidget(self.refresh_btn)

        body = QHBoxLayout()
        left = QVBoxLayout()
        left.addWidget(QLabel("테이블 목록"))
        self.table_list = QListWidget()
        self.table_list.itemSelectionChanged.connect(self._on_table_selected)
        left.addWidget(self.table_list, stretch=1)

        right = QVBoxLayout()
        right.addWidget(QLabel("테이블 내용"))
        self.grid = QTableWidget()
        self.grid.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.grid.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.grid.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.grid.setAlternatingRowColors(True)
        self.grid.setSortingEnabled(False)
        right.addWidget(self.grid, stretch=1)

        left_wrap = QWidget()
        left_wrap.setLayout(left)
        right_wrap = QWidget()
        right_wrap.setLayout(right)
        body.addWidget(left_wrap, stretch=0)
        body.addWidget(right_wrap, stretch=1)

        root.addLayout(top_row)
        root.addLayout(body, stretch=1)
        root.addWidget(self.status_label)
        self.setLayout(root)

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QDialog {
                background-color: #232b37;
            }
            QLabel {
                color: #eef4ff;
                background: transparent;
            }
            QListWidget, QTableWidget {
                background: #2f3948;
                color: #f4f7ff;
                border: 1px solid #49566b;
                border-radius: 4px;
                gridline-color: #49566b;
                alternate-background-color: #293240;
                selection-background-color: #456c9f;
                selection-color: #ffffff;
            }
            QHeaderView::section {
                background: #3a4659;
                color: #f4f7ff;
                padding: 4px 6px;
                border: 1px solid #49566b;
                font-weight: 700;
            }
            QPushButton {
                background-color: #446b9e;
                color: #f4f7ff;
                border: 1px solid #31547e;
                border-radius: 4px;
                padding: 5px 12px;
                font-weight: 700;
                min-height: 28px;
            }
            QPushButton:hover {
                background-color: #5380bb;
            }
            """
        )

    def _load_table_list(self) -> None:
        self.table_list.clear()
        self.grid.clear()
        self.grid.setRowCount(0)
        self.grid.setColumnCount(0)

        if not self.db_path.exists():
            self.status_label.setText(f"DB 없음: {self.db_path}")
            return

        try:
            with sqlite3.connect(self.db_path) as conn:
                for table_name in self.table_candidates:
                    if not _table_exists(conn, table_name):
                        continue
                    has_rows, count = _has_session_rows(conn, table_name, self.session_id)
                    if not has_rows:
                        continue
                    item = QListWidgetItem(f"{table_name} ({count})")
                    item.setData(Qt.ItemDataRole.UserRole, table_name)
                    self.table_list.addItem(item)
        except Exception as exc:
            QMessageBox.critical(self, "DB 조회 오류", str(exc))
            self.status_label.setText("테이블 목록 조회 실패")
            return

        if self.table_list.count() > 0:
            self.table_list.setCurrentRow(0)
        else:
            self.status_label.setText("현재 session_id 기준으로 표시할 테이블이 없습니다.")

    def _on_table_selected(self) -> None:
        item = self.table_list.currentItem()
        if item is None:
            return
        table_name = str(item.data(Qt.ItemDataRole.UserRole) or "")
        if not table_name:
            return
        self._load_table_rows(table_name)

    def _load_table_rows(self, table_name: str) -> None:
        try:
            with sqlite3.connect(self.db_path) as conn:
                cols = _table_columns(conn, table_name)
                if not cols or "session_id" not in cols:
                    self.status_label.setText(f"{table_name}: session_id 컬럼이 없어 표시하지 않습니다.")
                    self.grid.clear()
                    self.grid.setRowCount(0)
                    self.grid.setColumnCount(0)
                    return
                sql = f"select * from {table_name}"
                params: List[object] = []
                if self.session_id:
                    sql += " where session_id = ?"
                    params.append(self.session_id)
                else:
                    sql += " where session_id is not null and session_id != ''"
                sql += " limit ?"
                params.append(int(self.row_limit))
                rows = conn.execute(sql, tuple(params)).fetchall()
        except Exception as exc:
            QMessageBox.critical(self, "테이블 조회 오류", str(exc))
            self.status_label.setText(f"{table_name}: 조회 실패")
            return

        self.grid.clear()
        self.grid.setColumnCount(len(cols))
        self.grid.setHorizontalHeaderLabels(cols)
        self.grid.setRowCount(len(rows))

        for r_idx, row in enumerate(rows):
            for c_idx, value in enumerate(row):
                cell = QTableWidgetItem(_format_cell(value))
                cell.setFlags(cell.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.grid.setItem(r_idx, c_idx, cell)

        self.grid.resizeColumnsToContents()
        self.status_label.setText(f"{table_name}: {len(rows)}행 표시 (최대 {self.row_limit}행)")
