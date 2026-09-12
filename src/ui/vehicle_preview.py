"""차종 인식 미리보기 창."""

import logging
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QImage, QPixmap, QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

logger = logging.getLogger(__name__)


class VehiclePreviewWindow(QWidget):
    def __init__(self, video_path: Path, model_path: Path | str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle("차종 인식 미리보기")
        # Ensure it's a top-level window and not hidden behind the main window.
        try:
            self.setWindowFlag(Qt.Window, True)
        except Exception:
            logger.debug("Suppressed error", exc_info=True)
        self.video_path = video_path
        self.model_path = str(model_path)
        self._paused = False
        self._frame_count = 0
        self._skip_frame = 2
        self._cap = None
        self._model = None
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._process_frame)

        self._class_colors = {
            1: (0, 255, 0),
            2: (255, 0, 0),
            3: (0, 0, 255),
            4: (255, 255, 0),
            5: (255, 0, 255),
            6: (0, 255, 255),
            7: (128, 0, 128),
        }

        self._build_ui()
        # Give the window a reasonable default size and center it.
        try:
            self.resize(1600, 900)
            screen = QGuiApplication.primaryScreen()
            if screen is not None:
                geo = screen.availableGeometry()
                self.move(
                    int(geo.x() + (geo.width() - self.width()) / 2),
                    int(geo.y() + (geo.height() - self.height()) / 2),
                )
        except Exception:
            logger.debug("Suppressed error", exc_info=True)
        self._init_pipeline()

    def _build_ui(self) -> None:
        layout = QVBoxLayout()
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        self.video_label = QLabel("영상 로딩 중...")
        self.video_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.video_label.setMinimumSize(900, 520)
        self.video_label.setStyleSheet("QLabel { background: #121721; color: #dfe7f3; border: 1px solid #2b3342; }")
        layout.addWidget(self.video_label, stretch=1)

        btn_row = QHBoxLayout()
        self.pause_btn = QPushButton("일시정지")
        self.pause_btn.clicked.connect(self._pause)
        self.start_btn = QPushButton("재생")
        self.start_btn.clicked.connect(self._resume)
        self.exit_btn = QPushButton("닫기")
        self.exit_btn.setStyleSheet("QPushButton { color: #ffcf5b; font-weight: 800; }")
        self.exit_btn.clicked.connect(self.close)

        btn_row.addWidget(self.pause_btn)
        btn_row.addWidget(self.start_btn)
        self.save_checkbox = QCheckBox("결과 영상 저장")
        self.save_checkbox.setChecked(False)
        btn_row.addWidget(self.save_checkbox)
        btn_row.addStretch()
        btn_row.addWidget(self.exit_btn)
        layout.addLayout(btn_row)

        self.setLayout(layout)

    def _init_pipeline(self) -> None:
        try:
            import cv2  # local import (env may vary)
            from ultralytics import YOLO
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "미리보기 실패", f"필수 라이브러리를 불러올 수 없습니다:\n{exc}")
            self.close()
            return

        self._cap = cv2.VideoCapture(str(self.video_path))
        if not self._cap.isOpened():
            QMessageBox.critical(self, "미리보기 실패", f"영상을 열 수 없습니다:\n{self.video_path}")
            self.close()
            return

        try:
            self._model = YOLO(str(self.model_path))
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "미리보기 실패", f"모델을 불러올 수 없습니다:\n{self.model_path}\n{exc}")
            self.close()
            return

        self._fps = self._cap.get(cv2.CAP_PROP_FPS) or 0.0
        if not self._fps or self._fps <= 0:
            self._fps = 30.0

        original_w = int(self._cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        original_h = int(self._cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        if original_w <= 0 or original_h <= 0:
            original_w, original_h = 1280, 720
        self._disp_w = 1500
        self._disp_h = int(self._disp_w * original_h / original_w)
        self.video_label.setMinimumSize(self._disp_w, self._disp_h)
        self._writer = None

        interval_ms = max(1, int(1000 / self._fps))
        self._timer.start(interval_ms)

    def _pause(self) -> None:
        self._paused = True

    def _resume(self) -> None:
        self._paused = False

    def _get_color(self, cls_id: int):
        return self._class_colors.get(cls_id, (255, 255, 255))

    def _process_frame(self) -> None:
        if self._paused or self._cap is None or self._model is None:
            return
        import cv2  # already validated in _init_pipeline

        success, frame = self._cap.read()
        if not success or frame is None:
            self._timer.stop()
            self.video_label.setText("영상이 종료되었습니다.")
            return

        self._frame_count += 1
        if self._frame_count % self._skip_frame != 0:
            return

        try:
            results = self._model.predict(frame, conf=0.4, device=0, verbose=False)
        except Exception:
            results = self._model.predict(frame, conf=0.4, verbose=False)

        if results and results[0].boxes is not None:
            try:
                mask = results[0].boxes.cls != 0
                results[0].boxes = results[0].boxes[mask]
            except Exception:
                logger.debug("Suppressed error", exc_info=True)

        annotated = frame.copy()
        if results and results[0].boxes is not None:
            for box in results[0].boxes:
                x1, y1, x2, y2 = map(int, box.xyxy[0])
                cls_id = int(box.cls[0])
                conf = float(box.conf[0])
                color = self._get_color(cls_id)
                cv2.rectangle(annotated, (x1, y1), (x2, y2), color, 2)
                label = f"ID:{cls_id} {conf:.2f}"
                cv2.putText(annotated, label, (x1, max(0, y1 - 10)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 2)

        annotated = cv2.resize(annotated, (self._disp_w, self._disp_h))
        if self.save_checkbox.isChecked():
            if self._writer is None:
                fourcc = cv2.VideoWriter_fourcc(*"mp4v")
                out_path = Path("output_result.mp4")
                self._writer = cv2.VideoWriter(str(out_path), fourcc, float(self._fps), (self._disp_w, self._disp_h))
            if self._writer is not None:
                self._writer.write(annotated)
        else:
            if self._writer is not None:
                try:
                    self._writer.release()
                except Exception:
                    logger.debug("Suppressed error", exc_info=True)
                self._writer = None
        rgb = cv2.cvtColor(annotated, cv2.COLOR_BGR2RGB)
        h, w, ch = rgb.shape
        bytes_per_line = ch * w
        qimg = QImage(rgb.data, w, h, bytes_per_line, QImage.Format.Format_RGB888)
        pixmap = QPixmap.fromImage(qimg)
        self.video_label.setPixmap(pixmap)

    def closeEvent(self, event) -> None:
        try:
            self._timer.stop()
        except Exception:
            logger.debug("Suppressed error", exc_info=True)
        try:
            if self._cap is not None:
                self._cap.release()
        except Exception:
            logger.debug("Suppressed error", exc_info=True)
        try:
            if self._writer is not None:
                self._writer.release()
        except Exception:
            logger.debug("Suppressed error", exc_info=True)
        super().closeEvent(event)
