"""백그라운드 QThread 워커: 탐지/추적, 카운팅."""

import logging
from pathlib import Path
from typing import Dict, Optional

from PySide6.QtCore import QThread, Signal

from src.pipeline.count_tracks import run_count
from src.services.detection_service import DetectionRequest, DetectionService

logger = logging.getLogger(__name__)


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
            result = DetectionService().run(
                DetectionRequest(
                    cfg_path=self.cfg_path,
                    video_path=self.video_path,
                    line_path=self.line_path,
                    overrides=self.overrides,
                    session_id=str(self.overrides.get("session_id") or "").strip() or None,
                ),
                progress_cb=self.log_message.emit,
                should_stop_cb=self.isInterruptionRequested,
            )
            if result.stopped:
                self.failed.emit("중단됨")
            else:
                self.finished_ok.emit()
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))


class CountWorker(QThread):
    """백그라운드에서 카운팅을 실행하여 GUI 멈춤을 방지한다."""
    log_message = Signal(str)
    finished_ok = Signal(str, str)  # (label, output_path)
    failed = Signal(str, str)       # (label, error_message)

    def __init__(self, kwargs: Dict, mode: str, label: str):
        super().__init__()
        self._kwargs = kwargs
        self._mode = mode
        self._label = label

    def run(self) -> None:
        try:
            out, counts_df = run_count(
                log_cb=self.log_message.emit,
                mode=self._mode,
                **self._kwargs,
            )
            count_info = ""
            try:
                count_info = f"\n집계 건수: {len(counts_df)}"
            except Exception:
                logger.debug("Suppressed error", exc_info=True)
            self.finished_ok.emit(self._label, f"{out}{count_info}")
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(self._label, str(exc))
