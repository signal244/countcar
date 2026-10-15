"""여러 영상을 골라 차례로 탐지·궤적 저장하는 창.

교차로·세션은 영상 이름(<교차로명>_<세션명>)에서 자동으로 정하고 표에서 고칠 수 있다.
저장 DB 는 교차로마다 자동(tracks_<교차로명>.sqlite). 모델·이미지 크기는 기본이 메인 창 설정이며
영상별로 바꿀 수 있다. 실제 처리는 src/services/batch_detection.run_batch (CLI·Colab 과 같은 코드).
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from src.config.model_profiles import ModelProfileStore, list_model_files
from src.ui.class_dialogs import DetectClassDialog
from src.services.batch_detection import (
    BatchJob,
    db_path_for,
    jobs_to_dicts,
    parse_video_name,
    run_batch,
    session_state,
)
from src.services.colab_export import is_colab_path, to_colab_path
from src.ui.theme import DARK_DIALOG_STYLE
from src.ui.widgets import apply_button_tooltips, fit_to_screen

logger = logging.getLogger(__name__)

COL_VIDEO, COL_JUNCTION, COL_SESSION, COL_DB, COL_MODEL, COL_CLASSES, COL_IMGSZ, COL_STATUS = range(8)
HEADERS = ["영상", "교차로명", "세션명", "저장 DB (자동)", "모델", "탐지 차종", "이미지 크기", "상태"]
# 처음 열 폭(사용자가 머리글 경계를 끌어 바꿀 수 있다). 마지막 '상태' 열은 남는 폭을 쓴다.
COLUMN_WIDTHS = {COL_VIDEO: 260, COL_JUNCTION: 130, COL_SESSION: 170, COL_DB: 190,
                 COL_MODEL: 170, COL_CLASSES: 190, COL_IMGSZ: 100}
DEFAULT_SESSION = "기본"   # 파일 이름에서 세션명을 못 읽으면 넣는 값(표에서 고친다)
CURRENT = "(현재 설정)"
IMGSZ_CHOICES = [CURRENT, "auto", "640", "960", "1280", "1920"]
VIDEO_FILTER = "영상 (*.mp4 *.avi *.mkv *.mov *.m4v *.ts);;모든 파일 (*)"
# 세션 칸에 기억해 두는 값: 모델 이름을 붙이기 전 세션명, 마지막으로 자동으로 붙인 세션명.
ROLE_BASE_SESSION = Qt.ItemDataRole.UserRole
ROLE_AUTO_SESSION = Qt.ItemDataRole.UserRole + 1
STATUS_TEXT = {"running": "▶ 진행 중", "done": "✔ 완료", "skipped": "건너뜀", "failed": "✖ 실패", "stopped": "■ 중지"}


def _frame_count(video: Path) -> int:
    try:
        import cv2

        cap = cv2.VideoCapture(str(video))
        count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        cap.release()
        return count
    except Exception:
        logger.debug("프레임 수 읽기 실패: %s", video, exc_info=True)
        return 0


class BatchWorker(QThread):
    log = Signal(int, str)
    status = Signal(int, str, str)
    done = Signal(list)

    def __init__(self, jobs: List[BatchJob], cfg_path: Path, base_overrides: Dict, existing: str):
        super().__init__()
        self.jobs = jobs
        self.cfg_path = cfg_path
        self.base_overrides = base_overrides
        self.existing = existing
        self.stop_after_current = False

    def run(self) -> None:
        try:
            results = run_batch(
                self.jobs,
                cfg_path=self.cfg_path,
                base_overrides=self.base_overrides,
                existing=self.existing,
                progress_cb=self.log.emit,
                status_cb=self.status.emit,
                stop_now_cb=self.isInterruptionRequested,
                stop_after_current_cb=lambda: self.stop_after_current,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("일괄 탐지 실패", exc_info=True)
            self.log.emit(-1, f"[error] {exc}")
            results = []
        self.done.emit(results)


class BatchDetectWindow(QDialog):
    def __init__(self, cfg_path: Path, base_overrides_fn: Callable[[], Dict],
                 is_busy_fn: Callable[[], bool] = lambda: False, parent=None,
                 profile_store: Optional[ModelProfileStore] = None):
        super().__init__(parent)
        self.setWindowTitle("일괄 탐지 (객체 탐지 + 궤적 저장)")
        self.setStyleSheet(DARK_DIALOG_STYLE)
        self.cfg_path = cfg_path
        self.base_overrides_fn = base_overrides_fn
        self.is_busy_fn = is_busy_fn
        self.worker: Optional[BatchWorker] = None
        self.frame_totals: Dict[int, int] = {}
        self._logged_pct: Dict[int, int] = {}
        # 모델별 탐지 차종 설정(메인 창과 같은 저장소를 쓴다).
        self.profile_store = profile_store or ModelProfileStore(Path("config/model_profiles.json"))
        self._build_ui()
        fit_to_screen(self, 1250, 650)

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "영상 이름은 <교차로명>_<세션명> (예: 경원교차로_오전첨두.mp4). 교차로마다 DB 하나, 영상마다 세션 하나로 저장합니다.\n"
            "분석라인 없이 화면 전체를 탐지합니다. 모델·이미지 크기는 기본으로 메인 창 설정을 따르며 영상별로 바꿀 수 있습니다.\n"
            "같은 영상을 여러 모델로 돌리려면 [선택 행 복제] 후 모델을 바꾸세요. 세션명 뒤에 모델 이름이 붙어 따로 저장됩니다."))

        tools = QHBoxLayout()
        self.add_btn = QPushButton("영상 추가…")
        self.add_btn.clicked.connect(self.on_add)
        self.remove_btn = QPushButton("선택 제거")
        self.remove_btn.clicked.connect(self.on_remove)
        self.copy_btn = QPushButton("선택 행 복제")
        self.copy_btn.setToolTip("선택한 영상을 한 줄 더 추가합니다. 같은 영상을 다른 모델로 분석할 때 씁니다.\n"
                                 "복제한 줄의 모델을 바꾸면 세션명 뒤에 모델 이름이 자동으로 붙습니다(예: 오전첨두_yolo8m_v1).")
        self.copy_btn.clicked.connect(self.on_duplicate)
        self.up_btn = QPushButton("▲ 위로")
        self.up_btn.clicked.connect(lambda: self.on_move(-1))
        self.down_btn = QPushButton("▼ 아래로")
        self.down_btn.clicked.connect(lambda: self.on_move(1))
        for b in (self.add_btn, self.remove_btn, self.copy_btn, self.up_btn, self.down_btn):
            tools.addWidget(b)
        tools.addStretch()
        self.export_btn = QPushButton("Colab용 내보내기")
        self.export_btn.setToolTip("이 목록(영상별 모델·크기 포함)을 Colab 일괄탐지 노트북에서 쓸 파일로 저장합니다.\n"
                                   "영상은 Google Drive 안에 있어야 Colab 에서 열 수 있습니다.")
        self.export_btn.clicked.connect(self.on_export_colab)
        tools.addWidget(self.export_btn)
        layout.addLayout(tools)

        self.table = QTableWidget(0, len(HEADERS))
        self.table.setHorizontalHeaderLabels(HEADERS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        header = self.table.horizontalHeader()
        # 머리글 경계를 끌어 열 폭을 바꿀 수 있게 한다.
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)
        # 줄 안의 콤보·버튼이 아래 줄을 덮지 않게 줄 높이를 넉넉히 둔다.
        self.table.verticalHeader().setDefaultSectionSize(36)
        for col, width in COLUMN_WIDTHS.items():
            self.table.setColumnWidth(col, width)
        self.table.itemChanged.connect(self._on_item_changed)
        layout.addWidget(self.table, stretch=3)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(2000)
        layout.addWidget(self.log_view, stretch=2)

        run_row = QHBoxLayout()
        self.summary_label = QLabel("")
        run_row.addWidget(self.summary_label, stretch=1)
        self.start_btn = QPushButton("▶ 시작")
        self.start_btn.setProperty("btnType", "primary")
        self.start_btn.clicked.connect(self.on_start)
        self.after_btn = QPushButton("현재 영상 끝나면 멈춤")
        self.after_btn.clicked.connect(self.on_stop_after_current)
        self.stop_btn = QPushButton("즉시 중지")
        self.stop_btn.setProperty("btnType", "danger")
        self.stop_btn.clicked.connect(self.on_stop_now)
        for b in (self.start_btn, self.after_btn, self.stop_btn):
            run_row.addWidget(b)
        layout.addLayout(run_row)
        apply_button_tooltips(self, {
            "▶ 시작": "표의 영상을 위에서부터 차례로 탐지·궤적 저장합니다. 이미 완료한 영상은 건너뜁니다.",
            "현재 영상 끝나면 멈춤": "진행 중인 영상은 끝까지 처리하고, 다음 영상부터는 시작하지 않습니다.",
            "즉시 중지": "진행 중인 영상을 멈춥니다. 그 영상의 결과는 저장하지 않고 기존 데이터는 그대로 둡니다.",
        })
        self._set_running(False)

    def _set_running(self, running: bool) -> None:
        for w in (self.add_btn, self.remove_btn, self.copy_btn, self.up_btn, self.down_btn, self.start_btn, self.export_btn):
            w.setEnabled(not running)
        self.after_btn.setEnabled(running)
        self.stop_btn.setEnabled(running)
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers if running
            else QAbstractItemView.EditTrigger.DoubleClicked | QAbstractItemView.EditTrigger.EditKeyPressed)
        for r in range(self.table.rowCount()):
            for col in (COL_MODEL, COL_CLASSES, COL_IMGSZ):
                widget = self.table.cellWidget(r, col)
                if widget is not None:
                    widget.setEnabled(not running)

    # ------------------------------------------------------------------ 표 조작
    @staticmethod
    def _cell_combo() -> QComboBox:
        # 앱 전체 스타일의 콤보 최소 폭(120px)이 좁은 열을 넘어 옆 칸(상태)을 가리지 않게 한다.
        combo = QComboBox()
        combo.setStyleSheet("QComboBox { min-width: 0px; }")
        combo.setMinimumWidth(0)
        return combo

    def _model_combo(self) -> QComboBox:
        combo = self._cell_combo()
        combo.addItem(CURRENT, "")
        for path in list_model_files("models"):
            combo.addItem(path.name, str(path))
        return combo

    def _imgsz_combo(self) -> QComboBox:
        combo = self._cell_combo()
        for text in IMGSZ_CHOICES:
            combo.addItem(text, "" if text == CURRENT else text)
        return combo

    def add_video(self, video: Path) -> None:
        junction, session = parse_video_name(video)
        session = session or DEFAULT_SESSION   # 늘 채워 두고 표에서 고치게 한다
        row = self.table.rowCount()
        self.table.blockSignals(True)
        self.table.insertRow(row)
        item = QTableWidgetItem(video.name)
        item.setData(Qt.ItemDataRole.UserRole, str(video))
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        item.setToolTip(str(video))
        self.table.setItem(row, COL_VIDEO, item)
        self.table.setItem(row, COL_JUNCTION, QTableWidgetItem(junction))
        session_item = QTableWidgetItem(session)
        session_item.setData(ROLE_BASE_SESSION, session)
        session_item.setData(ROLE_AUTO_SESSION, session)
        self.table.setItem(row, COL_SESSION, session_item)
        for col in (COL_DB, COL_STATUS):
            cell = QTableWidgetItem("")
            cell.setFlags(cell.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(row, col, cell)
        model_combo = self._model_combo()
        model_combo.currentIndexChanged.connect(lambda _i, combo=model_combo: self._on_model_changed(combo))
        self.table.setCellWidget(row, COL_MODEL, model_combo)
        classes_btn = QPushButton("")
        classes_btn.setProperty("class_ids", None)
        classes_btn.setStyleSheet("QPushButton { padding: 1px 6px; min-height: 0px; }")   # 표 줄 높이에 맞춤
        classes_btn.clicked.connect(lambda _c=False, btn=classes_btn: self._on_pick_classes(btn))
        self.table.setCellWidget(row, COL_CLASSES, classes_btn)
        self.table.setCellWidget(row, COL_IMGSZ, self._imgsz_combo())
        self.table.blockSignals(False)
        self._update_classes_label(row)
        self._refresh_row(row)

    # ------------------------------------------------------------------ 탐지 차종
    def _row_model_path(self, row: int) -> str:
        """이 줄이 실제로 쓸 모델(현재 설정이면 메인 창 모델)."""
        model = self.table.cellWidget(row, COL_MODEL).currentData() or ""
        if model:
            return str(model)
        try:
            return str(self.base_overrides_fn().get("model_path") or "")
        except Exception:
            logger.debug("메인 창 모델 경로 읽기 실패", exc_info=True)
            return ""

    def _row_class_ids(self, row: int) -> Optional[List[int]]:
        """이 줄에서 직접 고른 차종. 없으면 None(모델별 저장값 또는 자동)."""
        ids = self.table.cellWidget(row, COL_CLASSES).property("class_ids")
        return [int(c) for c in ids] if ids else None

    def _update_classes_label(self, row: int) -> None:
        btn = self.table.cellWidget(row, COL_CLASSES)
        model = self._row_model_path(row)
        profile = self.profile_store.get_cached(model) if model else None
        saved = list(profile.detect_class_ids) if profile and profile.detect_class_ids else None
        ids = self._row_class_ids(row) or saved
        if ids and profile and profile.classes:
            names = [str(profile.classes.get(int(c), c)) for c in ids]
            text = f"{len(names)}종: " + ", ".join(names)
        elif ids:
            text = f"{len(ids)}종 선택"
        else:
            text = "자동 (차량만)"
        btn.setText(text if len(text) <= 26 else text[:25] + "…")
        model_name = Path(model).name if model else "-"
        btn.setToolTip(f"모델: {model_name}\n{text}\n누르면 이 영상(모델)에서 탐지할 차종을 고릅니다.")

    def _on_pick_classes(self, btn: QPushButton) -> None:
        row = next((r for r in range(self.table.rowCount()) if self.table.cellWidget(r, COL_CLASSES) is btn), -1)
        if row < 0:
            return
        model = self._row_model_path(row)
        if not model:
            QMessageBox.warning(self, "모델 없음", "이 줄의 모델을 먼저 고르세요.")
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            profile = self.profile_store.get(model)   # 처음 한 번은 모델 파일을 열어 클래스를 읽는다
        finally:
            QApplication.restoreOverrideCursor()
        if not profile.has_classes:
            QMessageBox.warning(self, "클래스 없음", f"모델에서 클래스를 읽지 못했습니다:\n{model}")
            return
        chosen = self._row_class_ids(row)
        if chosen:
            profile.detect_class_ids = list(chosen)
        dialog = DetectClassDialog(profile, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        ids = dialog.selected_class_ids()
        btn.setProperty("class_ids", ids)
        # 모델별 기본값으로도 저장한다(메인 창 '탐지 차종 선택'과 같은 설정).
        profile.detect_class_ids = list(ids)
        self.profile_store.put(profile)
        for r in range(self.table.rowCount()):
            self._update_classes_label(r)

    def _refresh_row(self, row: int) -> None:
        junction = self._text(row, COL_JUNCTION)
        self.table.blockSignals(True)
        self.table.item(row, COL_DB).setText(db_path_for(junction).name if junction else "")
        state = ""
        job = self._job(row)
        if job is not None:
            state = {"done": "이미 완료됨", "existing": "기존 세션 있음"}.get(
                session_state(db_path_for(job.junction), job.session_id), "대기")
        elif not self._text(row, COL_SESSION):
            state = "세션명 필요"
        self.table.item(row, COL_STATUS).setText(state)
        self.table.blockSignals(False)

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if item.column() in (COL_JUNCTION, COL_SESSION):
            self._refresh_row(item.row())

    def _text(self, row: int, col: int) -> str:
        item = self.table.item(row, col)
        return item.text().strip() if item else ""

    def _job(self, row: int) -> Optional[BatchJob]:
        junction, session = self._text(row, COL_JUNCTION), self._text(row, COL_SESSION)
        if not junction or not session:
            return None
        video = Path(self.table.item(row, COL_VIDEO).data(Qt.ItemDataRole.UserRole))
        model = self.table.cellWidget(row, COL_MODEL).currentData() or ""
        imgsz = self.table.cellWidget(row, COL_IMGSZ).currentData() or ""
        class_ids = self._row_class_ids(row)
        if class_ids is None and model:
            # 다른 모델을 고른 줄은 그 모델에 저장된 탐지 차종을 쓴다(없으면 자동).
            profile = self.profile_store.get_cached(model)
            if profile is not None and profile.detect_class_ids:
                class_ids = list(profile.detect_class_ids)
        return BatchJob(video, junction, session, model, imgsz, class_ids)

    def on_add(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "탐지할 영상 선택", "", VIDEO_FILTER)
        existing = {self.table.item(r, COL_VIDEO).data(Qt.ItemDataRole.UserRole) for r in range(self.table.rowCount())}
        for p in paths:
            if str(Path(p)) not in existing:
                self.add_video(Path(p))

    def _video_of(self, row: int) -> str:
        return str(self.table.item(row, COL_VIDEO).data(Qt.ItemDataRole.UserRole))

    def _on_model_changed(self, combo: QComboBox) -> None:
        """같은 영상이 여러 줄이면, 모델을 바꾼 줄의 세션명 뒤에 모델 이름을 붙여 세션이 겹치지 않게 한다.

        사용자가 세션명을 직접 고친 줄은 건드리지 않는다.
        """
        row = next((r for r in range(self.table.rowCount()) if self.table.cellWidget(r, COL_MODEL) is combo), -1)
        if row < 0:
            return
        # 차종 번호는 모델마다 다르다. 모델을 바꾸면 이 줄에서 고른 차종은 새 모델 기준으로 다시 정한다.
        self.table.cellWidget(row, COL_CLASSES).setProperty("class_ids", None)
        self._update_classes_label(row)
        video = self._video_of(row)
        if sum(1 for r in range(self.table.rowCount()) if self._video_of(r) == video) < 2:
            return
        item = self.table.item(row, COL_SESSION)
        base = str(item.data(ROLE_BASE_SESSION) or "")
        last_auto = str(item.data(ROLE_AUTO_SESSION) or "")
        if item.text().strip() not in (base, last_auto):
            return
        model = combo.currentData() or ""
        new_text = f"{base}_{Path(model).stem}" if model and base else base
        self.table.blockSignals(True)
        item.setText(new_text)
        item.setData(ROLE_AUTO_SESSION, new_text)
        self.table.blockSignals(False)
        self._refresh_row(row)

    def on_duplicate(self) -> None:
        """선택한 줄을 바로 아래에 복제한다(같은 영상을 다른 모델로 돌릴 때)."""
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        if not rows:
            QMessageBox.information(self, "선택 없음", "복제할 영상 줄을 먼저 선택하세요.")
            return
        snaps = []
        for r in range(self.table.rowCount()):
            snaps.append(self._row_snapshot(r))
            if r in rows:
                copy = dict(self._row_snapshot(r))
                # 복제본은 모델 이름이 붙기 전 세션명에서 시작한다.
                copy["session"] = copy["base_session"]
                copy["auto_session"] = copy["base_session"]
                copy["model"] = 0
                copy["class_ids"] = None   # 차종 번호는 모델 기준이라 모델과 함께 처음 값으로 되돌린다
                snaps.append(copy)
        self.table.setRowCount(0)
        for snap in snaps:
            self._restore_row(snap)
        self.log_view.appendPlainText(
            f"[복제] {len(rows)}줄을 복제했습니다. 복제한 줄의 모델을 바꾸면 세션명 뒤에 모델 이름이 붙습니다.")

    def on_remove(self) -> None:
        for row in sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True):
            self.table.removeRow(row)

    def on_move(self, step: int) -> None:
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        if len(rows) != 1:
            return
        row, target = rows[0], rows[0] + step
        if not 0 <= target < self.table.rowCount():
            return
        jobs = [self._row_snapshot(r) for r in range(self.table.rowCount())]
        jobs[row], jobs[target] = jobs[target], jobs[row]
        self.table.setRowCount(0)
        for snap in jobs:
            self._restore_row(snap)
        self.table.selectRow(target)

    def _row_snapshot(self, row: int) -> Dict:
        return {
            "video": Path(self.table.item(row, COL_VIDEO).data(Qt.ItemDataRole.UserRole)),
            "junction": self._text(row, COL_JUNCTION),
            "session": self._text(row, COL_SESSION),
            "base_session": str(self.table.item(row, COL_SESSION).data(ROLE_BASE_SESSION) or self._text(row, COL_SESSION)),
            "auto_session": str(self.table.item(row, COL_SESSION).data(ROLE_AUTO_SESSION) or ""),
            "model": self.table.cellWidget(row, COL_MODEL).currentIndex(),
            "class_ids": self._row_class_ids(row),
            "imgsz": self.table.cellWidget(row, COL_IMGSZ).currentIndex(),
        }

    def _restore_row(self, snap: Dict) -> None:
        self.add_video(snap["video"])
        row = self.table.rowCount() - 1
        self.table.blockSignals(True)
        self.table.item(row, COL_JUNCTION).setText(snap["junction"])
        session_item = self.table.item(row, COL_SESSION)
        session_item.setText(snap["session"])
        session_item.setData(ROLE_BASE_SESSION, snap.get("base_session", snap["session"]))
        session_item.setData(ROLE_AUTO_SESSION, snap.get("auto_session", snap["session"]))
        self.table.blockSignals(False)
        model_combo = self.table.cellWidget(row, COL_MODEL)
        model_combo.blockSignals(True)   # 복원할 때는 세션명을 다시 바꾸지 않는다
        model_combo.setCurrentIndex(snap["model"])
        model_combo.blockSignals(False)
        self.table.cellWidget(row, COL_CLASSES).setProperty("class_ids", snap.get("class_ids"))
        self._update_classes_label(row)
        self.table.cellWidget(row, COL_IMGSZ).setCurrentIndex(snap["imgsz"])
        self._refresh_row(row)

    # ------------------------------------------------------------------ 실행
    def _collect_jobs(self) -> Optional[List[BatchJob]]:
        """표의 작업 목록. 빠진 이름·없는 영상·중복 세션이 있으면 알리고 None."""
        if self.table.rowCount() == 0:
            QMessageBox.information(self, "영상 없음", "먼저 영상을 추가하세요.")
            return None
        jobs: List[BatchJob] = []
        for row in range(self.table.rowCount()):
            job = self._job(row)
            if job is None:
                QMessageBox.warning(self, "이름 확인", f"{row + 1}번째 영상의 교차로명과 세션명을 입력하세요.")
                return None
            if not job.video.is_file():
                QMessageBox.warning(self, "영상 없음", f"영상 파일을 찾을 수 없습니다:\n{job.video}")
                return None
            jobs.append(job)
        ids = [j.session_id for j in jobs]
        dups = sorted({s for s in ids if ids.count(s) > 1})
        if dups:
            QMessageBox.warning(self, "세션 중복", "같은 교차로·세션이 두 번 이상 있습니다:\n" + "\n".join(dups))
            return None
        return jobs

    def on_export_colab(self) -> None:
        jobs = self._collect_jobs()
        if jobs is None:
            return
        root = Path.cwd().resolve()
        outside = [j.video for j in jobs if not is_colab_path(to_colab_path(j.video.resolve(), root))]
        if outside:
            QMessageBox.warning(self, "Drive 밖의 영상", "Colab 은 Google Drive 안의 파일만 열 수 있습니다. "
                                "아래 영상을 Drive 로 옮긴 뒤 다시 내보내세요:\n" + "\n".join(str(p) for p in outside))
            return

        def model_path(value: str) -> str:
            path = Path(value)
            return to_colab_path(path.resolve(), root) if path.is_absolute() else path.as_posix()

        items = jobs_to_dicts(jobs, lambda p: to_colab_path(p.resolve(), root), model_path)
        out_dir = root / "colab" / "batch_jobs"
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"batch_{datetime.now():%Y%m%d_%H%M%S}.json"
        out.write_text(json.dumps({"created": datetime.now().isoformat(timespec="seconds"), "jobs": items},
                                  ensure_ascii=False, indent=1), encoding="utf-8")
        line = f"JOBS_FILE = 'colab/batch_jobs/{out.name}'"
        QGuiApplication.clipboard().setText(line)
        self.log_view.appendPlainText(f"[colab] 목록 저장: {out}")
        QMessageBox.information(
            self, "Colab용 내보내기",
            f"영상 {len(jobs)}개 목록을 저장했습니다:\n{out}\n\n"
            f"Colab 'colab/일괄탐지.ipynb' 설정 셀의 JOBS_FILE 을 아래처럼 바꾸세요(클립보드에 복사됨):\n{line}\n\n"
            "Drive 동기화가 끝난 뒤 Colab 에서 실행하세요.")

    def on_start(self) -> None:
        if self.is_busy_fn():
            QMessageBox.information(self, "실행 중", "메인 창에서 탐지가 실행 중입니다. 끝난 뒤 시작하세요.")
            return
        jobs = self._collect_jobs()
        if jobs is None:
            return

        states = [session_state(db_path_for(j.junction), j.session_id) for j in jobs]
        existing = "skip"
        n_existing = states.count("existing")
        if n_existing:
            names = "\n".join(f"  {j.session_id}" for j, s in zip(jobs, states) if s == "existing")
            box = QMessageBox(self)
            box.setWindowTitle("기존 세션 있음")
            box.setText(f"이미 DB에 있는 세션이 {n_existing}개 있습니다(일괄 탐지 완료 기록 없음):\n{names}\n\n어떻게 할까요?")
            skip_btn = box.addButton("건너뛰기", QMessageBox.ButtonRole.AcceptRole)
            overwrite_btn = box.addButton("다시 처리해 덮어쓰기", QMessageBox.ButtonRole.DestructiveRole)
            box.addButton("취소", QMessageBox.ButtonRole.RejectRole)
            box.exec()
            if box.clickedButton() is overwrite_btn:
                existing = "overwrite"
            elif box.clickedButton() is not skip_btn:
                return

        self.frame_totals = {i: _frame_count(j.video) for i, j in enumerate(jobs)}
        self._logged_pct = {}
        self.log_view.clear()
        base = dict(self.base_overrides_fn())
        self.log_view.appendPlainText(f"[batch] 영상 {len(jobs)}개 시작 (기본 모델: {base.get('model_path', '')})")
        self.worker = BatchWorker(jobs, self.cfg_path, base, existing)
        self.worker.log.connect(self._on_log, Qt.ConnectionType.QueuedConnection)
        self.worker.status.connect(self._on_status, Qt.ConnectionType.QueuedConnection)
        self.worker.done.connect(self._on_done, Qt.ConnectionType.QueuedConnection)
        self._set_running(True)
        self.worker.start()

    def is_running(self) -> bool:
        return self.worker is not None and self.worker.isRunning()

    def on_stop_after_current(self) -> None:
        if self.worker is not None:
            self.worker.stop_after_current = True
            self.after_btn.setEnabled(False)
            self.log_view.appendPlainText("[batch] 현재 영상이 끝나면 멈춥니다.")

    def on_stop_now(self) -> None:
        if self.worker is not None:
            self.worker.requestInterruption()
            self.stop_btn.setEnabled(False)
            self.log_view.appendPlainText("[batch] 중지 요청 — 진행 중인 영상의 결과는 저장하지 않습니다.")

    def _on_log(self, row: int, message: str) -> None:
        m = re.match(r"\[progress\] frame (\d+)", message)
        if m and row >= 0:
            total = self.frame_totals.get(row) or 0
            if total:
                pct = min(100, 100 * int(m.group(1)) // total)
                self.table.item(row, COL_STATUS).setText(f"▶ {pct}%")
                # 로그가 멈춘 것처럼 보이지 않게 5% 마다 한 줄 남긴다.
                step = pct // 5 * 5
                if step > self._logged_pct.get(row, -1):
                    self._logged_pct[row] = step
                    self.log_view.appendPlainText(
                        f"[{row + 1}] 진행 {step}% (프레임 {int(m.group(1)):,} / {total:,}) {datetime.now():%H:%M:%S}")
            return
        prefix = f"[{row + 1}] " if row >= 0 else ""
        self.log_view.appendPlainText(prefix + message)

    def _on_status(self, row: int, state: str, detail: str) -> None:
        text = STATUS_TEXT.get(state, state) + (f" {detail}" if detail else "")
        self.table.item(row, COL_STATUS).setText(text)
        self.log_view.appendPlainText(f"[{row + 1}] {self._text(row, COL_VIDEO)}: {text}")

    def _on_done(self, results: list) -> None:
        self._set_running(False)
        counts = {s: sum(r.status == s for r in results) for s in ("done", "skipped", "failed", "stopped")}
        summary = f"완료 {counts['done']} · 건너뜀 {counts['skipped']} · 실패 {counts['failed']}"
        if counts["stopped"] or len(results) < self.table.rowCount():
            summary += " · 중지됨(남은 영상은 다시 시작하면 이어서 처리)"
        self.summary_label.setText(summary)
        self.log_view.appendPlainText(f"[batch] 끝: {summary}")
        self.worker = None

    def closeEvent(self, event) -> None:  # noqa: N802
        if self.is_running():
            answer = QMessageBox.question(self, "실행 중", "일괄 탐지가 진행 중입니다. 중지하고 닫을까요?")
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.worker.requestInterruption()
            self.worker.wait()
        super().closeEvent(event)
