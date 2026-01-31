import sys
import traceback
import shutil
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

from PySide6.QtCore import QThread, Signal, Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDoubleSpinBox,
    QCheckBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from src.config.loader import load_app_config, load_json, load_line_settings, save_app_config
from src.db.schema import init_db
from src.pipeline.detect_to_db import build_tracker
from src.pipeline.count_tracks import run_count
from src.db.writer import TrackTrajDBWriter
from src.ui.line_editor import LineEditorWindow
from src.ui.line_drawer import LineDrawerWindow
from src.ui.video_selector import select_video
from src.ui.trajectory_viewer import TrajectoryViewerWindow


UI_SCALE = 1.35


class PipelineWorker(QThread):
    log_message = Signal(str)
    finished_ok = Signal()
    failed = Signal(str)

    def __init__(self, video_path: Path, cfg_path: Path, line_path: Path, overrides: Optional[Dict] = None):
        super().__init__()
        self.video_path = video_path
        self.cfg_path = cfg_path
        self.line_path = line_path
        self.overrides = overrides or {}

    def run(self) -> None:
        try:
            cfg = load_app_config(self.cfg_path)
            cfg.update(self.overrides)
            line_cfg = load_line_settings(self.line_path) if self.line_path else load_line_settings(Path(cfg["line_settings_path"]))
            class_mapping = load_json(Path(cfg["class_mapping_path"])) if cfg.get("class_mapping_path") else {}

            tracker, device = build_tracker(cfg, line_cfg, class_mapping)
            db_path = Path(cfg["db_path"])
            session_base = (self.overrides.get("session_id") or "").strip() or Path(self.video_path).stem
            session_id = session_base

            # session_id 충돌 시 자동으로 _YYYYMMDD_HHMMSS를 붙여 새 세션으로 저장
            try:
                import sqlite3

                init_db(db_path)

                def _session_exists(conn: sqlite3.Connection, sid: str) -> bool:
                    try:
                        row = conn.execute("SELECT 1 FROM track_trajs WHERE session_id=? LIMIT 1", (sid,)).fetchone()
                        if row:
                            return True
                    except sqlite3.OperationalError:
                        pass
                    try:
                        row = conn.execute("SELECT 1 FROM tracks WHERE session_id=? LIMIT 1", (sid,)).fetchone()
                        if row:
                            return True
                    except sqlite3.OperationalError:
                        pass
                    return False

                with sqlite3.connect(db_path) as conn:
                    if _session_exists(conn, session_id):
                        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                        session_id = f"{session_base}_{stamp}"
                        self.log_message.emit(f"[info] session_id 충돌 → 새 세션으로 저장: {session_id}")
            except Exception:
                pass

            self.log_message.emit(f"Device: {device}")
            self.log_message.emit(f"Running detection+tracking on {self.video_path}")
            with TrackTrajDBWriter(db_path) as writer:
                tracker.run(
                    video_path=self.video_path,
                    db_writer=writer,
                    session_id=session_id,
                    progress_cb=self.log_message.emit,
                    should_stop_cb=self.isInterruptionRequested,
                )
            if self.isInterruptionRequested():
                self.failed.emit("중지됨")
            else:
                self.finished_ok.emit()
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("차량 교통량 카운터 (v5)")
        # 글자 크기를 키우면 '가로 폭'이 먼저 부족해져서 입력 박스가 눌려 보일 수 있어,
        # 기본 창은 세로보다 가로를 더 넓게 잡는다.
        self.resize(int(1050 * UI_SCALE), int(700 * UI_SCALE))

        self.cfg_path = Path("config/app_config.json")
        self.video_path: Optional[Path] = None
        self.line_settings_path: Path = Path("config/line_settings.sample.json")
        self.worker: Optional[PipelineWorker] = None
        self._editors = []
        self._viewers = []

        self.cfg_defaults = load_app_config(self.cfg_path)

        self._build_ui()
        self._apply_style()

    def _stop_worker(self, wait_ms: int = 5000) -> None:
        worker = self.worker
        if worker is None:
            return
        try:
            if worker.isRunning():
                worker.requestInterruption()
                worker.wait(int(wait_ms))
                if worker.isRunning():
                    # 최후 수단: 강제 종료 (가능하면 위에서 정상 종료되도록 max_idle_frames/중단 체크로 커버)
                    worker.terminate()
                    worker.wait(1000)
        except Exception:
            pass
        self.worker = None

    def closeEvent(self, event) -> None:
        # 실행 중인 스레드가 있으면 먼저 정리해서 "QThread: Destroyed while thread is still running" 방지
        try:
            if self.worker and self.worker.isRunning():
                res = QMessageBox.question(
                    self,
                    "종료 확인",
                    "탐지/추적(DB 저장)이 실행 중입니다.\n중지하고 종료할까요?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if res != QMessageBox.StandardButton.Yes:
                    event.ignore()
                    return
        except Exception:
            pass

        for w in list(self._viewers):
            try:
                w.close()
            except Exception:
                pass
        for w in list(self._editors):
            try:
                w.close()
            except Exception:
                pass

        self._stop_worker()
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
            pass

    def _on_pipeline_done(self) -> None:
        self._append_log("파이프라인 완료.")

    def _build_ui(self) -> None:
        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(18, 18, 18, 18)
        main_layout.setSpacing(12)

        main_layout.addWidget(self._header())
        main_layout.addWidget(self._session_video_box())
        main_layout.addWidget(self._settings_box())
        main_layout.addWidget(self._count_box())
        # 교차로명이 이미 입력되어 있고 DB 경로가 기본값이면 db_snapshots 경로로 자동 제안
        try:
            self._maybe_update_count_db_path_from_junction()
        except Exception:
            pass
        main_layout.addLayout(self._buttons_row())
        log_box = self._log_box()
        main_layout.addWidget(log_box, stretch=1)

        container = QWidget()
        container.setLayout(main_layout)
        self.setCentralWidget(container)

    def _header(self) -> QWidget:
        # 제목 영역 제거: 빈 컨테이너 반환 (여백도 최소화)
        widget = QWidget()
        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        widget.setLayout(layout)
        return widget

    def _session_video_box(self) -> QGroupBox:
        box = QGroupBox("세션 및 영상")
        layout = QGridLayout()
        layout.setVerticalSpacing(8)
        layout.setHorizontalSpacing(10)

        # 교차로명/세션명은 별도 저장하고, 실제 session_id는 "{교차로명} {세션명}"으로 조합해 사용한다.
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
        self.junction_input.setPlaceholderText("예: 상남사거리")
        self.session_input = QLineEdit(session_name_default)
        self.session_input.setPlaceholderText("예: 오전첨두_1")

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
        # 폭 배분: 교차로명(좁게), 세션명(중간~넓게), 영상(넓게)
        layout.setColumnStretch(1, 2)
        layout.setColumnStretch(3, 4)
        layout.setColumnStretch(5, 6)

        box.setLayout(layout)
        try:
            self.junction_input.textChanged.connect(self._maybe_update_count_db_path_from_junction)
        except Exception:
            pass
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

    def _suggest_db_path_for_junction(self, junction_name: str) -> Path:
        safe = self._safe_filename(junction_name)
        return Path("output/db_snapshots") / f"tracks_{safe}.sqlite"

    def _maybe_update_count_db_path_from_junction(self) -> None:
        """교차로명을 입력하면 기본 DB 경로를 db_snapshots 아래로 제안."""
        if not hasattr(self, "count_db_input") or not hasattr(self, "junction_input"):
            return
        junction = self.junction_input.text().strip()
        if not junction:
            return
        current = (self.count_db_input.text() or "").strip()
        suggested = str(self._suggest_db_path_for_junction(junction))

        # 사용자가 DB를 직접 지정한 경우는 건드리지 않음
        if getattr(self, "_count_db_user_set", False):
            return

        # 기본값/기존 db_snapshots 사용 중이면 제안 경로로 업데이트
        if (not current) or ("output/tracks.sqlite" in current) or ("db_snapshots" in current):
            self.count_db_input.setText(suggested)
            try:
                self._refresh_db_sessions()
            except Exception:
                pass

    def _settings_box(self) -> QGroupBox:
        box = QGroupBox("분석 설정")
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
        for s in ["640", "960", "1280"]:
            self.imgsz_combo.addItem(s)
        default_imgsz = int(self.cfg_defaults.get("yolo_imgsz") or self.cfg_defaults.get("resize_width") or 640)
        default_imgsz = 1280 if default_imgsz not in (640, 960, 1280) else default_imgsz
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

        # Track end threshold (frames)
        self.max_idle_frames_spin = QSpinBox()
        self.max_idle_frames_spin.setRange(1, 10_000)
        self.max_idle_frames_spin.setValue(int(self.cfg_defaults.get("max_idle_frames", 30)))

        # Model path
        self.model_path_input = QLineEdit(self.cfg_defaults.get("model_path", ""))
        model_btn = QPushButton("모델 선택")
        model_btn.setFixedWidth(160)
        model_btn.clicked.connect(self._choose_model)

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
        layout.addWidget(self._cap("추론 크기(imgsz)"), row, 0)
        layout.addWidget(self.imgsz_combo, row, 1)
        layout.addWidget(self._cap("Confidence"), row, 2)
        layout.addWidget(self.conf_input, row, 3)
        layout.addWidget(self._cap("DB 전송 기간(분)"), row, 4)
        layout.addWidget(self.flush_combo, row, 5)
        layout.addWidget(self._cap("ID 종료(프레임)"), row, 6)
        layout.addWidget(self.max_idle_frames_spin, row, 7)

        row += 1
        layout.addWidget(self._cap("모델 경로"), row, 0)
        layout.addWidget(self.model_path_input, row, 1, 1, 4)
        layout.addWidget(model_btn, row, 5)
        layout.addWidget(self._cap("Tracker"), row, 6)
        layout.addWidget(self.tracker_combo, row, 7)

        box.setLayout(layout)
        return box

    def _count_box(self) -> QGroupBox:
        """DB 기반 카운팅 설정 입력."""
        box = QGroupBox("카운팅 설정 (DB 분석)")
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

        # Session selector (카운팅/궤적보기에서 사용)
        self.count_session_combo = QComboBox()
        self.count_session_combo.addItem("(전체)")
        self.count_session_refresh_btn = QPushButton("세션 갱신")
        self.count_session_refresh_btn.setFixedWidth(120)
        self.count_session_refresh_btn.clicked.connect(self._refresh_db_sessions)
        self.count_session_delete_btn = QPushButton("세션 삭제")
        self.count_session_delete_btn.setFixedWidth(120)
        self.count_session_delete_btn.clicked.connect(self._delete_selected_session)
        try:
            self.count_db_input.editingFinished.connect(self._refresh_db_sessions)
            self.count_db_input.editingFinished.connect(lambda: setattr(self, "_count_db_user_set", True))
        except Exception:
            pass

        # Interval / reconnect / extrap
        self.count_interval_combo = QComboBox()
        for m in ["5", "15", "30", "60"]:
            self.count_interval_combo.addItem(m)
        default_interval = str(int(self.cfg_defaults.get("count_interval_minutes", 15)))
        if default_interval in [self.count_interval_combo.itemText(i) for i in range(self.count_interval_combo.count())]:
            self.count_interval_combo.setCurrentText(default_interval)

        self.count_reconnect_dist = QDoubleSpinBox()
        self.count_reconnect_dist.setRange(0.0, 10_000.0)
        self.count_reconnect_dist.setDecimals(1)
        self.count_reconnect_dist.setValue(float(self.cfg_defaults.get("count_reconnect_dist", 50.0)))

        self.count_reconnect_gap = QDoubleSpinBox()
        self.count_reconnect_gap.setRange(0.0, 120.0)
        self.count_reconnect_gap.setDecimals(1)
        self.count_reconnect_gap.setValue(float(self.cfg_defaults.get("count_reconnect_gap", 3.0)))

        self.count_reconnect_passes = QSpinBox()
        self.count_reconnect_passes.setRange(0, 10)
        self.count_reconnect_passes.setValue(int(self.cfg_defaults.get("count_reconnect_passes", 2)))

        reconnect_enabled_default = bool(self.cfg_defaults.get("count_reconnect_enabled"))
        if "count_reconnect_enabled" not in self.cfg_defaults:
            reconnect_enabled_default = int(self.cfg_defaults.get("count_reconnect_passes", 2)) > 0
        self.count_reconnect_enabled = QCheckBox("재연결 사용")
        self.count_reconnect_enabled.setProperty("role", "caption")
        self.count_reconnect_enabled.setChecked(reconnect_enabled_default)

        extrap_enabled_default = bool(self.cfg_defaults.get("count_extrap_enabled"))
        if "count_extrap_enabled" not in self.cfg_defaults:
            extrap_enabled_default = float(self.cfg_defaults.get("count_extrap_horizon", 200.0)) > 0
        self.count_extrap_enabled = QCheckBox("외삽 사용")
        self.count_extrap_enabled.setProperty("role", "caption")
        self.count_extrap_enabled.setChecked(extrap_enabled_default)

        self.count_extrap_horizon = QDoubleSpinBox()
        self.count_extrap_horizon.setRange(0.0, 10_000.0)
        self.count_extrap_horizon.setDecimals(1)
        self.count_extrap_horizon.setValue(float(self.cfg_defaults.get("count_extrap_horizon", 200.0)))
        # 외삽 길이/재연결 횟수 입력란 폭을 절반 수준으로 축소
        try:
            self.count_extrap_horizon.setFixedWidth(90)
            self.count_reconnect_passes.setFixedWidth(90)
        except Exception:
            pass

        row = 0
        # 1줄: DB 경로 + 세션(선택/갱신/삭제)
        layout.addWidget(self._cap("DB 경로"), row, 0)
        layout.addWidget(self.count_db_input, row, 1, 1, 3)
        layout.addWidget(db_btn, row, 4)
        layout.addWidget(self._cap("세션"), row, 5)
        layout.addWidget(self.count_session_combo, row, 6)
        layout.addWidget(self.count_session_refresh_btn, row, 7)
        layout.addWidget(self.count_session_delete_btn, row, 8)

        row += 1
        # 2줄: 분석라인 + 결과저장폴더
        layout.addWidget(self._cap("분석라인"), row, 0)
        layout.addWidget(self.count_lines_input, row, 1, 1, 3)
        layout.addWidget(lines_btn, row, 4)
        layout.addWidget(self._cap("결과 저장폴더"), row, 5)
        layout.addWidget(self.count_output_dir_input, row, 6, 1, 2)
        layout.addWidget(out_dir_btn, row, 8)

        row += 1
        # 3줄: 슬롯/옵션 + 외삽 길이
        layout.addWidget(self._cap("슬롯(분)"), row, 0)
        layout.addWidget(self.count_interval_combo, row, 1)
        layout.addWidget(self.count_reconnect_enabled, row, 2)
        layout.addWidget(self.count_extrap_enabled, row, 3)
        self._extrap_label = self._cap("외삽 길이")
        layout.addWidget(self._extrap_label, row, 4)
        layout.addWidget(self.count_extrap_horizon, row, 5)
        layout.addWidget(QLabel(""), row, 6, 1, 3)

        row += 1
        # 4줄: 재연결 거리/시간차/횟수
        self._reconnect_passes_label = QLabel("재연결 횟수")
        self._reconnect_passes_label.setProperty("role", "caption")
        self._reconnect_passes_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._reconnect_dist_label = QLabel("재연결 거리")
        self._reconnect_gap_label = QLabel("재연결 시간차(초)")
        for lbl in (self._reconnect_dist_label, self._reconnect_gap_label):
            lbl.setProperty("role", "caption")
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)

        layout.addWidget(self._reconnect_dist_label, row, 0)
        layout.addWidget(self.count_reconnect_dist, row, 1)
        layout.addWidget(self._reconnect_gap_label, row, 2)
        layout.addWidget(self.count_reconnect_gap, row, 3)
        layout.addWidget(self._reconnect_passes_label, row, 4)
        layout.addWidget(self.count_reconnect_passes, row, 5)
        layout.addWidget(QLabel(""), row, 6, 1, 3)

        # column stretches (가로 폭 배분)
        # - 창을 넓혔을 때 DB 경로(1~3)가 과하게 늘어나지 않게 하고
        # - 결과 저장폴더(6~7)와 세션 선택(6)이 더 넓어지도록 배분
        layout.setColumnStretch(1, 2)
        layout.setColumnStretch(2, 1)
        layout.setColumnStretch(3, 1)
        layout.setColumnStretch(6, 10)
        layout.setColumnStretch(7, 10)

        def apply_reconnect_visibility(enabled: bool) -> None:
            self._reconnect_passes_label.setVisible(enabled)
            self.count_reconnect_passes.setVisible(enabled)
            self._reconnect_dist_label.setVisible(enabled)
            self.count_reconnect_dist.setVisible(enabled)
            self._reconnect_gap_label.setVisible(enabled)
            self.count_reconnect_gap.setVisible(enabled)

        apply_reconnect_visibility(self.count_reconnect_enabled.isChecked())
        self.count_reconnect_enabled.toggled.connect(apply_reconnect_visibility)

        def apply_extrap_visibility(enabled: bool) -> None:
            self._extrap_label.setVisible(enabled)
            self.count_extrap_horizon.setVisible(enabled)

        apply_extrap_visibility(self.count_extrap_enabled.isChecked())
        self.count_extrap_enabled.toggled.connect(apply_extrap_visibility)

        box.setLayout(layout)
        return box

    def _buttons_row(self) -> QHBoxLayout:
        detect_btn = QPushButton("탐지/궤적 저장 (DB)")
        detect_btn.clicked.connect(self.on_run)
        traj_btn = QPushButton("궤적 보기")
        traj_btn.clicked.connect(self.on_show_trajectories)

        count_btn = QPushButton("교차로 카운팅 시작")
        count_btn.clicked.connect(self.on_count)
        approach_btn = QPushButton("접근로 카운팅 시작")
        approach_btn.clicked.connect(self.on_count_approach)
        preview_btn = QPushButton("영상에서 이미지추출")
        preview_btn.clicked.connect(self.on_extract_images)
        quit_btn = QPushButton("프로그램 종료")
        quit_btn.setStyleSheet("QPushButton { color: #ff4d4d; font-weight: 800; }")
        quit_btn.clicked.connect(self.on_quit_program)
        save_btn = QPushButton("설정 저장")
        save_btn.clicked.connect(self._save_config)

        btns = QHBoxLayout()
        btns.addWidget(detect_btn)
        btns.addWidget(traj_btn)
        btns.addWidget(count_btn)
        btns.addWidget(approach_btn)
        btns.addWidget(save_btn)
        btns.addWidget(preview_btn)
        btns.addWidget(quit_btn)
        btns.addStretch()
        return btns

    def on_extract_images(self) -> None:
        """
        UI 설정 기반으로 영상에서 프레임을 샘플링해 이미지로 저장.
        - 추출 FPS = (분석 설정 FPS) / 10
        - 저장 위치 = 결과 저장폴더/images
        - 파일명 = 교차로명_세션명_000001.jpg ...
        """
        try:
            import cv2  # local import (env may vary)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "OpenCV 없음", f"cv2를 불러올 수 없습니다:\n{exc}")
            return

        video = self.video_path or self.video_path2 or self.video_path3
        if not video:
            QMessageBox.warning(self, "영상 없음", "영상 파일을 선택하세요.")
            return
        if not Path(video).exists():
            QMessageBox.warning(self, "영상 없음", f"영상 파일을 찾을 수 없습니다:\n{video}")
            return

        out_dir_txt = ""
        if hasattr(self, "count_output_dir_input"):
            out_dir_txt = self.count_output_dir_input.text().strip()
        if not out_dir_txt:
            QMessageBox.warning(self, "저장 폴더 없음", "결과 저장폴더를 먼저 선택하세요.")
            return
        out_dir = Path(out_dir_txt).expanduser()
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "폴더 생성 실패", f"결과 저장폴더를 만들 수 없습니다:\n{out_dir}\n{exc}")
            return
        images_dir = out_dir / "images"
        images_dir.mkdir(parents=True, exist_ok=True)

        try:
            import re
        except Exception:
            re = None  # type: ignore

        junction_raw = self.junction_input.text().strip() if hasattr(self, "junction_input") else ""
        session_raw = self.session_input.text().strip() if hasattr(self, "session_input") else ""
        junction_safe = self._safe_filename(junction_raw) if junction_raw else ""
        session_safe = self._safe_filename(session_raw) if session_raw else ""
        if re is not None:
            junction_safe = re.sub(r"\s+", "_", junction_safe)
            session_safe = re.sub(r"\s+", "_", session_safe)
        if junction_safe and session_safe:
            prefix = f"{junction_safe}_{session_safe}"
        else:
            prefix = junction_safe or session_safe or "junction"

        try:
            target_fps = float(self.fps_input.value()) if hasattr(self, "fps_input") else 10.0
        except Exception:
            target_fps = 10.0
        extract_fps = max(0.1, target_fps / 10.0)

        cap = cv2.VideoCapture(str(video))
        if not cap.isOpened():
            QMessageBox.critical(self, "영상 열기 실패", f"영상을 열 수 없습니다:\n{video}")
            return
        src_fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
        if not src_fps or src_fps <= 0:
            src_fps = 30.0
        step = max(1, int(round(src_fps / extract_fps)))

        # 기존 파일이 있으면 다음 번호부터 이어서 저장
        start_idx = 1
        try:
            import re

            pat = re.compile(rf"^{re.escape(prefix)}_(\d{{6}})\.jpg$", re.IGNORECASE)
            max_idx = 0
            for p in images_dir.glob(f"{prefix}_*.jpg"):
                m = pat.match(p.name)
                if not m:
                    continue
                try:
                    max_idx = max(max_idx, int(m.group(1)))
                except Exception:
                    continue
            if max_idx > 0:
                start_idx = max_idx + 1
        except Exception:
            pass

        def log(msg: str) -> None:
            try:
                if self.log_view:
                    self.log_view.append(str(msg))
                app = QApplication.instance()
                if app is not None:
                    app.processEvents()
            except Exception:
                return

        log(
            f"[extract] video={video} src_fps={src_fps:.2f} target_fps={target_fps:.2f} extract_fps={extract_fps:.2f} step={step}"
        )
        log(f"[extract] out_dir={images_dir}")

        frame_id = 0
        saved = 0
        img_idx = start_idx
        try:
            while True:
                ok, frame = cap.read()
                if not ok or frame is None:
                    break
                if frame_id % step == 0:
                    out_path = images_dir / f"{prefix}_{img_idx:06d}.jpg"
                    ok2 = bool(cv2.imwrite(str(out_path), frame))
                    if not ok2:
                        log(f"[extract][warn] 저장 실패: {out_path}")
                    saved += 1
                    img_idx += 1
                frame_id += 1
                if frame_id % 2000 == 0:
                    log(f"[extract] frame={frame_id} saved={saved}")
        finally:
            cap.release()
        log(f"[extract] done. saved={saved}")

    def _log_box(self) -> QGroupBox:
        box = QGroupBox("로그")
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        layout = QVBoxLayout()
        layout.addWidget(self.log_view)
        box.setLayout(layout)
        return box

    def _apply_style(self) -> None:
        style = """
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
            /* 숫자/선택 박스가 너무 좁아 글자가 눌려 보이는 문제 완화 */
            QSpinBox, QDoubleSpinBox {
                min-width: 96px;
            }
            QComboBox {
                min-width: 120px;
            }
            /* 콤보박스 화살표는 QSS에서 image 지정 시(특히 한글/공백 경로) 로드 문제가 잦아
               기본 스타일 렌더링을 그대로 사용한다. */
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
            """
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(style)
        else:
            self.setStyleSheet(style)

    def _choose_model(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "YOLO 모델 선택",
            str(Path("models").resolve()),
            "Model Files (*.pt *.onnx);;All Files (*)",
        )
        if path:
            self.model_path_input.setText(path)

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
        path = QFileDialog.getExistingDirectory(self, "결과 저장폴더 선택", str(start_dir))
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
            self._refresh_db_sessions()

    def _refresh_db_sessions(self) -> None:
        if not hasattr(self, "count_session_combo"):
            return
        try:
            db_path = Path(self.count_db_input.text().strip() or "output/tracks.sqlite")
            if not db_path.exists():
                return
            import sqlite3

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
                pass
            self.count_session_combo.blockSignals(False)
        except Exception:
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
            related_preview = f"\n\n관련 파일 삭제 대상({len(related_paths)}개):\n{related_preview}"
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
            import sqlite3

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
                    pass
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
            viewer = TrajectoryViewerWindow(
                db_path,
                lines_path=lines_path if lines_path.exists() else None,
                video_path=video_path,
                resize=None,
                session_id=session_id,
                config_path=self.cfg_path,
            )
            viewer.show()
            viewer.raise_()
            self._viewers.append(viewer)
        except Exception as exc:  # noqa: BLE001
            if self.log_view:
                self.log_view.append(f"궤적 보기 오류: {exc}")
            QMessageBox.critical(self, "궤적 보기 오류", str(exc))

    def _build_count_overrides(self) -> Dict:
        reconnect_enabled = bool(getattr(self, "count_reconnect_enabled", None) and self.count_reconnect_enabled.isChecked())
        return {
            "count_db_path": self.count_db_input.text().strip(),
            "count_output_dir": self.count_output_dir_input.text().strip() if hasattr(self, "count_output_dir_input") else "",
            "count_lines_path": self.count_lines_input.text().strip(),
            "count_interval_minutes": int(self.count_interval_combo.currentText()),
            "count_reconnect_enabled": reconnect_enabled,
            "count_reconnect_dist": float(self.count_reconnect_dist.value()),
            "count_reconnect_gap": float(self.count_reconnect_gap.value()),
            "count_reconnect_passes": int(self.count_reconnect_passes.value()) if reconnect_enabled else 0,
            "count_extrap_enabled": bool(self.count_extrap_enabled.isChecked()),
            "count_extrap_horizon": float(self.count_extrap_horizon.value()) if self.count_extrap_enabled.isChecked() else 0.0,
        }

    def _build_overrides(self) -> Dict:
        """탐지/추적(궤적 저장) 설정을 수집."""
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
            "model_path": self.model_path_input.text().strip(),
            "yolo_version": self.yolo_combo.currentText(),
            "tracker_config": tracker_config,
            "flush_interval_minutes": int(self.flush_combo.currentText()),
            "max_idle_frames": int(self.max_idle_frames_spin.value()),
        }

    def _save_config(self) -> None:
        try:
            cfg = load_app_config(self.cfg_path)
            cfg.update(self._build_overrides())
            cfg.update(self._build_count_overrides())
            save_app_config(self.cfg_path, cfg)
            # colab 실행 설정도 동일하게 반영
            for alt in [Path("colab/app_config_drive.json"), Path("colab/app_config_colab.json")]:
                try:
                    alt.parent.mkdir(parents=True, exist_ok=True)
                    save_app_config(alt, cfg)
                except Exception:
                    pass
            if self.log_view:
                self.log_view.append("설정을 저장했습니다.")
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "저장 실패", str(exc))

    def on_count(self) -> None:
        overrides_count = self._build_count_overrides()
        db_path = Path(overrides_count["count_db_path"] or "output/tracks.sqlite")
        lines_path = Path(overrides_count["count_lines_path"] or "config/lines.json")
        if not db_path.exists():
            QMessageBox.warning(self, "DB 없음", f"DB 파일을 찾을 수 없습니다:\n{db_path}")
            return
        if not lines_path.exists():
            QMessageBox.warning(self, "라인 JSON 없음", f"lines.json 파일을 찾을 수 없습니다:\n{lines_path}")
            return
        try:
            self.log_view.append(f"[count] db={db_path}")
            self.log_view.append(f"[count] lines={lines_path}")
            session_sel = None
            if hasattr(self, "count_session_combo"):
                sel = self.count_session_combo.currentText().strip()
                session_sel = None if sel in ("", "(전체)") else sel
            if session_sel:
                self.log_view.append(f"[count] session_id={session_sel}")
            session_name = session_sel or self._compose_session_id() or "counts"
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            out_dir = Path(overrides_count.get("count_output_dir") or "").expanduser()
            if not str(out_dir).strip():
                out_dir = db_path.parent
            out_dir.mkdir(parents=True, exist_ok=True)
            out_xlsx = out_dir / f"{self._safe_filename(session_name)}_교차로_{stamp}.xlsx"
            out, counts_df = run_count(
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
                mode="turn",
                log_cb=self.log_view.append,
            )
            self.log_view.append(f"카운팅 완료: {out}")
            try:
                self.log_view.append(f"행 개수: {len(counts_df)}")
            except Exception:
                pass
        except Exception as exc:  # noqa: BLE001
            self.log_view.append("[count] ERROR")
            self.log_view.append(traceback.format_exc())
            QMessageBox.critical(self, "카운팅 실패", str(exc))

    def on_count_approach(self) -> None:
        overrides_count = self._build_count_overrides()
        db_path = Path(overrides_count["count_db_path"] or "output/tracks.sqlite")
        lines_path = Path(overrides_count["count_lines_path"] or "config/lines.json")
        if not db_path.exists():
            QMessageBox.warning(self, "DB 없음", f"DB 파일을 찾을 수 없습니다:\n{db_path}")
            return
        if not lines_path.exists():
            QMessageBox.warning(self, "라인 JSON 없음", f"lines.json 파일을 찾을 수 없습니다:\n{lines_path}")
            return
        try:
            self.log_view.append(f"[approach] db={db_path}")
            self.log_view.append(f"[approach] lines={lines_path}")
            session_sel = None
            if hasattr(self, "count_session_combo"):
                sel = self.count_session_combo.currentText().strip()
                session_sel = None if sel in ("", "(전체)") else sel
            if session_sel:
                self.log_view.append(f"[approach] session_id={session_sel}")
            session_name = session_sel or self._compose_session_id() or "counts"
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            out_dir = Path(overrides_count.get("count_output_dir") or "").expanduser()
            if not str(out_dir).strip():
                out_dir = db_path.parent
            out_dir.mkdir(parents=True, exist_ok=True)
            out_xlsx = out_dir / f"{self._safe_filename(session_name)}_접근로_{stamp}.xlsx"
            out, counts_df = run_count(
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
                mode="approach",
                log_cb=self.log_view.append,
            )
            self.log_view.append(f"접근로 카운팅 완료: {out}")
            try:
                self.log_view.append(f"행 개수: {len(counts_df)}")
            except Exception:
                pass
        except Exception as exc:  # noqa: BLE001
            self.log_view.append("[approach] ERROR")
            self.log_view.append(traceback.format_exc())
            QMessageBox.critical(self, "접근로 카운팅 실패", str(exc))

    def on_run(self) -> None:
        if self.worker and self.worker.isRunning():
            QMessageBox.information(self, "실행 중", "파이프라인이 이미 실행 중입니다.")
            return

        video = self.video_path or self.video_path2 or self.video_path3
        if not video:
            QMessageBox.warning(self, "영상 없음", "영상 파일을 선택하세요.")
            return

        overrides = self._build_overrides()
        if not overrides.get("model_path"):
            QMessageBox.warning(self, "모델 경로 없음", "모델 경로를 설정하세요.")
            return
        if not Path(overrides["model_path"]).exists():
            QMessageBox.critical(self, "모델 파일 없음", f"모델 파일을 찾을 수 없습니다:\n{overrides['model_path']}")
            return

        self.log_view.append("파이프라인 시작...")
        self.log_view.append(f"모델 경로: {overrides['model_path']}")
        self.worker = PipelineWorker(video, self.cfg_path, self.line_settings_path, overrides)
        self.worker.log_message.connect(self._append_log, Qt.ConnectionType.QueuedConnection)
        self.worker.finished_ok.connect(self._on_pipeline_done, Qt.ConnectionType.QueuedConnection)
        self.worker.failed.connect(self.on_worker_failed, Qt.ConnectionType.QueuedConnection)
        self.worker.start()

    def on_worker_failed(self, message: str) -> None:
        self.log_view.append(f"실패: {message}")
        QMessageBox.critical(self, "Error", message)

    def on_quit_program(self) -> None:
        # 종료 시 현재 설정을 저장하고, 실행 중인 워커가 있으면 안전하게 정지 시도
        try:
            self._save_config()
        except Exception:
            pass
        try:
            self._stop_worker(wait_ms=5000)
        except Exception:
            pass
        app = QApplication.instance()
        if app is not None:
            app.quit()
        else:
            self.close()


def main() -> None:
    app = QApplication(sys.argv)

    # 전체 UI 글자 크기 약 1.2배 확대
    try:
        base = app.font()
        pt = float(base.pointSizeF() if base.pointSizeF() > 0 else base.pointSize())
        if pt <= 0:
            pt = 10.0
        base.setPointSizeF(pt * UI_SCALE)
        app.setFont(base)
    except Exception:
        pass

    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
