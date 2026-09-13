import json
import logging
import re
import sqlite3
import sys
import traceback
import shutil
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, Tuple

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QGraphicsOpacityEffect,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QInputDialog,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from src.config.loader import load_app_config, load_app_config_with_state, load_json, save_user_state
from src.config.model_profiles import ModelProfileStore
from src.config.model_resolver import can_auto_download_model, ensure_model_source, resolve_model_source
from src.db.queries import distinct_vehicle_types
from src.services.colab_export import (
    build_colab_cell,
    build_colab_config,
    is_colab_path,
    to_colab_path,
    unconvertible_paths,
)
from src.ui.class_dialogs import CountClassMappingDialog, DetectClassDialog
from src.ui.line_drawer import LineDrawerWindow
from src.ui.model_selector import ModelComboBox
from src.ui.theme import DARK_DIALOG_STYLE, MAIN_WINDOW_STYLE
from src.ui.vehicle_preview import VehiclePreviewWindow
from src.ui.widgets import StatusBar, WorkflowStepper, section_divider
from src.ui.video_selector import select_video
from src.ui.workers import CountWorker, PipelineWorker
from src.ui.trajectory_viewer2 import TrajectoryViewer2Window


logger = logging.getLogger(__name__)

UI_SCALE = 1.35


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("차량 교통량 카운팅(v6.0)")
        # Keep the initial window wide enough for long path inputs.
        self.resize(int(1050 * UI_SCALE), int(700 * UI_SCALE))

        self.cfg_path = Path("config/app_config.json")
        self.state_path = Path("config/user_state.json")
        self.video_path: Optional[Path] = None
        self.worker: Optional[PipelineWorker] = None
        self._pipeline_start_ts: Optional[datetime] = None
        self._editors = []
        self._viewers = []

        self.cfg_defaults = load_app_config_with_state(self.cfg_path, self.state_path)
        self.profile_store = ModelProfileStore(Path("config/model_profiles.json"))
        self.line_settings_path = self._resolve_initial_line_settings_path()
        self._build_ui()
        self._apply_style()
        self._update_detect_classes_label()

    def _resolve_initial_line_settings_path(self) -> Path:
        candidates = [
            self.cfg_defaults.get("line_settings_path"),
            self.cfg_defaults.get("count_lines_path"),
            "config/lines/line_settings.sample.json",
        ]
        for candidate in candidates:
            if not candidate:
                continue
            path = Path(str(candidate)).expanduser()
            if path.exists():
                return path
        return Path("config/lines/line_settings.sample.json")

    def _clear_finished_worker(self) -> None:
        worker = self.worker
        if worker is not None and not worker.isRunning():
            self.worker = None

    def _stop_worker(self, wait_ms: int = 5000) -> bool:
        worker = self.worker
        if worker is None:
            return True
        try:
            if worker.isRunning():
                worker.requestInterruption()
                worker.wait(int(wait_ms))
        except Exception:
            logger.debug("Suppressed error", exc_info=True)
        if worker.isRunning():
            return False
        if self.worker is worker:
            self.worker = None
        return True

    def _close_child_windows(self) -> bool:
        app = QApplication.instance()
        for collection_name in ("_viewers", "_editors"):
            kept = []
            for w in list(getattr(self, collection_name, []) or []):
                try:
                    if w is None:
                        continue
                    if hasattr(w, "isVisible") and not w.isVisible():
                        continue
                    closed = w.close()
                    if app is not None:
                        app.processEvents()
                    if closed is False:
                        kept.append(w)
                        continue
                    if hasattr(w, "isVisible") and w.isVisible():
                        kept.append(w)
                except Exception:
                    kept.append(w)
            setattr(self, collection_name, kept)
        return not self._viewers and not self._editors

    def closeEvent(self, event) -> None:
        # 실행 중인 스레드가 있으면 먼저 정리해서 종료 시 충돌을 막는다.
        try:
            if self.worker and self.worker.isRunning():
                res = QMessageBox.question(
                    self,
                    "종료 확인",
                    "감지/추적(DB 저장)이 아직 실행 중입니다.\n중단하고 종료할까요?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if res != QMessageBox.StandardButton.Yes:
                    event.ignore()
                    return
        except Exception:
            logger.debug("Suppressed error", exc_info=True)

        if not self._close_child_windows():
            QMessageBox.warning(self, "종료 지연", "하위 창의 백그라운드 작업이 아직 종료되지 않았습니다. 잠시 후 다시 시도하세요.")
            event.ignore()
            return

        try:
            self._save_config()
        except Exception:
            logger.debug("Suppressed error", exc_info=True)
        if not self._stop_worker():
            QMessageBox.warning(self, "종료 지연", "백그라운드 작업이 아직 종료되지 않았습니다. 잠시 후 다시 시도하세요.")
            event.ignore()
            return
        super().closeEvent(event)

    def _cap(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setProperty("role", "caption")
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        return lbl

    def _append_log(self, msg: str) -> None:
        try:
            self.log_view.append(str(msg))
        except Exception:
            logger.debug("Suppressed error", exc_info=True)

    def _on_pipeline_done(self) -> None:
        self._append_log("파이프라인 완료.")
        if self._pipeline_start_ts is not None:
            end_ts = datetime.now()
            elapsed = end_ts - self._pipeline_start_ts
            self._append_log(f"[time] 시작: {self._pipeline_start_ts.strftime('%Y-%m-%d %H:%M:%S')}")
            self._append_log(f"[time] 종료: {end_ts.strftime('%Y-%m-%d %H:%M:%S')}")
            self._append_log(f"[time] 소요: {elapsed}")
            self._pipeline_start_ts = None

    def _build_ui(self) -> None:
        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(18, 18, 18, 18)
        main_layout.setSpacing(12)

        main_layout.addWidget(self._header())
        main_layout.addWidget(self._session_video_box())
        main_layout.addWidget(self._settings_box())
        main_layout.addWidget(self._count_box())
        # Initialize suggested DB paths once related widgets are available.
        try:
            self._maybe_update_count_db_path_from_junction()
        except Exception:
            logger.debug("Suppressed error", exc_info=True)
        main_layout.addLayout(self._buttons_row())
        log_box = self._log_box()
        main_layout.addWidget(log_box, stretch=1)

        container = QWidget()
        container.setLayout(main_layout)
        self.setCentralWidget(container)

    def _header(self) -> QWidget:
        self.stepper = WorkflowStepper(
            ["설정", "Colab", "궤적 DB", "카운팅", "결과"],
        )
        return self.stepper

    def _session_video_box(self) -> QGroupBox:
        box = QGroupBox("📍 교차로 · 영상")
        box.setObjectName("groupTeal")
        layout = QGridLayout()
        layout.setVerticalSpacing(8)
        layout.setHorizontalSpacing(10)

        # Recover junction/session defaults from legacy session_id when needed.
        junction_default = str(self.cfg_defaults.get("junction_name") or "").strip()
        session_name_default = str(self.cfg_defaults.get("session_name") or "").strip()
        session_id_default = str(self.cfg_defaults.get("session_id") or "").strip()
        if (not junction_default or not session_name_default) and session_id_default:
            parts = session_id_default.split()
            if not junction_default and len(parts) >= 2:
                junction_default = parts[0]
            if not session_name_default:
                session_name_default = " ".join(parts[1:]) if len(parts) >= 2 else session_id_default

        self.junction_input = QLineEdit(junction_default)
        self.junction_input.setPlaceholderText("예: 시네파크남측")
        self.session_input = QLineEdit(session_name_default)
        self.session_input.setPlaceholderText("예: 토욜전반")

        video_edit = QLineEdit()
        browse_btn = QPushButton("찾기")
        browse_btn.setFixedWidth(60)

        def choose() -> None:
            path = select_video(self)
            if path:
                video_edit.setText(str(path))
                self.video_path = path
                stem = Path(path).stem
                parts = stem.split()
                if hasattr(self, "junction_input") and len(parts) >= 2:
                    self.junction_input.setText(parts[0])
                if hasattr(self, "session_input"):
                    self.session_input.setText(" ".join(parts[1:]) if len(parts) >= 2 else stem)

        browse_btn.clicked.connect(choose)

        layout.addWidget(self._cap("교차로명"), 0, 0)
        layout.addWidget(self.junction_input, 0, 1)
        layout.addWidget(self._cap("세션명"), 0, 2)
        layout.addWidget(self.session_input, 0, 3)
        layout.addWidget(self._cap("영상"), 0, 4)
        layout.addWidget(video_edit, 0, 5)
        layout.addWidget(browse_btn, 0, 6)
        # Give long text fields more width than labels and buttons.
        layout.setColumnStretch(1, 2)
        layout.setColumnStretch(3, 4)
        layout.setColumnStretch(5, 6)

        box.setLayout(layout)
        try:
            self.junction_input.textChanged.connect(self._maybe_update_count_db_path_from_junction)
            self.junction_input.textChanged.connect(self._maybe_update_detect_db_path_from_junction)
        except Exception:
            logger.debug("Suppressed error", exc_info=True)
        self._refresh_db_sessions()
        return box

    def _compose_session_id(self) -> str:
        junction = self.junction_input.text().strip() if hasattr(self, "junction_input") else ""
        session_name = self.session_input.text().strip() if hasattr(self, "session_input") else ""
        if junction and session_name:
            return f"{junction} {session_name}"
        return junction or session_name

    def _safe_filename(self, name: str) -> str:
        bad = '<>:"/\\\\|?*'
        cleaned = "".join("_" if c in bad else c for c in (name or "").strip())
        cleaned = cleaned.strip().strip(".")
        return cleaned or "counts"

    def _build_count_filename_prefix(self, session_sel: Optional[str] = None) -> str:
        junction_raw = self.junction_input.text().strip() if hasattr(self, "junction_input") else ""
        session_raw = self.session_input.text().strip() if hasattr(self, "session_input") else ""

        if not session_raw and session_sel:
            selected = session_sel.strip()
            if junction_raw and selected.startswith(f"{junction_raw} "):
                session_raw = selected[len(junction_raw) :].strip()
            else:
                session_raw = selected

        junction_safe = self._safe_filename(junction_raw) if junction_raw else ""
        session_safe = self._safe_filename(session_raw) if session_raw else ""
        junction_safe = junction_safe.replace(" ", "_")
        session_safe = session_safe.replace(" ", "_")

        if junction_safe and session_safe:
            return f"{junction_safe}_{session_safe}"
        return junction_safe or session_safe or "counts"

    def _suggest_db_path_for_junction(self, junction_name: str) -> Path:
        safe = self._safe_filename(junction_name)
        return Path("output/db_snapshots") / f"tracks_{safe}.sqlite"

    # 별칭: 감지/카운트 양쪽에서 동일 경로 사용
    _suggest_detect_db_path_for_junction = _suggest_db_path_for_junction

    def _maybe_update_count_db_path_from_junction(self) -> None:
        """Suggest a default count DB path under output/db_snapshots."""
        if not hasattr(self, "count_db_input") or not hasattr(self, "junction_input"):
            return
        junction = self.junction_input.text().strip()
        if not junction:
            return
        current = (self.count_db_input.text() or "").strip()
        suggested = str(self._suggest_db_path_for_junction(junction))

        # Respect a path the user selected manually.
        if getattr(self, "_count_db_user_set", False):
            return

        # Auto-fill only while the field is still empty or on a default path.
        if (not current) or ("output/tracks.sqlite" in current) or ("db_snapshots" in current):
            self.count_db_input.setText(suggested)
            try:
                self._refresh_db_sessions()
            except Exception:
                logger.debug("Suppressed error", exc_info=True)

    def _maybe_update_detect_db_path_from_junction(self) -> None:
        """Suggest a default detection DB path under output/db_snapshots."""
        if not hasattr(self, "db_path_input") or not hasattr(self, "junction_input"):
            return
        junction = self.junction_input.text().strip()
        if not junction:
            return
        current = (self.db_path_input.text() or "").strip()
        suggested = str(self._suggest_detect_db_path_for_junction(junction))
        if getattr(self, "_detect_db_user_set", False):
            return
        if (not current) or ("output/tracks.sqlite" in current) or ("db_snapshots" in current):
            self.db_path_input.setText(suggested)

    def _settings_box(self) -> QGroupBox:
        box = QGroupBox("⚙️ 분석 설정")
        box.setObjectName("groupTeal")
        layout = QGridLayout()
        layout.setVerticalSpacing(8)
        layout.setHorizontalSpacing(12)

        # FPS / Head / Tail
        self.fps_input = QDoubleSpinBox()
        self.fps_input.setDecimals(1)
        self.fps_input.setRange(0.1, 120.0)
        self.fps_input.setValue(float(self.cfg_defaults.get("target_fps", 10.0)))

        self.head_input = QDoubleSpinBox()
        self.head_input.setDecimals(1)
        self.head_input.setRange(0.0, 10.0)
        self.head_input.setValue(float(self.cfg_defaults.get("head_seconds", 1.0)))

        self.tail_input = QDoubleSpinBox()
        self.tail_input.setDecimals(1)
        self.tail_input.setRange(0.0, 10.0)
        self.tail_input.setValue(float(self.cfg_defaults.get("tail_seconds", 1.0)))

        # YOLO version display
        self.yolo_combo = QComboBox()
        self.yolo_combo.addItems(["v8", "v11"])
        self.yolo_combo.setCurrentText(str(self.cfg_defaults.get("yolo_version", "v8")))

        # YOLO imgsz (list)
        self.imgsz_combo = QComboBox()
        for s in ["640", "960", "1280", "1920"]:
            self.imgsz_combo.addItem(s)
        default_imgsz = int(self.cfg_defaults.get("yolo_imgsz") or self.cfg_defaults.get("resize_width") or 640)
        default_imgsz = 1280 if default_imgsz not in (640, 960, 1280, 1920) else default_imgsz
        self.imgsz_combo.setCurrentText(str(default_imgsz))

        # Confidence
        self.conf_input = QDoubleSpinBox()
        self.conf_input.setDecimals(2)
        self.conf_input.setRange(0.05, 1.0)
        self.conf_input.setSingleStep(0.05)
        self.conf_input.setValue(float(self.cfg_defaults.get("confidence_threshold", 0.25)))

        # DB flush interval (minutes)
        self.flush_combo = QComboBox()
        for minutes in ["5", "15", "30", "60"]:
            self.flush_combo.addItem(minutes)
        default_flush = str(int(self.cfg_defaults.get("flush_interval_minutes", 15)))
        if default_flush in [self.flush_combo.itemText(i) for i in range(self.flush_combo.count())]:
            self.flush_combo.setCurrentText(default_flush)
        self.flush_combo.setToolTip("궤적 데이터를 DB에 기록하는 주기 (분 단위)")

        # Track end threshold (frames)
        self.max_idle_frames_spin = QSpinBox()
        self.max_idle_frames_spin.setRange(1, 10_000)
        self.max_idle_frames_spin.setValue(int(self.cfg_defaults.get("max_idle_frames", 30)))
        self.max_idle_frames_spin.setToolTip("이 프레임 수 동안 감지되지 않으면 ID 종료 처리")

        # Model selection: models 폴더를 자동으로 훑어 목록으로 보여준다.
        self.model_combo = ModelComboBox("models")
        self.model_combo.set_path(self.cfg_defaults.get("model_path", ""))
        self.model_combo.currentIndexChanged.connect(self._on_model_selection_changed)
        model_classes_btn = QPushButton("탐지 차종 선택")
        model_classes_btn.setFixedWidth(160)
        model_classes_btn.clicked.connect(self.on_select_detect_classes)
        self.detect_classes_label = QLabel("")
        self.detect_classes_label.setWordWrap(True)

        # DB path for detection/tracking
        self.db_path_input = QLineEdit(self.cfg_defaults.get("db_path", "output/tracks.sqlite"))
        db_path_btn = QPushButton("경로 설정")
        db_path_btn.setFixedWidth(160)
        db_path_btn.clicked.connect(self._choose_detect_db)
        try:
            self.db_path_input.editingFinished.connect(lambda: setattr(self, "_detect_db_user_set", True))
        except Exception:
            logger.debug("Suppressed error", exc_info=True)

        # Tracker type (botsort / bytetrack)
        self.tracker_combo = QComboBox()
        self.tracker_combo.addItems(["botsort", "bytetrack"])
        tracker_cfg = str(self.cfg_defaults.get("tracker_config", "config/botsort_stable.yaml"))
        self.tracker_combo.setCurrentText("botsort" if "botsort" in tracker_cfg.lower() else "bytetrack")

        row = 0
        layout.addWidget(self._cap("FPS"), row, 0)
        layout.addWidget(self.fps_input, row, 1)
        layout.addWidget(self._cap("Head"), row, 2)
        layout.addWidget(self.head_input, row, 3)
        layout.addWidget(self._cap("Tail"), row, 4)
        layout.addWidget(self.tail_input, row, 5)
        layout.addWidget(self._cap("YOLO 버전"), row, 6)
        layout.addWidget(self.yolo_combo, row, 7)

        row += 1
        layout.addWidget(self._cap("표시 크기(imgsz)"), row, 0)
        layout.addWidget(self.imgsz_combo, row, 1)
        layout.addWidget(self._cap("Confidence"), row, 2)
        layout.addWidget(self.conf_input, row, 3)
        layout.addWidget(self._cap("DB 갱신 간격(분)"), row, 4)
        layout.addWidget(self.flush_combo, row, 5)
        layout.addWidget(self._cap("ID 종료(프레임)"), row, 6)
        layout.addWidget(self.max_idle_frames_spin, row, 7)

        row += 1
        layout.addWidget(self._cap("모델"), row, 0)
        layout.addWidget(self.model_combo, row, 1, 1, 4)
        layout.addWidget(model_classes_btn, row, 5)
        layout.addWidget(self._cap("Tracker"), row, 6)
        layout.addWidget(self.tracker_combo, row, 7)

        row += 1
        layout.addWidget(self._cap("탐지 차종"), row, 0)
        layout.addWidget(self.detect_classes_label, row, 1, 1, 7)

        row += 1
        layout.addWidget(self._cap("감지 DB 경로"), row, 0)
        layout.addWidget(self.db_path_input, row, 1, 1, 4)
        layout.addWidget(db_path_btn, row, 5)
        layout.addWidget(QLabel(""), row, 6, 1, 2)

        box.setLayout(layout)
        return box

    def _count_box(self) -> QGroupBox:
        """Build controls for DB-based counting."""
        box = QGroupBox("📊 카운팅 설정")
        box.setObjectName("groupBlue")
        layout = QGridLayout()
        layout.setVerticalSpacing(8)
        layout.setHorizontalSpacing(12)

        # DB / Lines / Output
        self.count_db_input = QLineEdit(self.cfg_defaults.get("count_db_path", "output/tracks.sqlite"))
        db_btn = QPushButton("찾기")
        db_btn.setFixedWidth(90)
        db_btn.clicked.connect(self._choose_count_db)

        # Output dir for Excel results
        self.count_output_dir_input = QLineEdit(self.cfg_defaults.get("count_output_dir", ""))
        out_dir_btn = QPushButton("폴더 선택")
        out_dir_btn.setFixedWidth(120)
        out_dir_btn.clicked.connect(self._choose_count_output_dir)

        self.count_lines_input = QLineEdit(self.cfg_defaults.get("count_lines_path", "config/lines.json"))
        lines_btn = QPushButton("찾기")
        lines_btn.setFixedWidth(90)
        lines_btn.clicked.connect(self._choose_count_lines)

        # Session selector for existing rows in the selected DB.
        self.count_session_combo = QComboBox()
        self.count_session_combo.addItem("(전체)")
        self.count_session_refresh_btn = QPushButton("목록 갱신")
        self.count_session_refresh_btn.setFixedWidth(120)
        self.count_session_refresh_btn.clicked.connect(self._refresh_db_sessions)
        self.count_session_delete_btn = QPushButton("세션 삭제")
        self.count_session_delete_btn.setFixedWidth(120)
        self.count_session_delete_btn.clicked.connect(self._delete_selected_session)
        try:
            self.count_db_input.editingFinished.connect(self._refresh_db_sessions)
            self.count_db_input.editingFinished.connect(lambda: setattr(self, "_count_db_user_set", True))
        except Exception:
            logger.debug("Suppressed error", exc_info=True)

        # Interval / reconnect / extrap
        self.count_interval_combo = QComboBox()
        for m in ["5", "15", "30", "60"]:
            self.count_interval_combo.addItem(m)
        self.count_interval_combo.setFixedWidth(140)
        default_interval = str(int(self.cfg_defaults.get("count_interval_minutes", 15)))
        if default_interval in [self.count_interval_combo.itemText(i) for i in range(self.count_interval_combo.count())]:
            self.count_interval_combo.setCurrentText(default_interval)
        self.count_analysis_basis_combo = QComboBox()
        self.count_analysis_basis_combo.addItem("원본 기준 분석", "original")
        self.count_analysis_basis_combo.addItem("후처리 기준 분석", "postprocess")
        self.count_analysis_basis_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.count_classes_btn = QPushButton("집계 차종 설정")
        self.count_classes_btn.setFixedWidth(150)
        self.count_classes_btn.clicked.connect(self.on_select_count_classes)
        default_basis = str(self.cfg_defaults.get("count_analysis_basis", "original") or "original").strip().lower()
        if default_basis not in ("original", "postprocess"):
            default_basis = "original"
        idx_basis = self.count_analysis_basis_combo.findData(default_basis)
        if idx_basis >= 0:
            self.count_analysis_basis_combo.setCurrentIndex(idx_basis)
        row = 0
        # Row 1: DB path and session controls.
        layout.addWidget(self._cap("DB 경로"), row, 0)
        layout.addWidget(self.count_db_input, row, 1, 1, 3)
        layout.addWidget(db_btn, row, 4)
        layout.addWidget(self._cap("세션"), row, 5)
        layout.addWidget(self.count_session_combo, row, 6)
        layout.addWidget(self.count_session_refresh_btn, row, 7)
        layout.addWidget(self.count_session_delete_btn, row, 8)

        row += 1
        # Row 2: line file and output folder.
        layout.addWidget(self._cap("분석라인"), row, 0)
        layout.addWidget(self.count_lines_input, row, 1, 1, 3)
        layout.addWidget(lines_btn, row, 4)
        layout.addWidget(self._cap("결과 폴더"), row, 5)
        layout.addWidget(self.count_output_dir_input, row, 6, 1, 2)
        layout.addWidget(out_dir_btn, row, 8)

        row += 1
        # Row 3: interval and analysis basis.
        layout.addWidget(self._cap("슬롯(분)"), row, 0)
        layout.addWidget(self.count_interval_combo, row, 1)
        layout.addWidget(self._cap("분석기준"), row, 2)
        layout.addWidget(self.count_analysis_basis_combo, row, 3, 1, 5)
        layout.addWidget(self.count_classes_btn, row, 8)

        row += 1
        # Row 4: spacer.
        layout.addWidget(QLabel(""), row, 6, 1, 3)
        # Stretch input columns more than fixed-size button columns.
        layout.setColumnStretch(1, 2)
        layout.setColumnStretch(2, 1)
        layout.setColumnStretch(3, 8)
        layout.setColumnStretch(6, 10)
        layout.setColumnStretch(7, 10)
        box.setLayout(layout)
        return box

    def _buttons_row(self) -> QVBoxLayout:
        outer = QVBoxLayout()
        outer.setSpacing(8)

        # ── 구분선 ──
        outer.addWidget(section_divider("▼ 실행"))

        # ── Row 1: Colab 코드 생성 / 설정 저장 / 차종 미리보기 ──
        colab_btn = QPushButton("📋 Colab 코드 생성")
        colab_btn.setProperty("btnType", "primary")
        colab_btn.clicked.connect(self.on_export_colab)

        save_btn = QPushButton("💾 설정 저장")
        save_btn.clicked.connect(self._save_config)

        preview_vehicle_btn = QPushButton("🔍 차종 미리보기")
        preview_vehicle_btn.clicked.connect(self.on_preview_vehicle)

        row1 = QHBoxLayout()
        row1.addWidget(colab_btn)
        row1.addWidget(save_btn)
        row1.addWidget(preview_vehicle_btn)
        row1.addStretch()
        outer.addLayout(row1)

        # ── Row 2: 궤적보기/수정 / 교차로 카운팅 / 접근로 카운팅 ──
        traj_btn = QPushButton("👁 궤적보기/수정")
        traj_btn.setProperty("btnType", "secondary")
        traj_btn.clicked.connect(self.on_show_trajectories)

        count_btn = QPushButton("📐 교차로 카운팅")
        count_btn.setProperty("btnType", "primary")
        count_btn.clicked.connect(self.on_count)

        approach_btn = QPushButton("📐 접근로 카운팅")
        approach_btn.setProperty("btnType", "primary")
        approach_btn.clicked.connect(self.on_count_approach)

        row2 = QHBoxLayout()
        row2.addWidget(traj_btn)
        row2.addWidget(count_btn)
        row2.addWidget(approach_btn)
        row2.addStretch()
        outer.addLayout(row2)

        # ── 로컬실행 그룹 (반투명) ──
        local_box = QGroupBox("🖥️ 영상분석 및 궤적저장 (로컬실행)")
        local_box.setObjectName("groupGray")
        opacity = QGraphicsOpacityEffect()
        opacity.setOpacity(0.75)
        local_box.setGraphicsEffect(opacity)

        detect_btn = QPushButton("▶ 로컬 감지/궤적 저장(DB)")
        detect_btn.setProperty("btnType", "secondary")
        detect_btn.clicked.connect(self.on_run)

        extract_btn = QPushButton("🖼 Extract Images + Labels")
        extract_btn.clicked.connect(self.on_extract_images)

        gpu_label = QLabel("⚠️ GPU 필요")
        gpu_label.setStyleSheet("color: #f59e0b; font-weight: 600;")

        local_row = QHBoxLayout()
        local_row.addWidget(detect_btn)
        local_row.addWidget(extract_btn)
        local_row.addWidget(gpu_label)
        local_row.addStretch()
        local_box.setLayout(local_row)
        outer.addWidget(local_box)

        # ── 하단: StatusBar + 종료 ──
        self.status_bar = StatusBar()
        self.status_bar.add_item("model", "모델: –", "off")
        self.status_bar.add_item("db", "DB: –", "off")
        self.status_bar.add_item("gpu", "GPU: –", "off")
        self._update_status_bar()

        quit_btn = QPushButton("종료")
        quit_btn.setProperty("btnType", "danger")
        quit_btn.setFixedWidth(80)
        quit_btn.clicked.connect(self.on_quit_program)

        bottom_row = QHBoxLayout()
        bottom_row.addWidget(self.status_bar, stretch=1)
        bottom_row.addWidget(quit_btn)
        outer.addLayout(bottom_row)

        return outer

    def on_extract_images(self) -> None:
        """
        Extract images from video and generate YOLO label txt files in one run.
        """
        try:
            import cv2  # local import (env may vary)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "OpenCV Missing", f"Failed to import cv2:\n{exc}")
            return

        video = self._get_active_video_path()
        if not video:
            QMessageBox.warning(self, "No Video", "Please select a video file.")
            return
        if not Path(video).exists():
            QMessageBox.warning(self, "No Video", f"Video file not found:\n{video}")
            return

        start_dir_txt = ""
        if hasattr(self, "count_output_dir_input"):
            start_dir_txt = self.count_output_dir_input.text().strip()
        start_dir = Path(start_dir_txt or "output").expanduser().resolve()
        selected_dir = QFileDialog.getExistingDirectory(self, "이미지/라벨 저장 부모 폴더 선택", str(start_dir))
        if not selected_dir:
            return

        extract_root = Path(selected_dir).expanduser()
        try:
            extract_root.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Folder Create Failed", f"Cannot create extract root folder:\n{extract_root}\n{exc}")
            return
        if hasattr(self, "count_output_dir_input"):
            self.count_output_dir_input.setText(str(extract_root))
        images_dir = extract_root / "images"
        images_dir.mkdir(parents=True, exist_ok=True)

        junction_raw = self.junction_input.text().strip() if hasattr(self, "junction_input") else ""
        session_raw = self.session_input.text().strip() if hasattr(self, "session_input") else ""
        junction_safe = self._safe_filename(junction_raw) if junction_raw else ""
        session_safe = self._safe_filename(session_raw) if session_raw else ""
        junction_safe = re.sub(r"\s+", "_", junction_safe)
        session_safe = re.sub(r"\s+", "_", session_safe)
        if junction_safe and session_safe:
            prefix = f"{junction_safe}_{session_safe}"
        else:
            prefix = junction_safe or session_safe or "junction"

        # Popup: extraction mode
        mode_label, ok_mode = QInputDialog.getItem(
            self,
            "Extract Mode",
            "Choose frame extraction mode:",
            ["Sample (Every 2 Sec)", "All Frames", "First N Frames"],
            0,
            False,
        )
        if not ok_mode:
            return

        first_n_limit = 0
        if mode_label == "First N Frames":
            n_value, ok_n = QInputDialog.getInt(
                self,
                "Frame Count",
                "How many frames to extract?",
                300,
                1,
                1_000_000,
                1,
            )
            if not ok_n:
                return
            first_n_limit = int(n_value)

        cap = cv2.VideoCapture(str(video))
        if not cap.isOpened():
            QMessageBox.critical(self, "Video Open Failed", f"Cannot open video:\n{video}")
            return

        src_fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
        if not src_fps or src_fps <= 0:
            src_fps = 30.0

        try:
            target_fps = float(self.fps_input.value()) if hasattr(self, "fps_input") else 10.0
        except Exception:
            target_fps = 10.0
        sample_extract_fps = 0.5

        if mode_label == "All Frames":
            step = 1
        else:
            step = max(1, int(round(src_fps / sample_extract_fps)))

        # Resume numbering if files already exist.
        start_idx = 1
        try:
            pat = re.compile(rf"^{re.escape(prefix)}_(\d{{6}})\.jpg$", re.IGNORECASE)
            max_idx = 0
            for p_img in images_dir.glob(f"{prefix}_*.jpg"):
                m = pat.match(p_img.name)
                if not m:
                    continue
                try:
                    max_idx = max(max_idx, int(m.group(1)))
                except Exception:
                    continue
            if max_idx > 0:
                start_idx = max_idx + 1
        except Exception:
            logger.debug("Suppressed error", exc_info=True)

        def log(msg: str) -> None:
            try:
                if self.log_view:
                    self.log_view.append(str(msg))
                app = QApplication.instance()
                if app is not None:
                    app.processEvents()
            except Exception:
                logger.debug("Suppressed error", exc_info=True)
                return

        log(
            f"[extract:start] mode={mode_label} video={video} src_fps={src_fps:.2f} "
            f"sample_fps={sample_extract_fps:.2f} (2 sec/frame) step={step}"
        )
        if first_n_limit > 0:
            log(f"[extract:limit] first_n={first_n_limit}")
        log(f"[extract:dir] {images_dir}")

        frame_id = 0
        saved = 0
        img_idx = start_idx
        extracted_paths = []
        try:
            while True:
                ok, frame = cap.read()
                if not ok or frame is None:
                    break
                if frame_id % step == 0:
                    out_path = images_dir / f"{prefix}_{img_idx:06d}.jpg"
                    ok2 = bool(cv2.imwrite(str(out_path), frame))
                    if not ok2:
                        log(f"[extract:warn] save failed: {out_path}")
                    else:
                        saved += 1
                        img_idx += 1
                        extracted_paths.append(out_path)
                        if first_n_limit > 0 and saved >= first_n_limit:
                            break
                frame_id += 1
                if frame_id % 2000 == 0:
                    log(f"[extract:progress] frame={frame_id} saved={saved}")
        finally:
            cap.release()
        log(f"[extract:done] saved={saved}")

        labels_dir = extract_root / "labels"
        labels_dir.mkdir(parents=True, exist_ok=True)

        model_path_text = self._current_model_path()
        model_path = Path(model_path_text) if model_path_text else Path("models/best.pt")
        model_source, auto_download = resolve_model_source(model_path)
        if not model_source:
            log("[label:warn] empty model path")
            QMessageBox.warning(self, "Model Missing", "Label model path is empty.")
            return
        if not model_path.exists() and not auto_download:
            log(f"[label:warn] model file not found: {model_path}")
            QMessageBox.warning(self, "Model Missing", f"Label model file not found:\n{model_path}")
            return

        try:
            from ultralytics import YOLO
        except Exception as exc:  # noqa: BLE001
            log(f"[label:warn] failed to import ultralytics: {exc}")
            QMessageBox.warning(self, "ultralytics Missing", f"Failed to import ultralytics for labeling.\n{exc}")
            return

        try:
            label_model = YOLO(str(ensure_model_source(model_path)))
        except Exception as exc:  # noqa: BLE001
            log(f"[label:warn] failed to load model: {model_source} ({exc})")
            QMessageBox.warning(self, "Model Load Failed", f"Failed to load label model:\n{model_source}\n{exc}")
            return

        try:
            conf = float(self.conf_input.value()) if hasattr(self, "conf_input") else 0.25
        except Exception:
            conf = 0.25

        # Reuse the same class mapping policy as the main pipeline.
        class_mapping_path = Path(self.cfg_defaults.get("class_mapping_path", "config/category_mapping.json"))
        raw_mapping = {}
        try:
            if class_mapping_path.exists():
                raw_mapping = load_json(class_mapping_path) or {}
        except Exception as exc:  # noqa: BLE001
            log(f"[label:warn] failed to load class mapping: {class_mapping_path} ({exc})")

        class_mapping = {str(k).strip().lower(): str(v).strip().lower() for k, v in dict(raw_mapping).items()}
        class_name_to_id = {
            "person": 0,
            "small_bus": 1,
            "passenger_car": 2,
            "medium_truck": 3,
            "large_truck": 4,
            "large_bus": 5,
            "etc": 6,
            "small_truck": 7,
            # Fallback names for generic models
            "car": 2,
            "bus": 5,
            "truck": 4,
        }
        mapped_name_to_id = {
            "소형버스": 1,
            "승용차": 2,
            "중형화물": 3,
            "대형화물": 4,
            "대형버스": 5,
            "기타": 6,
            "소형화물": 7,
        }
        ignore_labels = {"ignore", "ignored", "none", "", "person"}

        # Label newly extracted images first; if none extracted in this run, continue on existing images.
        if extracted_paths:
            target_images = extracted_paths
        else:
            log("[label:info] No new images extracted. Continue labeling existing images.")
            target_images = sorted(images_dir.glob(f"{prefix}_*.jpg"))

        total_images = len(target_images)
        if total_images <= 0:
            log("[label:info] No images found for labeling.")
            return

        labeled_count = 0
        skipped_count = 0
        no_det_count = 0
        error_count = 0

        log(
            f"[label:start] images={total_images} conf={conf:.2f} model={model_source} out_dir={labels_dir}"
        )

        for idx, img_path in enumerate(target_images, start=1):
            label_path = labels_dir / f"{img_path.stem}.txt"

            try:
                result = label_model(str(img_path), conf=conf, verbose=False)[0]
            except Exception as exc:  # noqa: BLE001
                error_count += 1
                if error_count <= 5:
                    log(f"[label:warn] infer failed: {img_path.name} ({exc})")
                continue

            boxes = result.boxes
            if boxes is None or len(boxes) == 0:
                no_det_count += 1
                continue

            lines = []
            for box in boxes:
                try:
                    cls_raw = int(box.cls[0])
                    cls_name = str(label_model.names.get(cls_raw, str(cls_raw))).strip().lower()
                    mapped_name = class_mapping.get(cls_name, cls_name)
                    if mapped_name in ignore_labels:
                        continue
                    cls = class_name_to_id.get(cls_name)
                    if cls is None:
                        cls = class_name_to_id.get(mapped_name)
                    if cls is None:
                        cls = mapped_name_to_id.get(mapped_name, cls_raw)
                    if cls == 0:
                        continue
                    x_c, y_c, w, h = box.xywhn[0].tolist()
                except Exception:
                    continue

                lines.append(f"{cls} {x_c:.6f} {y_c:.6f} {w:.6f} {h:.6f}\n")

            if not lines:
                no_det_count += 1
                continue

            try:
                label_path.write_text("".join(lines), encoding="utf-8")
                labeled_count += 1
            except Exception as exc:  # noqa: BLE001
                error_count += 1
                if error_count <= 5:
                    log(f"[label:warn] save failed: {img_path.name} ({exc})")
                continue


            if idx % 100 == 0 or idx == total_images:
                log(
                    f"[label:progress] [{idx}/{total_images}] saved={labeled_count} skipped={skipped_count} "
                    f"no_det={no_det_count} errors={error_count}"
                )

        log(
            f"[label:done] saved={labeled_count} skipped={skipped_count} "
            f"no_det={no_det_count} errors={error_count}"
        )
        log(f"[label:dir] {labels_dir}")

    def on_preview_vehicle(self) -> None:
        video_path = select_video(self)
        if not video_path:
            return
        model_path_text = self._current_model_path()
        model_path = Path(model_path_text) if model_path_text else Path("models/best.pt")
        model_source, auto_download = resolve_model_source(model_path)
        if not model_source:
            QMessageBox.warning(self, "모델 경로 없음", "모델 경로를 설정하세요.")
            return
        if not model_path.exists() and not auto_download:
            QMessageBox.warning(self, "모델 경로 없음", f"모델 파일을 찾을 수 없습니다:\n{model_path}")
            return
        viewer = VehiclePreviewWindow(video_path=video_path, model_path=ensure_model_source(model_path), parent=self)
        viewer.show()
        viewer.raise_()
        self._viewers.append(viewer)

    def _log_box(self) -> QGroupBox:
        box = QGroupBox("📝 로그")
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(False)
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        layout = QVBoxLayout()
        layout.addWidget(self.progress_bar)
        layout.addWidget(self.log_view)
        box.setLayout(layout)
        return box

    def _apply_style(self) -> None:
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(MAIN_WINDOW_STYLE)
        else:
            self.setStyleSheet(MAIN_WINDOW_STYLE)

    def _update_status_bar(self) -> None:
        """StatusBar 항목을 현재 설정값으로 갱신한다."""
        if not hasattr(self, "status_bar"):
            return
        # 모델
        model = self._current_model_path()
        if model and Path(model).exists():
            self.status_bar.update_item("model", f"모델: {Path(model).name}", "ok")
        elif model:
            self.status_bar.update_item("model", f"모델: {Path(model).name}", "warn")
        else:
            self.status_bar.update_item("model", "모델: –", "off")
        # DB
        db_text = ""
        if hasattr(self, "db_path_input"):
            db_text = self.db_path_input.text().strip()
        if db_text and Path(db_text).exists():
            self.status_bar.update_item("db", f"DB: {Path(db_text).name}", "ok")
        elif db_text:
            self.status_bar.update_item("db", f"DB: {Path(db_text).name}", "warn")
        else:
            self.status_bar.update_item("db", "DB: –", "off")
        # GPU
        try:
            import torch
            if torch.cuda.is_available():
                gpu_name = torch.cuda.get_device_name(0)
                self.status_bar.update_item("gpu", f"GPU: {gpu_name}", "ok")
            else:
                self.status_bar.update_item("gpu", "GPU: 없음", "off")
        except Exception:
            self.status_bar.update_item("gpu", "GPU: 확인불가", "off")

    # ------------------------------------------------------------------ 모델/차종

    def _current_model_path(self) -> str:
        """현재 선택된 모델 경로. 콤보박스가 아직 없으면 설정값을 쓴다."""
        if hasattr(self, "model_combo"):
            path = self.model_combo.current_path()
            if path:
                return path
        return str(self.cfg_defaults.get("model_path", "") or "").strip()

    def _current_model_profile(self, refresh: bool = False):
        """선택된 모델의 프로파일(클래스 목록 + 차종 설정)을 가져온다.

        모델 파일을 여는 작업이라 처음 한 번만 느리고 이후에는 캐시를 쓴다.
        """
        model_path = self._current_model_path()
        if not model_path:
            return None
        QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            return self.profile_store.get(model_path, refresh=refresh)
        finally:
            QGuiApplication.restoreOverrideCursor()

    def _detect_class_ids(self) -> list:
        """탐지에 사용할 클래스 번호. 프로파일이 없으면 설정값을 그대로 쓴다."""
        profile = self.profile_store.get_cached(self._current_model_path())
        if profile is not None and profile.detect_class_ids:
            return list(profile.detect_class_ids)
        fallback = self.cfg_defaults.get("allowed_classes")
        return list(fallback) if isinstance(fallback, list) else []

    def _on_model_selection_changed(self, _index: int) -> None:
        """콤보박스에서 모델을 바꾸면 프로파일을 읽어 차종 표시를 갱신한다."""
        if self.model_combo.is_browse_selected():
            previous = str(self.cfg_defaults.get("model_path", "") or "")
            chosen = self.model_combo.prompt_for_model(self)
            if not chosen:
                self.model_combo.set_path(previous)
                return
        self._update_detect_classes_label()

    def _update_detect_classes_label(self) -> None:
        """'탐지 차종' 줄에 현재 선택 상태를 요약해 보여준다."""
        if not hasattr(self, "detect_classes_label"):
            return
        model_path = self._current_model_path()
        if not model_path:
            self.detect_classes_label.setText("모델을 선택하세요.")
            return
        profile = self.profile_store.get_cached(model_path)
        if profile is None or not profile.has_classes:
            self.detect_classes_label.setText("'탐지 차종 선택'을 눌러 모델의 클래스를 읽어오세요.")
            return
        names = profile.detect_class_names()
        if not names:
            self.detect_classes_label.setText("선택된 차종이 없습니다.")
            return
        self.detect_classes_label.setText(f"{len(names)}종: " + ", ".join(names))

    def on_select_detect_classes(self) -> None:
        """모델 클래스를 읽어 탐지 대상 차종을 고른다."""
        model_path = self._current_model_path()
        if not model_path:
            QMessageBox.warning(self, "모델 없음", "먼저 모델을 선택하세요.")
            return
        if not Path(model_path).exists():
            QMessageBox.warning(self, "모델 없음", f"모델 파일을 찾을 수 없습니다:\n{model_path}")
            return
        profile = self._current_model_profile()
        if profile is None or not profile.has_classes:
            QMessageBox.warning(
                self,
                "클래스 읽기 실패",
                f"모델에서 클래스 목록을 읽지 못했습니다:\n{model_path}\n"
                "ultralytics 설치 상태와 모델 파일을 확인하세요.",
            )
            return
        dialog = DetectClassDialog(profile, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        profile.detect_class_ids = dialog.selected_class_ids()
        self.profile_store.put(profile)
        self._update_detect_classes_label()
        self.log_view.append(f"[classes] 탐지 차종: {', '.join(profile.detect_class_names())}")

    def on_select_count_classes(self) -> None:
        """DB에 저장된 차종을 엑셀 집계 열로 매핑한다."""
        model_path = self._current_model_path()
        if not model_path:
            QMessageBox.warning(self, "모델 없음", "먼저 모델을 선택하세요. 차종 설정은 모델별로 저장됩니다.")
            return
        profile = self._current_model_profile()
        if profile is None:
            QMessageBox.warning(self, "모델 없음", f"모델 프로파일을 만들 수 없습니다:\n{model_path}")
            return

        db_path = Path(self.count_db_input.text().strip() or "")
        session_sel = self._get_count_session_id()
        db_counts = distinct_vehicle_types(db_path, session_sel) if db_path.exists() else {}
        if not db_counts and not profile.has_classes:
            QMessageBox.warning(
                self,
                "차종 없음",
                "DB에서도 모델에서도 차종을 읽지 못했습니다.\nDB 경로와 모델을 확인하세요.",
            )
            return

        dialog = CountClassMappingDialog(profile, db_counts, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        profile.excel_mapping = dialog.result_mapping()
        profile.excel_columns = dialog.result_columns()
        self.profile_store.put(profile)
        self.log_view.append(f"[classes] 집계 열: {', '.join(profile.excel_columns)}")

    def _choose_detect_db(self) -> None:
        start = Path(self.db_path_input.text().strip() or "output").resolve()
        path, _ = QFileDialog.getSaveFileName(
            self,
            "감지 DB 경로 설정",
            str(start if start.suffix else start.parent),
            "SQLite Files (*.sqlite *.db);;All Files (*)",
        )
        if path:
            self.db_path_input.setText(path)
            self._detect_db_user_set = True

    def _choose_count_db(self) -> None:
        start = Path(self.count_db_input.text() or "output").resolve()
        path, _ = QFileDialog.getOpenFileName(
            self,
            "DB 선택",
            str(start.parent if start.exists() else Path("output").resolve()),
            "SQLite Files (*.sqlite *.db);;All Files (*)",
        )
        if path:
            self._count_db_user_set = True
            self.count_db_input.setText(path)
            self._refresh_db_sessions()

    def _choose_count_output_dir(self) -> None:
        start_dir = Path(self.count_output_dir_input.text().strip() or "output").resolve()
        path = QFileDialog.getExistingDirectory(self, "결과 폴더 선택", str(start_dir))
        if not path:
            return
        self.count_output_dir_input.setText(path)

    def _choose_count_lines(self) -> None:
        start = Path(self.count_lines_input.text() or "config").resolve()
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Lines JSON 선택",
            str(start.parent if start.exists() else Path("config").resolve()),
            "JSON Files (*.json);;All Files (*)",
        )
        if path:
            self.count_lines_input.setText(path)
            if not getattr(self, "_line_settings_user_set", False):
                self.line_settings_path = Path(path)
            self._refresh_db_sessions()

    def _refresh_db_sessions(self) -> None:
        if not hasattr(self, "count_session_combo"):
            return
        try:
            db_path = Path(self.count_db_input.text().strip() or "output/tracks.sqlite")
            if not db_path.exists():
                return
            with sqlite3.connect(db_path) as conn:
                sessions_set: set[str] = set()
                for table in ("track_trajs", "tracks"):
                    try:
                        exists = (
                            conn.execute(
                                "select 1 from sqlite_master where type='table' and name=? limit 1",
                                (table,),
                            ).fetchone()
                            is not None
                        )
                    except Exception:
                        exists = False
                    if not exists:
                        continue
                    try:
                        rows = conn.execute(
                            f"select distinct session_id from {table} where session_id is not null and session_id != '' order by session_id"
                        ).fetchall()
                        for r in rows:
                            if r and r[0]:
                                sessions_set.add(str(r[0]))
                    except Exception:
                        continue
            sessions = sorted(sessions_set)
            cur = self.count_session_combo.currentText().strip()
            self.count_session_combo.blockSignals(True)
            self.count_session_combo.clear()
            self.count_session_combo.addItem("(전체)")
            for s in sessions:
                self.count_session_combo.addItem(str(s))
            if cur and cur in sessions:
                self.count_session_combo.setCurrentText(cur)
            else:
                desired = self.session_input.text().strip() if hasattr(self, "session_input") else ""
                if desired and desired in sessions:
                    self.count_session_combo.setCurrentText(desired)
            try:
                if self.count_session_combo.count() > 0:
                    self.count_session_combo.setItemText(0, "(전체)")
            except Exception:
                logger.debug("Suppressed error", exc_info=True)
            self.count_session_combo.blockSignals(False)
        except Exception:
            logger.debug("Suppressed error", exc_info=True)
            return

    def _delete_selected_session(self) -> None:
        if not hasattr(self, "count_session_combo"):
            return
        if self.count_session_combo.currentIndex() <= 0:
            QMessageBox.information(self, "세션 삭제", "삭제할 세션을 선택하세요.")
            return
        session_id = self.count_session_combo.currentText().strip()
        if not session_id:
            return
        db_path = Path(self.count_db_input.text().strip() or "output/tracks.sqlite")
        output_dir = db_path.parent if db_path.parent.exists() else Path("output")
        related_paths = []
        try:
            if output_dir.exists():
                for p in output_dir.iterdir():
                    if session_id in p.name:
                        related_paths.append(p)
        except Exception:
            related_paths = []
        related_preview = ""
        if related_paths:
            preview_items = related_paths[:5]
            related_preview = "\n".join([f"- {p.name}" for p in preview_items])
            if len(related_paths) > 5:
                related_preview += f"\n- ... (+{len(related_paths) - 5}개)"
            related_preview = f"\n\n관련 파일 삭제 대상 {len(related_paths)}개:\n{related_preview}"
        reply = QMessageBox.question(
            self,
            "세션 삭제 확인",
            f"선택한 세션을 DB에서 삭제할까요?\nsession_id: {session_id}{related_preview}",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        if not db_path.exists():
            QMessageBox.warning(self, "DB 없음", f"DB 파일을 찾을 수 없습니다:\n{db_path}")
            return
        try:
            with sqlite3.connect(db_path) as conn:
                cur = conn.cursor()
                cur.execute("DELETE FROM track_trajs WHERE session_id = ?", (session_id,))
                cur.execute("DELETE FROM tracks WHERE session_id = ?", (session_id,))
                conn.commit()
            deleted_files = 0
            for p in related_paths:
                try:
                    if p.is_dir():
                        shutil.rmtree(p, ignore_errors=True)
                    else:
                        p.unlink(missing_ok=True)
                    deleted_files += 1
                except Exception:
                    logger.debug("Suppressed error", exc_info=True)
            self.log_view.append(f"[session] deleted: {session_id}")
            if deleted_files:
                self.log_view.append(f"[session] related files deleted: {deleted_files}")
            self._refresh_db_sessions()
        except Exception as exc:  # noqa: BLE001
            self.log_view.append(f"[session] delete failed: {exc}")
            QMessageBox.critical(self, "세션 삭제 실패", str(exc))

    def on_edit_lines(self) -> None:
        video = self.video_path
        if not video:
            QMessageBox.warning(self, "영상 없음", "라인 편집 전에 영상(영상1)을 선택하세요.")
            return
        editor = LineDrawerWindow(video_path=video, line_path=self.line_settings_path)
        editor.show()
        editor.raise_()
        self._editors.append(editor)

    def on_show_trajectories(self) -> None:
        try:
            db_path = Path(self.count_db_input.text().strip() or "output/tracks.sqlite")
            if not db_path.exists():
                QMessageBox.warning(self, "DB 없음", f"DB 파일을 찾을 수 없습니다:\n{db_path}")
                return
            lines_path = Path(self.count_lines_input.text().strip() or "config/lines.json")
            video = self.video_path
            video_path = Path(video) if video else None
            session_id = None
            if hasattr(self, "count_session_combo") and self.count_session_combo.currentIndex() > 0:
                session_id = self.count_session_combo.currentText().strip() or None
            if not session_id and hasattr(self, "session_input"):
                session_id = self.session_input.text().strip() or None
            if not session_id and video_path:
                session_id = video_path.stem
            viewer = TrajectoryViewer2Window(
                db_path,
                lines_path=lines_path if lines_path.exists() else None,
                video_path=video_path,
                resize=None,
                session_id=session_id,
                config_path=self.state_path,
            )
            viewer.show()
            viewer.raise_()
            self._viewers.append(viewer)
        except Exception as exc:  # noqa: BLE001
            if self.log_view:
                self.log_view.append(f"궤적보기/수정 오류: {exc}")
            QMessageBox.critical(self, "궤적보기/수정 오류", str(exc))

    def _count_class_settings(self) -> Tuple[Dict[str, str], list]:
        """현재 모델 프로파일에 저장된 엑셀 차종 매핑과 열 순서.

        설정이 없으면 빈 값을 돌려주고, count_tracks 가 기존 자동 판별로 처리한다.
        """
        profile = self.profile_store.get_cached(self._current_model_path())
        if profile is None:
            return {}, []
        return dict(profile.excel_mapping), list(profile.excel_columns)

    def _build_count_overrides(self) -> Dict:
        mapping, columns = self._count_class_settings()
        return {
            "count_db_path": self.count_db_input.text().strip(),
            "count_output_dir": self.count_output_dir_input.text().strip() if hasattr(self, "count_output_dir_input") else "",
            "count_lines_path": self.count_lines_input.text().strip(),
            "count_interval_minutes": int(self.count_interval_combo.currentText()),
            "count_analysis_basis": str(self.count_analysis_basis_combo.currentData() or "original"),
            "count_reconnect_enabled": False,
            "count_reconnect_dist": 0.0,
            "count_reconnect_gap": 0.0,
            "count_reconnect_passes": 0,
            "count_extrap_enabled": False,
            "count_extrap_horizon": 0.0,
            "count_class_mapping": mapping,
            "count_class_columns": columns,
        }

    def _build_overrides(self) -> Dict:
        """Collect overrides for detection and tracking."""
        tracker_name = self.tracker_combo.currentText() if hasattr(self, "tracker_combo") else "botsort"
        tracker_config = "config/botsort_stable.yaml" if tracker_name == "botsort" else "config/bytetrack.yaml"
        junction_name = self.junction_input.text().strip() if hasattr(self, "junction_input") else ""
        session_name = self.session_input.text().strip() if hasattr(self, "session_input") else ""
        return {
            "junction_name": junction_name,
            "session_name": session_name,
            "session_id": self._compose_session_id(),
            "target_fps": float(self.fps_input.value()),
            "head_seconds": float(self.head_input.value()),
            "tail_seconds": float(self.tail_input.value()),
            "yolo_imgsz": int(self.imgsz_combo.currentText()),
            "yolo_rect": True,
            "confidence_threshold": float(self.conf_input.value()),
            "model_path": self._current_model_path(),
            "allowed_classes": self._detect_class_ids(),
            "db_path": self.db_path_input.text().strip(),
            "line_settings_path": str(self.line_settings_path),
            "yolo_version": self.yolo_combo.currentText(),
            "tracker_config": tracker_config,
            "flush_interval_minutes": int(self.flush_combo.currentText()),
            "max_idle_frames": int(self.max_idle_frames_spin.value()),
        }

    def _get_count_session_id(self) -> Optional[str]:
        if hasattr(self, "count_session_combo") and self.count_session_combo.currentIndex() > 0:
            return self.count_session_combo.currentText().strip() or None
        return None

    def on_export_colab(self) -> None:
        """현재 설정을 Colab용 설정 JSON과 실행 셀 코드로 내보낸다."""
        try:
            cfg = dict(self.cfg_defaults)
            cfg.update(self._build_overrides())
            cfg.update(self._build_count_overrides())

            project_root = Path.cwd().resolve()
            mapping, columns = self._count_class_settings()
            colab_cfg = build_colab_config(
                cfg,
                project_root,
                allowed_classes=self._detect_class_ids(),
                count_class_mapping=mapping or None,
                count_class_columns=columns or None,
            )

            out_dir = Path("colab")
            out_dir.mkdir(parents=True, exist_ok=True)
            junction = self._safe_filename(self.junction_input.text().strip()) if hasattr(self, "junction_input") else ""
            name = f"app_config_colab_{junction}.json" if junction else "app_config_colab_generated.json"
            cfg_out = out_dir / name
            with open(cfg_out, "w", encoding="utf-8") as f:
                json.dump(colab_cfg, f, ensure_ascii=False, indent=2)

            cell = build_colab_cell(
                project_root=project_root,
                config_path=cfg_out,
                video_path=self._get_active_video_path() or "",
                line_settings_path=str(self.line_settings_path),
                session_id=self._compose_session_id(),
                interval_minutes=int(self.count_interval_combo.currentText()),
            )

            QGuiApplication.clipboard().setText(cell)
            self.log_view.append(f"[colab] 설정 저장: {cfg_out}")
            self.log_view.append("[colab] 실행 셀 코드를 클립보드에 복사했습니다.")

            # 구글드라이브 밖의 경로는 Colab에서 열 수 없다. 조용히 실패하지 않도록 알린다.
            bad_paths = unconvertible_paths(colab_cfg)
            video_now = self._get_active_video_path() or ""
            if video_now and not is_colab_path(to_colab_path(video_now, project_root)):
                bad_paths["video"] = video_now
            if bad_paths:
                detail = "\n".join(f"  - {k}: {v}" for k, v in bad_paths.items())
                self.log_view.append("[colab:warn] 드라이브 밖 경로가 있어 Colab에서 열 수 없습니다:")
                for k, v in bad_paths.items():
                    self.log_view.append(f"[colab:warn]   {k} = {v}")
                QMessageBox.warning(
                    self,
                    "Colab에서 열 수 없는 경로",
                    "아래 경로는 구글드라이브 안에 있지 않아 Colab에서 접근할 수 없습니다."
                    "\n드라이브로 옮기거나 셀 코드에서 직접 고치세요.\n\n" + detail,
                )

            dialog = QDialog(self)
            dialog.setWindowTitle("Colab 실행 셀 (클립보드에 복사됨)")
            dialog.setStyleSheet(DARK_DIALOG_STYLE)
            dialog.resize(820, 560)
            layout = QVBoxLayout()
            layout.addWidget(QLabel(f"설정 파일: {cfg_out}\n아래 코드를 Colab 셀에 붙여넣으세요."))
            view = QTextEdit()
            view.setPlainText(cell)
            view.setReadOnly(True)
            layout.addWidget(view)
            copy_btn = QPushButton("다시 복사")
            copy_btn.clicked.connect(lambda: QGuiApplication.clipboard().setText(cell))
            close_btn = QPushButton("닫기")
            close_btn.clicked.connect(dialog.accept)
            row = QHBoxLayout()
            row.addWidget(copy_btn)
            row.addStretch()
            row.addWidget(close_btn)
            layout.addLayout(row)
            dialog.setLayout(layout)
            dialog.exec()
        except Exception as exc:  # noqa: BLE001
            self.log_view.append("[colab] ERROR")
            self.log_view.append(traceback.format_exc())
            QMessageBox.critical(self, "Colab 코드 생성 실패", str(exc))

    def _save_config(self) -> None:
        try:
            state = load_app_config(self.state_path) if self.state_path.exists() else {}
            state.update(self._build_overrides())
            state.update(self._build_count_overrides())
            save_user_state(self.state_path, state)
            if self.log_view:
                self.log_view.append(f"사용자 설정을 저장했습니다: {self.state_path}")
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "설정 저장 실패", str(exc))

    def _get_active_video_path(self) -> Optional[str]:
        for attr in ("video_path", "video_path2", "video_path3"):
            value = getattr(self, attr, None)
            if value:
                return str(value)
        return None

    def _run_count_common(self, mode: str) -> None:
        """교차로(turn) / 접근로(approach) 카운팅을 백그라운드 스레드에서 실행."""
        if getattr(self, "_count_worker", None) and self._count_worker.isRunning():
            QMessageBox.information(self, "실행 중", "카운팅이 이미 실행 중입니다.")
            return

        label = "교차로" if mode == "turn" else "접근로"
        tag = "count" if mode == "turn" else "approach"
        overrides_count = self._build_count_overrides()
        db_path = Path(overrides_count["count_db_path"] or "output/tracks.sqlite")
        lines_path = Path(overrides_count["count_lines_path"] or "config/lines.json")
        if not db_path.exists():
            QMessageBox.warning(self, "DB 없음", f"DB 파일을 찾을 수 없습니다:\n{db_path}")
            return
        if not lines_path.exists():
            QMessageBox.warning(self, "Lines JSON 없음", f"lines.json 파일을 찾을 수 없습니다:\n{lines_path}")
            return

        analysis_basis = str(overrides_count.get("count_analysis_basis") or "original")
        use_postprocess = analysis_basis == "postprocess"
        basis_label = "후처리" if use_postprocess else "원본"
        session_sel = None
        if hasattr(self, "count_session_combo"):
            sel = self.count_session_combo.currentText().strip()
            session_sel = None if sel in ("", "(전체)") else sel

        self.log_view.append(f"[{tag}] db={db_path}")
        self.log_view.append(f"[{tag}] lines={lines_path}")
        self.log_view.append(f"[{tag}] basis={basis_label}")
        if session_sel:
            self.log_view.append(f"[{tag}] session_id={session_sel}")

        filename_prefix = self._build_count_filename_prefix(session_sel=session_sel)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir = Path(overrides_count.get("count_output_dir") or "").expanduser()
        if not str(out_dir).strip():
            out_dir = db_path.parent
        out_dir.mkdir(parents=True, exist_ok=True)
        out_xlsx = out_dir / f"{filename_prefix}_{label}_{basis_label}_{stamp}.xlsx"

        kwargs = dict(
            db_path=db_path,
            lines_path=lines_path,
            interval_min=int(overrides_count["count_interval_minutes"]),
            reconnect_dist=float(overrides_count["count_reconnect_dist"]),
            reconnect_gap=float(overrides_count["count_reconnect_gap"]),
            reconnect_passes=int(overrides_count["count_reconnect_passes"]),
            extrap_horizon=float(overrides_count["count_extrap_horizon"]),
            out_csv=None,
            out_xlsx=out_xlsx,
            resize=None,
            session_id=session_sel,
            use_track_merge=use_postprocess,
            use_virtual_events=use_postprocess,
            class_mapping=overrides_count.get("count_class_mapping") or None,
            class_columns=overrides_count.get("count_class_columns") or None,
        )

        self._count_worker = CountWorker(kwargs, mode, f"{label}({basis_label})")
        self._count_worker.log_message.connect(self.log_view.append)
        self._count_worker.finished_ok.connect(self._on_count_finished)
        self._count_worker.failed.connect(self._on_count_failed)
        self.log_view.append(f"[{tag}] 카운팅 시작...")
        self._count_worker.start()

    def _on_count_finished(self, label: str, result: str) -> None:
        self.log_view.append(f"{label} 카운팅 완료: {result}")

    def _on_count_failed(self, label: str, error: str) -> None:
        self.log_view.append(f"[count] ERROR: {error}")
        QMessageBox.critical(self, f"{label} 카운팅 실패", error)

    def on_count(self) -> None:
        self._run_count_common("turn")

    def on_count_approach(self) -> None:
        self._run_count_common("approach")

    def on_run(self) -> None:
        if self.worker and self.worker.isRunning():
            QMessageBox.information(self, "실행 중", "파이프라인이 이미 실행 중입니다.")
            return

        video = self._get_active_video_path()
        if not video:
            QMessageBox.warning(self, "영상 없음", "영상 파일을 선택하세요.")
            return

        overrides = self._build_overrides()
        if not overrides.get("model_path"):
            QMessageBox.warning(self, "모델 경로 없음", "모델 경로를 설정하세요.")
            return
        if not Path(overrides["model_path"]).exists() and not can_auto_download_model(overrides["model_path"]):
            QMessageBox.critical(self, "모델 파일 없음", f"모델 파일을 찾을 수 없습니다:\n{overrides['model_path']}")
            return

        self.log_view.append("파이프라인 시작...")
        self.log_view.append(f"모델 경로: {overrides['model_path']}")
        self._pipeline_start_ts = datetime.now()
        self.log_view.append(f"[time] 시작: {self._pipeline_start_ts.strftime('%Y-%m-%d %H:%M:%S')}")
        self.worker = PipelineWorker(video, self.cfg_path, self.line_settings_path, overrides)
        self.worker.finished.connect(self._clear_finished_worker, Qt.ConnectionType.QueuedConnection)
        self.worker.log_message.connect(self._append_log, Qt.ConnectionType.QueuedConnection)
        self.worker.finished_ok.connect(self._on_pipeline_done, Qt.ConnectionType.QueuedConnection)
        self.worker.failed.connect(self.on_worker_failed, Qt.ConnectionType.QueuedConnection)
        self.worker.start()

    def on_worker_failed(self, message: str) -> None:
        self.log_view.append(f"실패: {message}")
        if self._pipeline_start_ts is not None:
            end_ts = datetime.now()
            elapsed = end_ts - self._pipeline_start_ts
            self._append_log(f"[time] 종료: {end_ts.strftime('%Y-%m-%d %H:%M:%S')}")
            self._append_log(f"[time] 소요: {elapsed}")
            self._pipeline_start_ts = None
        QMessageBox.critical(self, "실행 오류", message)

    def on_quit_program(self) -> None:
        # Persist settings and stop the worker cleanly before quitting.
        try:
            self._save_config()
        except Exception:
            logger.debug("Suppressed error", exc_info=True)
        try:
            self._stop_worker(wait_ms=5000)
        except Exception:
            logger.debug("Suppressed error", exc_info=True)
        app = QApplication.instance()
        if app is not None:
            app.quit()
        else:
            self.close()


def main() -> None:
    app = QApplication(sys.argv)

    # Slightly increase the base font size for desktop readability.
    try:
        base = app.font()
        pt = float(base.pointSizeF() if base.pointSizeF() > 0 else base.pointSize())
        if pt <= 0:
            pt = 10.0
        base.setPointSizeF(pt * UI_SCALE)
        app.setFont(base)
    except Exception:
        logger.debug("Suppressed error", exc_info=True)

    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
