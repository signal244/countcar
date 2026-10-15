"""이미지·라벨 추출 창: 여러 영상에서 학습용 이미지를 뽑고 YOLO 라벨을 만든다.

메인 창 'Extract Images + Labels' 버튼이 연다. 실제 처리는 src/services/label_extraction.py.
"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from src.config.loader import load_app_config, load_json, save_user_state
from src.config.model_profiles import ModelProfileStore, list_model_files
from src.services.batch_detection import parse_video_name
from src.services.label_extraction import ExtractJob, ExtractSettings, StopFlags, run_extraction
from src.ui.class_dialogs import DetectClassDialog
from src.ui.theme import DARK_DIALOG_STYLE
from src.ui.widgets import apply_button_tooltips, fit_to_screen

logger = logging.getLogger(__name__)

COL_VIDEO, COL_JUNCTION, COL_SESSION, COL_PREFIX, COL_STATUS = range(5)
HEADERS = ["영상", "교차로명", "세션명", "파일 이름", "상태"]
COLUMN_WIDTHS = {COL_VIDEO: 280, COL_JUNCTION: 140, COL_SESSION: 140, COL_PREFIX: 240}
VIDEO_FILTER = "영상 (*.mp4 *.avi *.mkv *.mov *.m4v *.ts);;모든 파일 (*)"
STATE_KEY = "extract_settings"
IMGSZ_CHOICES = ["auto", "640", "960", "1280", "1920"]
STATUS_TEXT = {"running": "▶ 진행 중", "done": "✔ 완료", "failed": "✖ 실패", "stopped": "■ 중지"}


def _parse_time(text: str) -> float:
    """'mm:ss', 'hh:mm:ss' 또는 초 숫자 -> 초. 비우면 0."""
    text = (text or "").strip()
    if not text:
        return 0.0
    parts = [float(p) for p in text.split(":")]
    sec = 0.0
    for p in parts:
        sec = sec * 60 + p
    return sec


class ExtractWorker(QThread):
    log = Signal(int, str)
    status = Signal(int, str, str)
    progress = Signal(int, int, int)
    done = Signal(list)

    def __init__(self, jobs: List[ExtractJob], settings: ExtractSettings):
        super().__init__()
        self.jobs = jobs
        self.settings = settings
        self.flags = StopFlags()

    def run(self) -> None:
        try:
            results = run_extraction(self.jobs, self.settings, flags=self.flags, log_cb=self.log.emit,
                                     status_cb=self.status.emit, progress_cb=self.progress.emit)
        except Exception as exc:  # noqa: BLE001
            logger.debug("추출 실패", exc_info=True)
            self.log.emit(-1, f"[error] {exc}")
            results = []
        self.done.emit(results)


class ExtractWindow(QDialog):
    def __init__(self, cfg_defaults: Dict, state_path: Path, main_model_fn: Callable[[], str],
                 default_conf: float = 0.25, profile_store: Optional[ModelProfileStore] = None,
                 is_busy_fn: Callable[[], bool] = lambda: False, parent=None):
        super().__init__(parent)
        self.setWindowTitle("이미지·라벨 추출 (학습 데이터 만들기)")
        self.setStyleSheet(DARK_DIALOG_STYLE)
        self.cfg_defaults = cfg_defaults
        self.state_path = Path(state_path)
        self.main_model_fn = main_model_fn
        self.default_conf = default_conf
        self.profile_store = profile_store or ModelProfileStore(Path("config/model_profiles.json"))
        self.is_busy_fn = is_busy_fn
        self.worker: Optional[ExtractWorker] = None
        self._class_ids: Optional[List[int]] = None
        self._logged_pct: Dict[int, int] = {}
        self._build_ui()
        self._restore_settings()
        fit_to_screen(self, 1250, 760)

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "영상들에서 정한 간격으로 이미지를 저장하고, 고른 모델로 YOLO 라벨(txt)을 만듭니다. 위에서부터 차례로 처리합니다.\n"
            "저장 폴더 아래 images/ 와 labels/ 에 <교차로명>_<세션명>_번호 이름으로 저장하고, 다시 뽑으면 다음 번호부터 이어 붙입니다."))

        tools = QHBoxLayout()
        self.add_btn = QPushButton("영상 추가…")
        self.add_btn.clicked.connect(self.on_add)
        self.remove_btn = QPushButton("선택 제거")
        self.remove_btn.clicked.connect(self.on_remove)
        self.up_btn = QPushButton("▲ 위로")
        self.up_btn.clicked.connect(lambda: self.on_move(-1))
        self.down_btn = QPushButton("▼ 아래로")
        self.down_btn.clicked.connect(lambda: self.on_move(1))
        for b in (self.add_btn, self.remove_btn, self.up_btn, self.down_btn):
            tools.addWidget(b)
        tools.addStretch()
        layout.addLayout(tools)

        self.table = QTableWidget(0, len(HEADERS))
        self.table.setHorizontalHeaderLabels(HEADERS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)
        for col, width in COLUMN_WIDTHS.items():
            self.table.setColumnWidth(col, width)
        self.table.itemChanged.connect(self._on_item_changed)
        layout.addWidget(self.table, stretch=3)

        box = QGroupBox("추출 설정")
        grid = QGridLayout(box)
        self.out_input = QLineEdit()
        out_btn = QPushButton("폴더 선택")
        out_btn.clicked.connect(self._choose_out_dir)
        grid.addWidget(QLabel("저장 폴더"), 0, 0)
        grid.addWidget(self.out_input, 0, 1, 1, 4)
        grid.addWidget(out_btn, 0, 5)

        self.model_combo = QComboBox()
        self.model_combo.addItem("(메인 창 모델)", "")
        for path in list_model_files("models"):
            self.model_combo.addItem(path.name, str(path))
        self.model_combo.currentIndexChanged.connect(self._on_model_changed)
        self.classes_btn = QPushButton("")
        self.classes_btn.clicked.connect(self._on_pick_classes)
        grid.addWidget(QLabel("라벨링 모델"), 1, 0)
        grid.addWidget(self.model_combo, 1, 1, 1, 2)
        grid.addWidget(QLabel("라벨링 차종"), 1, 3)
        grid.addWidget(self.classes_btn, 1, 4, 1, 2)

        self.runtime_combo = QComboBox()
        for label, value in (("자동", "auto"), ("GPU", "gpu"), ("CPU", "cpu")):
            self.runtime_combo.addItem(label, value)
        self.runtime_combo.setToolTip("자동: NVIDIA GPU(메모리 4GB 이상)가 있으면 GPU, 없으면 CPU 로 라벨링합니다.")
        self.imgsz_combo = QComboBox()
        self.imgsz_combo.addItems(IMGSZ_CHOICES)
        self.imgsz_combo.setToolTip("auto: 모델을 학습한 크기. 크게 할수록 작은 차량을 잘 잡지만 느려집니다.")
        self.conf_spin = QDoubleSpinBox()
        self.conf_spin.setDecimals(2)
        self.conf_spin.setRange(0.05, 1.0)
        self.conf_spin.setSingleStep(0.05)
        grid.addWidget(QLabel("실행 모드"), 2, 0)
        grid.addWidget(self.runtime_combo, 2, 1)
        grid.addWidget(QLabel("이미지 크기"), 2, 2)
        grid.addWidget(self.imgsz_combo, 2, 3)
        grid.addWidget(QLabel("신뢰도"), 2, 4)
        grid.addWidget(self.conf_spin, 2, 5)

        self.every_spin = QDoubleSpinBox()
        self.every_spin.setDecimals(1)
        self.every_spin.setRange(0.0, 600.0)
        self.every_spin.setSingleStep(0.5)
        self.every_spin.setSuffix(" 초마다 1장")
        self.every_spin.setToolTip("0 이면 모든 프레임을 저장합니다(매우 많음).")
        self.start_input = QLineEdit()
        self.start_input.setPlaceholderText("처음부터 (예: 5:00)")
        self.end_input = QLineEdit()
        self.end_input.setPlaceholderText("끝까지 (예: 1:30:00)")
        for w in (self.start_input, self.end_input):
            w.setToolTip("분:초 또는 시:분:초. 비우면 처음부터/끝까지. 예: 촬영 초반 카메라를 맞추는 구간을 건너뛸 때 시작을 5:00 으로.")
        self.max_spin = QSpinBox()
        self.max_spin.setRange(0, 1_000_000)
        self.max_spin.setSpecialValueText("제한 없음")
        self.max_spin.setToolTip("영상 하나에서 이 장수를 넘으면 그 영상은 멈추고 다음 영상으로 넘어갑니다.")
        grid.addWidget(QLabel("추출 간격"), 3, 0)
        grid.addWidget(self.every_spin, 3, 1)
        grid.addWidget(QLabel("구간"), 3, 2)
        span = QHBoxLayout()
        span.addWidget(self.start_input)
        span.addWidget(QLabel("~"))
        span.addWidget(self.end_input)
        grid.addLayout(span, 3, 3)
        grid.addWidget(QLabel("영상당 최대"), 3, 4)
        grid.addWidget(self.max_spin, 3, 5)

        self.scheme_combo = QComboBox()
        self.scheme_combo.addItem("학습 데이터 기준 8클래스 (기존 데이터와 호환)", "dataset")
        self.scheme_combo.addItem("모델 클래스 번호 그대로", "model")
        self.scheme_combo.setToolTip(
            "학습 데이터 기준: 승용차=2, 대형화물=4 처럼 기존 학습 데이터 번호로 바꿉니다. 바꿀 수 없는 클래스는 뺍니다.\n"
            "모델 번호 그대로: 라벨링 모델의 번호를 그대로 씁니다(그 모델 구성으로 다시 학습할 때).")
        self.images_only_check = QCheckBox("이미지만 추출 (라벨 안 만듦)")
        self.images_only_check.toggled.connect(self._sync_enabled)
        grid.addWidget(QLabel("라벨 번호"), 4, 0)
        grid.addWidget(self.scheme_combo, 4, 1, 1, 3)
        grid.addWidget(self.images_only_check, 4, 4, 1, 2)
        for col in (1, 3, 5):
            grid.setColumnStretch(col, 1)
        layout.addWidget(box)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(3000)
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
            "▶ 시작": "표의 영상을 위에서부터 차례로 이미지 추출·라벨링합니다.",
            "현재 영상 끝나면 멈춤": "진행 중인 영상은 끝까지 처리하고, 다음 영상부터는 시작하지 않습니다.",
            "즉시 중지": "진행 중인 영상을 멈춥니다. 이미 저장한 이미지·라벨은 남습니다.",
        })
        self._set_running(False)

    def _set_running(self, running: bool) -> None:
        for w in (self.add_btn, self.remove_btn, self.up_btn, self.down_btn, self.start_btn,
                  self.model_combo, self.classes_btn, self.runtime_combo, self.imgsz_combo, self.conf_spin,
                  self.every_spin, self.start_input, self.end_input, self.max_spin, self.scheme_combo,
                  self.images_only_check, self.out_input):
            w.setEnabled(not running)
        self.after_btn.setEnabled(running)
        self.stop_btn.setEnabled(running)
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers if running
            else QAbstractItemView.EditTrigger.DoubleClicked | QAbstractItemView.EditTrigger.EditKeyPressed)
        if not running:
            self._sync_enabled()

    def _sync_enabled(self) -> None:
        labeling = not self.images_only_check.isChecked()
        for w in (self.model_combo, self.classes_btn, self.runtime_combo, self.imgsz_combo, self.conf_spin,
                  self.scheme_combo):
            w.setEnabled(labeling and not self.is_running())

    # ------------------------------------------------------------------ 설정 저장/복원
    def _settings_dict(self) -> Dict:
        return {
            "out_dir": self.out_input.text().strip(),
            "model_path": self.model_combo.currentData() or "",
            "class_ids": self._class_ids,
            "runtime_mode": self.runtime_combo.currentData(),
            "imgsz": self.imgsz_combo.currentText(),
            "conf": float(self.conf_spin.value()),
            "every_sec": float(self.every_spin.value()),
            "start": self.start_input.text().strip(),
            "end": self.end_input.text().strip(),
            "max_images": int(self.max_spin.value()),
            "id_scheme": self.scheme_combo.currentData(),
            "images_only": self.images_only_check.isChecked(),
        }

    def _restore_settings(self) -> None:
        saved: Dict = {}
        try:
            if self.state_path.exists():
                saved = dict(load_app_config(self.state_path).get(STATE_KEY) or {})
        except Exception:
            logger.debug("추출 설정 읽기 실패", exc_info=True)
        self.out_input.setText(saved.get("out_dir") or str(Path("output/extract").resolve()))
        idx = self.model_combo.findData(saved.get("model_path") or "")
        self.model_combo.setCurrentIndex(max(0, idx))
        self._class_ids = saved.get("class_ids") or None
        self.runtime_combo.setCurrentIndex(max(0, self.runtime_combo.findData(saved.get("runtime_mode") or "auto")))
        self.imgsz_combo.setCurrentText(str(saved.get("imgsz") or "auto"))
        self.conf_spin.setValue(float(saved.get("conf") or self.default_conf))
        self.every_spin.setValue(float(saved.get("every_sec", 2.0)))
        self.start_input.setText(str(saved.get("start") or ""))
        self.end_input.setText(str(saved.get("end") or ""))
        self.max_spin.setValue(int(saved.get("max_images") or 0))
        self.scheme_combo.setCurrentIndex(max(0, self.scheme_combo.findData(saved.get("id_scheme") or "dataset")))
        self.images_only_check.setChecked(bool(saved.get("images_only")))
        self._update_classes_label()
        self._sync_enabled()

    def _save_settings(self) -> None:
        try:
            state = load_app_config(self.state_path) if self.state_path.exists() else {}
            state[STATE_KEY] = self._settings_dict()
            save_user_state(self.state_path, state)
        except Exception:
            logger.debug("추출 설정 저장 실패", exc_info=True)

    # ------------------------------------------------------------------ 모델·차종
    def _model_path(self) -> str:
        model = self.model_combo.currentData() or ""
        if model:
            return str(model)
        try:
            return str(self.main_model_fn() or "")
        except Exception:
            logger.debug("메인 창 모델 읽기 실패", exc_info=True)
            return ""

    def _effective_class_ids(self) -> Optional[List[int]]:
        if self._class_ids:
            return [int(c) for c in self._class_ids]
        model = self._model_path()
        profile = self.profile_store.get_cached(model) if model else None
        if profile is not None and profile.detect_class_ids:
            return list(profile.detect_class_ids)
        return None

    def _update_classes_label(self) -> None:
        model = self._model_path()
        profile = self.profile_store.get_cached(model) if model else None
        ids = self._effective_class_ids()
        if ids and profile and profile.classes:
            names = [str(profile.classes.get(int(c), c)) for c in ids]
            text = f"{len(names)}종: " + ", ".join(names)
        elif ids:
            text = f"{len(ids)}종 선택"
        else:
            text = "사람 빼고 전부"
        self.classes_btn.setText(text if len(text) <= 40 else text[:39] + "…")
        self.classes_btn.setToolTip(f"모델: {Path(model).name if model else '-'}\n{text}\n누르면 라벨을 만들 차종을 고릅니다.")

    def _on_model_changed(self, _index: int) -> None:
        self._class_ids = None   # 차종 번호는 모델마다 다르다
        self._update_classes_label()

    def _on_pick_classes(self) -> None:
        model = self._model_path()
        if not model:
            QMessageBox.warning(self, "모델 없음", "라벨링 모델을 먼저 고르세요.")
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            profile = self.profile_store.get(model)
        finally:
            QApplication.restoreOverrideCursor()
        if not profile.has_classes:
            QMessageBox.warning(self, "클래스 없음", f"모델에서 클래스를 읽지 못했습니다:\n{model}")
            return
        if self._class_ids:
            profile.detect_class_ids = list(self._class_ids)
        dialog = DetectClassDialog(profile, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self._class_ids = dialog.selected_class_ids()
        self._update_classes_label()

    # ------------------------------------------------------------------ 표 조작
    def add_video(self, video: Path) -> None:
        junction, session = parse_video_name(video)
        row = self.table.rowCount()
        self.table.blockSignals(True)
        self.table.insertRow(row)
        item = QTableWidgetItem(video.name)
        item.setData(Qt.ItemDataRole.UserRole, str(video))
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        item.setToolTip(str(video))
        self.table.setItem(row, COL_VIDEO, item)
        self.table.setItem(row, COL_JUNCTION, QTableWidgetItem(junction))
        self.table.setItem(row, COL_SESSION, QTableWidgetItem(session))
        for col in (COL_PREFIX, COL_STATUS):
            cell = QTableWidgetItem("")
            cell.setFlags(cell.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(row, col, cell)
        self.table.blockSignals(False)
        self._refresh_row(row)

    def _text(self, row: int, col: int) -> str:
        item = self.table.item(row, col)
        return item.text().strip() if item else ""

    def _job(self, row: int) -> ExtractJob:
        return ExtractJob(Path(self.table.item(row, COL_VIDEO).data(Qt.ItemDataRole.UserRole)),
                          self._text(row, COL_JUNCTION), self._text(row, COL_SESSION))

    def _refresh_row(self, row: int) -> None:
        self.table.blockSignals(True)
        self.table.item(row, COL_PREFIX).setText(f"{self._job(row).prefix}_NNNNNN.jpg")
        self.table.blockSignals(False)

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if item.column() in (COL_JUNCTION, COL_SESSION):
            self._refresh_row(item.row())

    def on_add(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "이미지를 뽑을 영상 선택", "", VIDEO_FILTER)
        existing = {self.table.item(r, COL_VIDEO).data(Qt.ItemDataRole.UserRole) for r in range(self.table.rowCount())}
        for p in paths:
            if str(Path(p)) not in existing:
                self.add_video(Path(p))

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
        snaps = [(self._job(r).video, self._text(r, COL_JUNCTION), self._text(r, COL_SESSION))
                 for r in range(self.table.rowCount())]
        snaps[row], snaps[target] = snaps[target], snaps[row]
        self.table.setRowCount(0)
        for video, junction, session in snaps:
            self.add_video(video)
            r = self.table.rowCount() - 1
            self.table.blockSignals(True)
            self.table.item(r, COL_JUNCTION).setText(junction)
            self.table.item(r, COL_SESSION).setText(session)
            self.table.blockSignals(False)
            self._refresh_row(r)
        self.table.selectRow(target)

    def _choose_out_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "이미지·라벨 저장 폴더", self.out_input.text().strip() or ".")
        if path:
            self.out_input.setText(path)

    # ------------------------------------------------------------------ 실행
    def _collect(self) -> Optional[tuple]:
        if self.table.rowCount() == 0:
            QMessageBox.information(self, "영상 없음", "먼저 영상을 추가하세요.")
            return None
        jobs = [self._job(r) for r in range(self.table.rowCount())]
        missing = [j.video.name for j in jobs if not j.video.is_file()]
        if missing:
            QMessageBox.warning(self, "영상 없음", "영상 파일을 찾을 수 없습니다:\n" + "\n".join(missing))
            return None
        out_dir = self.out_input.text().strip()
        if not out_dir:
            QMessageBox.warning(self, "저장 폴더", "저장 폴더를 고르세요.")
            return None
        try:
            start, end = _parse_time(self.start_input.text()), _parse_time(self.end_input.text())
        except ValueError:
            QMessageBox.warning(self, "구간", "구간은 분:초 또는 시:분:초로 입력하세요. 예: 5:00, 1:30:00")
            return None
        if end and end <= start:
            QMessageBox.warning(self, "구간", "끝 시간이 시작 시간보다 뒤여야 합니다.")
            return None
        images_only = self.images_only_check.isChecked()
        model = self._model_path()
        if not images_only and not model:
            QMessageBox.warning(self, "모델 없음", "라벨링 모델을 고르세요.")
            return None
        mapping: Dict[str, str] = {}
        mapping_path = Path(str(self.cfg_defaults.get("class_mapping_path") or "config/category_mapping.json"))
        try:
            if mapping_path.exists():
                mapping = {str(k).strip().lower(): str(v).strip().lower() for k, v in (load_json(mapping_path) or {}).items()}
        except Exception:
            logger.debug("차종 매핑 읽기 실패: %s", mapping_path, exc_info=True)
        settings = ExtractSettings(
            out_dir=Path(out_dir), model_path=model, every_sec=float(self.every_spin.value()),
            start_sec=start, end_sec=end, max_images=int(self.max_spin.value()),
            imgsz=self.imgsz_combo.currentText(), conf=float(self.conf_spin.value()),
            class_ids=self._effective_class_ids(), id_scheme=str(self.scheme_combo.currentData()),
            images_only=images_only, runtime_mode=str(self.runtime_combo.currentData()),
            device_cfg=str(self.cfg_defaults.get("device") or "auto"), class_mapping=mapping)
        return jobs, settings

    def on_start(self) -> None:
        if self.is_running():
            return
        if self.is_busy_fn():
            QMessageBox.information(self, "실행 중", "탐지가 실행 중입니다. 끝난 뒤 시작하세요.")
            return
        collected = self._collect()
        if collected is None:
            return
        jobs, settings = collected
        self._save_settings()
        for r in range(self.table.rowCount()):
            self.table.item(r, COL_STATUS).setText("대기")
        self._logged_pct = {}
        self.summary_label.setText("")
        self.log_view.appendPlainText(
            f"[extract] 영상 {len(jobs)}개 시작 → {settings.out_dir} ({datetime.now():%H:%M:%S})")
        self.worker = ExtractWorker(jobs, settings)
        self.worker.log.connect(self._on_log, Qt.ConnectionType.QueuedConnection)
        self.worker.status.connect(self._on_status, Qt.ConnectionType.QueuedConnection)
        self.worker.progress.connect(self._on_progress, Qt.ConnectionType.QueuedConnection)
        self.worker.done.connect(self._on_done, Qt.ConnectionType.QueuedConnection)
        self._set_running(True)
        self.worker.start()

    def is_running(self) -> bool:
        return self.worker is not None and self.worker.isRunning()

    def on_stop_after_current(self) -> None:
        if self.worker is not None:
            self.worker.flags.after_current = True
            self.after_btn.setEnabled(False)
            self.log_view.appendPlainText("[extract] 현재 영상이 끝나면 멈춥니다.")

    def on_stop_now(self) -> None:
        if self.worker is not None:
            self.worker.flags.now = True
            self.stop_btn.setEnabled(False)
            self.log_view.appendPlainText("[extract] 중지 요청 — 이미 저장한 이미지·라벨은 남습니다.")

    def _on_log(self, row: int, message: str) -> None:
        prefix = f"[{row + 1}] " if row >= 0 else ""
        self.log_view.appendPlainText(prefix + message)

    def _on_progress(self, row: int, done: int, planned: int) -> None:
        if planned <= 0 or row < 0:
            return
        pct = min(100, 100 * done // planned)
        self.table.item(row, COL_STATUS).setText(f"▶ {pct}% ({done:,}/{planned:,})")
        step = pct // 5 * 5
        if step > self._logged_pct.get(row, -1):
            self._logged_pct[row] = step
            self.log_view.appendPlainText(f"[{row + 1}] 진행 {step}% ({done:,}/{planned:,}장) {datetime.now():%H:%M:%S}")

    def _on_status(self, row: int, state: str, detail: str) -> None:
        text = STATUS_TEXT.get(state, state) + (f" {detail}" if detail else "")
        self.table.item(row, COL_STATUS).setText(text)

    def _on_done(self, results: list) -> None:
        self._set_running(False)
        images = sum(r.images for r in results)
        labeled = sum(r.labeled for r in results)
        failed = sum(r.status == "failed" for r in results)
        summary = f"이미지 {images:,}장 · 라벨 {labeled:,}장 · 실패 {failed}"
        if len(results) < self.table.rowCount() or any(r.status == "stopped" for r in results):
            summary += " · 중지됨"
        self.summary_label.setText(summary)
        self.log_view.appendPlainText(f"[extract] 끝: {summary} ({datetime.now():%H:%M:%S})")
        self.worker = None

    def closeEvent(self, event) -> None:  # noqa: N802
        if self.is_running():
            answer = QMessageBox.question(self, "실행 중", "추출이 진행 중입니다. 중지하고 닫을까요?")
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.worker.flags.now = True
            self.worker.wait()
        self._save_settings()
        super().closeEvent(event)
