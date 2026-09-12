"""차종 선택 대화상자.

- DetectClassDialog: 탐지 단계에서 어떤 클래스를 잡을지 고른다(allowed_classes).
- CountClassMappingDialog: 집계 단계에서 각 차종을 엑셀 어느 열로 낼지 정한다.

두 창 모두 결과를 ModelProfile 에 반영해 모델별로 저장한다.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.config.model_profiles import (
    OPTIONAL_EXCEL_MAPPING,
    ModelProfile,
    suggest_excel_mapping,
)
from src.ui.theme import DARK_DIALOG_STYLE


class DetectClassDialog(QDialog):
    """모델이 가진 클래스 중 탐지에 사용할 것을 고르는 창."""

    def __init__(self, profile: ModelProfile, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle(f"탐지 차종 선택 - {profile.key}")
        self.setStyleSheet(DARK_DIALOG_STYLE)
        self.setMinimumWidth(460)
        self._profile = profile
        self._checks: Dict[int, QCheckBox] = {}

        layout = QVBoxLayout()
        layout.addWidget(
            QLabel(
                f"모델 '{profile.key}'가 인식하는 클래스입니다.\n"
                "체크한 클래스만 탐지해 DB에 저장합니다. 빼면 나중에 되돌릴 수 없습니다."
            )
        )

        self.table = QTableWidget(0, 3, self)
        self.table.setHorizontalHeaderLabels(["사용", "번호", "클래스 이름"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)

        selected = set(profile.detect_class_ids)
        for row, class_id in enumerate(sorted(profile.classes)):
            self.table.insertRow(row)
            check = QCheckBox()
            check.setChecked(class_id in selected)
            holder = QWidget()
            holder_layout = QHBoxLayout(holder)
            holder_layout.setContentsMargins(10, 0, 0, 0)
            holder_layout.addWidget(check)
            holder_layout.addStretch()
            self.table.setCellWidget(row, 0, holder)
            self.table.setItem(row, 1, QTableWidgetItem(str(class_id)))
            self.table.setItem(row, 2, QTableWidgetItem(str(profile.classes[class_id])))
            self._checks[class_id] = check
        layout.addWidget(self.table)

        tools = QHBoxLayout()
        select_all = QPushButton("전체 선택")
        select_all.clicked.connect(lambda: self._set_all(True))
        clear_all = QPushButton("전체 해제")
        clear_all.clicked.connect(lambda: self._set_all(False))
        tools.addWidget(select_all)
        tools.addWidget(clear_all)
        tools.addStretch()
        layout.addLayout(tools)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.setLayout(layout)

    def _set_all(self, checked: bool) -> None:
        for check in self._checks.values():
            check.setChecked(checked)

    def _on_accept(self) -> None:
        if not self.selected_class_ids():
            QMessageBox.warning(self, "선택 없음", "최소 한 개의 차종을 선택해야 합니다.")
            return
        self.accept()

    def selected_class_ids(self) -> List[int]:
        return sorted(cid for cid, check in self._checks.items() if check.isChecked())


class CountClassMappingDialog(QDialog):
    """DB에 있는 차종을 엑셀 집계 열로 매핑하는 창."""

    EXCLUDED_LABEL = "(제외)"

    def __init__(
        self,
        profile: ModelProfile,
        db_class_counts: Optional[Dict[str, int]] = None,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.setWindowTitle(f"집계 차종 설정 - {profile.key}")
        self.setStyleSheet(DARK_DIALOG_STYLE)
        self.setMinimumSize(680, 520)
        self._profile = profile
        self._db_counts = dict(db_class_counts or {})
        self._combos: Dict[str, QComboBox] = {}

        # DB에 실제로 있는 차종을 우선하고, 모델 클래스 중 아직 안 나온 것도 함께 보여준다.
        self._class_names: List[str] = list(self._db_counts.keys())
        for name in profile.class_names():
            if name and name not in self._class_names:
                self._class_names.append(name)

        layout = QVBoxLayout()
        source = "DB에서 읽은 차종" if self._db_counts else "모델 클래스"
        layout.addWidget(
            QLabel(
                f"{source}을(를) 엑셀 집계 열로 묶습니다.\n"
                "여러 차종을 같은 열 이름으로 지정하면 합산됩니다. '(제외)'는 집계에서 뺍니다."
            )
        )

        self.table = QTableWidget(0, 3, self)
        self.table.setHorizontalHeaderLabels(["차종 (DB/모델)", "DB 트랙 수", "엑셀 집계 열"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)

        known_targets = self._initial_targets()
        for row, name in enumerate(self._class_names):
            self.table.insertRow(row)
            name_item = QTableWidgetItem(name)
            name_item.setFlags(name_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(row, 0, name_item)

            count = self._db_counts.get(name)
            count_item = QTableWidgetItem("-" if count is None else f"{count:,}")
            count_item.setFlags(count_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(row, 1, count_item)

            combo = QComboBox()
            combo.setEditable(True)  # 새 열 이름을 직접 입력할 수 있게 한다
            combo.addItem(self.EXCLUDED_LABEL)
            for target in known_targets:
                combo.addItem(target)
            current = profile.excel_mapping.get(name, "")
            combo.setCurrentText(current if current else self.EXCLUDED_LABEL)
            combo.currentTextChanged.connect(self._refresh_column_order)
            self.table.setCellWidget(row, 2, combo)
            self._combos[name] = combo
        layout.addWidget(self.table)

        layout.addWidget(QLabel("엑셀 열 순서 (위에서부터 왼쪽 열)"))
        order_row = QHBoxLayout()
        self.order_list = QListWidget()
        self.order_list.setMaximumHeight(140)
        order_row.addWidget(self.order_list, 1)

        order_buttons = QVBoxLayout()
        up_btn = QPushButton("위로")
        up_btn.clicked.connect(lambda: self._move_order(-1))
        down_btn = QPushButton("아래로")
        down_btn.clicked.connect(lambda: self._move_order(1))
        order_buttons.addWidget(up_btn)
        order_buttons.addWidget(down_btn)
        order_buttons.addStretch()
        order_row.addLayout(order_buttons)
        layout.addLayout(order_row)

        tools = QHBoxLayout()
        reset_btn = QPushButton("기본값으로 되돌리기")
        reset_btn.clicked.connect(self._reset_to_suggested)
        tools.addWidget(reset_btn)
        tools.addStretch()
        layout.addLayout(tools)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.setLayout(layout)

        self._refresh_column_order()

    def _initial_targets(self) -> List[str]:
        """콤보박스에 미리 채워 둘 열 후보."""
        targets = list(self._profile.excel_columns)
        for value in self._profile.excel_mapping.values():
            if value and value not in targets:
                targets.append(value)
        if not targets:
            suggested_map, suggested_cols = suggest_excel_mapping(self._class_names)
            targets = list(suggested_cols)
            for value in suggested_map.values():
                if value and value not in targets:
                    targets.append(value)
        # 기본 열에는 없지만 이 모델이 인식하는 차종(이륜차·자전거)은
        # 직접 타이핑하지 않고도 고를 수 있게 후보로 넣어 둔다.
        lowered = {n.lower() for n in self._class_names}
        for class_name, target in OPTIONAL_EXCEL_MAPPING.items():
            if class_name in lowered and target not in targets:
                targets.append(target)
        return targets

    def _current_mapping(self) -> Dict[str, str]:
        mapping: Dict[str, str] = {}
        for name, combo in self._combos.items():
            target = combo.currentText().strip()
            if target and target != self.EXCLUDED_LABEL:
                mapping[name] = target
        return mapping

    def _refresh_column_order(self) -> None:
        """매핑이 바뀌면 열 목록을 갱신하되 기존 순서는 최대한 유지한다."""
        targets = list(dict.fromkeys(self._current_mapping().values()))
        previous = [self.order_list.item(i).text() for i in range(self.order_list.count())]
        ordered = [c for c in previous if c in targets]
        for target in targets:
            if target not in ordered:
                ordered.append(target)
        self.order_list.clear()
        self.order_list.addItems(ordered)

    def _move_order(self, delta: int) -> None:
        row = self.order_list.currentRow()
        target_row = row + delta
        if row < 0 or not (0 <= target_row < self.order_list.count()):
            return
        item = self.order_list.takeItem(row)
        self.order_list.insertItem(target_row, item)
        self.order_list.setCurrentRow(target_row)

    def _reset_to_suggested(self) -> None:
        suggested_map, _ = suggest_excel_mapping(self._class_names)
        for name, combo in self._combos.items():
            target = suggested_map.get(name, "")
            combo.setCurrentText(target if target else self.EXCLUDED_LABEL)
        self.order_list.clear()
        self._refresh_column_order()

    def _on_accept(self) -> None:
        if not self._current_mapping():
            QMessageBox.warning(self, "선택 없음", "최소 한 개의 차종을 집계 열에 연결해야 합니다.")
            return
        self.accept()

    def result_mapping(self) -> Dict[str, str]:
        return self._current_mapping()

    def result_columns(self) -> List[str]:
        return [self.order_list.item(i).text() for i in range(self.order_list.count())]
