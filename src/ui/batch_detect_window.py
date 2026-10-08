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

from src.config.model_profiles import list_model_files
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

COL_VIDEO, COL_JUNCTION, COL_SESSION, COL_DB, COL_MODEL, COL_IMGSZ, COL_STATUS = range(7)
HEADERS = ["영상", "교차로명", "세션명", "저장 DB (자동)", "모델", "이미지 크기", "상태"]
CURRENT = "(현재 설정)"
IMGSZ_CHOICES = [CURRENT, "auto", "640", "960", "1280", "1920"]
VIDEO_FILTER = "영상 (*.mp4 *.avi *.mkv *.mov *.m4v *.ts);;모든 파일 (*)"
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
                 is_busy_fn: Callable[[], bool] = lambda: False, parent=None):
        super().__init__(parent)
        self.setWindowTitle("일괄 탐지 (객체 탐지 + 궤적 저장)")
        self.setStyleSheet(DARK_DIALOG_STYLE)
        self.cfg_path = cfg_path
        self.base_overrides_fn = base_overrides_fn
        self.is_busy_fn = is_busy_fn
        self.worker: Optional[BatchWorker] = None
        self.frame_totals: Dict[int, int] = {}
        self._build_ui()
        fit_to_screen(self, 1250, 650)

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "영상 이름은 <교차로명>_<세션명> (예: 경원교차로_오전첨두.mp4). 교차로마다 DB 하나, 영상마다 세션 하나로 저장합니다.\n"
            "분석라인 없이 화면 전체를 탐지합니다. 모델·이미지 크기는 기본으로 메인 창 설정을 따르며 영상별로 바꿀 수 있습니다."))

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
        header.setSectionResizeMode(COL_VIDEO, QHeaderView.ResizeMode.Stretch)
        for col in (COL_JUNCTION, COL_SESSION, COL_DB, COL_MODEL, COL_IMGSZ, COL_STATUS):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
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
        for w in (self.add_btn, self.remove_btn, self.up_btn, self.down_btn, self.start_btn, self.export_btn):
            w.setEnabled(not running)
        self.after_btn.setEnabled(running)
        self.stop_btn.setEnabled(running)
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers if running
            else QAbstractItemView.EditTrigger.DoubleClicked | QAbstractItemView.EditTrigger.EditKeyPressed)
        for r in range(self.table.rowCount()):
            for col in (COL_MODEL, COL_IMGSZ):
                widget = self.table.cellWidget(r, col)
                if widget is not None:
                    widget.setEnabled(not running)

    # ------------------------------------------------------------------ 표 조작
    def _model_combo(self) -> QComboBox:
        combo = QComboBox()
        combo.addItem(CURRENT, "")
        for path in list_model_files("models"):
            combo.addItem(path.name, str(path))
        return combo

    def _imgsz_combo(self) -> QComboBox:
        combo = QComboBox()
        for text in IMGSZ_CHOICES:
            combo.addItem(text, "" if text == CURRENT else text)
        return combo

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
        for col in (COL_DB, COL_STATUS):
            cell = QTableWidgetItem("")
            cell.setFlags(cell.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(row, col, cell)
        self.table.setCellWidget(row, COL_MODEL, self._model_combo())
        self.table.setCellWidget(row, COL_IMGSZ, self._imgsz_combo())
        self.table.blockSignals(False)
        self._refresh_row(row)

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
        return BatchJob(video, junction, session, model, imgsz)

    def on_add(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "탐지할 영상 선택", "", VIDEO_FILTER)
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
            "model": self.table.cellWidget(row, COL_MODEL).currentIndex(),
            "imgsz": self.table.cellWidget(row, COL_IMGSZ).currentIndex(),
        }

    def _restore_row(self, snap: Dict) -> None:
        self.add_video(snap["video"])
        row = self.table.rowCount() - 1
        self.table.blockSignals(True)
        self.table.item(row, COL_JUNCTION).setText(snap["junction"])
        self.table.item(row, COL_SESSION).setText(snap["session"])
        self.table.blockSignals(False)
        self.table.cellWidget(row, COL_MODEL).setCurrentIndex(snap["model"])
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
                self.table.item(row, COL_STATUS).setText(f"▶ {min(100, 100 * int(m.group(1)) // total)}%")
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
