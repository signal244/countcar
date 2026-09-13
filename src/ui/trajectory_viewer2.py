import logging
import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import re
from typing import Dict, List, Optional, Tuple

import cv2
from PySide6.QtCore import QObject, QPointF, QRectF, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QColor, QImage, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGraphicsEllipseItem,
    QGraphicsLineItem,
    QGraphicsPathItem,
    QGraphicsPixmapItem,
    QGraphicsScene,
    QGraphicsTextItem,
    QGraphicsView,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSpacerItem,
    QSpinBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)
from shiboken6 import isValid

from src.config.loader import load_app_config, save_app_config
from src.db.schema import init_db
from src.db.writer import decode_traj
from src.ui.db_session_viewer import DbSessionViewerDialog
from src.ui.widgets import fit_to_screen, wrap_in_scroll
from src.pipeline.track_merge import load_effective_track_merge_map, save_manual_track_merge

VIEWER_EXTRAP_OVERSHOOT_PX = 20.0



logger = logging.getLogger(__name__)

def _segment_speed(a_xy: Tuple[float, float], a_ts: float, b_xy: Tuple[float, float], b_ts: float) -> float:
    dt = float(b_ts) - float(a_ts)
    if dt <= 1e-6:
        return 0.0
    return float(math.hypot(float(b_xy[0]) - float(a_xy[0]), float(b_xy[1]) - float(a_xy[1]))) / dt


def _project_point(
    pt: Tuple[float, float],
    direction: Tuple[float, float],
    distance: float,
) -> Tuple[float, float]:
    return (
        float(pt[0]) + (float(direction[0]) * float(distance)),
        float(pt[1]) + (float(direction[1]) * float(distance)),
    )


def _extend_hit_endpoint(
    start: Tuple[float, float],
    hit: Tuple[float, float],
    extra_px: float = VIEWER_EXTRAP_OVERSHOOT_PX,
) -> Tuple[float, float]:
    dx = float(hit[0]) - float(start[0])
    dy = float(hit[1]) - float(start[1])
    norm = float(math.hypot(dx, dy))
    if norm <= 1e-6 or extra_px <= 0.0:
        return (float(hit[0]), float(hit[1]))
    scale = float(extra_px) / norm
    return (
        float(hit[0]) + (dx * scale),
        float(hit[1]) + (dy * scale),
    )


def _unit_direction(a_xy: Tuple[float, float], b_xy: Tuple[float, float]) -> Optional[Tuple[float, float]]:
    vx = float(b_xy[0]) - float(a_xy[0])
    vy = float(b_xy[1]) - float(a_xy[1])
    norm = float(math.hypot(vx, vy))
    if norm <= 1e-6:
        return None
    return (vx / norm, vy / norm)


def _effective_merge_distance(
    end_pair: Optional[Tuple[Tuple[float, float], Tuple[float, float], float, float]],
    start_pair: Optional[Tuple[Tuple[float, float], Tuple[float, float], float, float]],
    gap_sec: float,
) -> float:
    if end_pair is None or start_pair is None:
        return float("inf")
    prev_xy, end_xy, prev_ts, end_ts = end_pair
    start_xy, next_xy, start_ts, next_ts = start_pair
    raw_dist = float(math.hypot(float(end_xy[0]) - float(start_xy[0]), float(end_xy[1]) - float(start_xy[1])))
    if gap_sec <= 1e-6:
        return raw_dist
    dists = [raw_dist]
    tail_dir = _unit_direction(prev_xy, end_xy)
    if tail_dir is not None:
        tail_speed = _segment_speed(prev_xy, prev_ts, end_xy, end_ts)
        pred = _project_point(end_xy, tail_dir, tail_speed * gap_sec)
        dists.append(float(math.hypot(float(pred[0]) - float(start_xy[0]), float(pred[1]) - float(start_xy[1]))))
    head_dir = _unit_direction(start_xy, next_xy)
    if head_dir is not None:
        back = _project_point(start_xy, (-float(head_dir[0]), -float(head_dir[1])), _segment_speed(start_xy, start_ts, next_xy, next_ts) * gap_sec)
        dists.append(float(math.hypot(float(end_xy[0]) - float(back[0]), float(end_xy[1]) - float(back[1]))))
        if tail_dir is not None:
            pred = _project_point(end_xy, tail_dir, _segment_speed(prev_xy, prev_ts, end_xy, end_ts) * gap_sec)
            dists.append(float(math.hypot(float(pred[0]) - float(back[0]), float(pred[1]) - float(back[1]))))
    dx = float(start_xy[0]) - float(end_xy[0])
    dy = float(start_xy[1]) - float(end_xy[1])
    if tail_dir is not None:
        tail_speed = _segment_speed(prev_xy, prev_ts, end_xy, end_ts)
        along = (dx * float(tail_dir[0])) + (dy * float(tail_dir[1]))
        lateral = abs((dx * float(tail_dir[1])) - (dy * float(tail_dir[0])))
        dists.append(lateral + (0.25 * abs(along - (tail_speed * gap_sec))))
    if head_dir is not None:
        head_speed = _segment_speed(start_xy, start_ts, next_xy, next_ts)
        along = (dx * float(head_dir[0])) + (dy * float(head_dir[1]))
        lateral = abs((dx * float(head_dir[1])) - (dy * float(head_dir[0])))
        dists.append(lateral + (0.25 * abs(along - (head_speed * gap_sec))))
    return min(dists)


def _dist_point_to_segment(pt: Tuple[float, float], a: Tuple[float, float], b: Tuple[float, float]) -> float:
    ax, ay = float(a[0]), float(a[1])
    bx, by = float(b[0]), float(b[1])
    px, py = float(pt[0]), float(pt[1])
    abx = bx - ax
    aby = by - ay
    denom = (abx * abx) + (aby * aby)
    if denom <= 1e-9:
        return float(math.hypot(px - ax, py - ay))
    t = ((px - ax) * abx + (py - ay) * aby) / denom
    t = max(0.0, min(1.0, t))
    qx = ax + (t * abx)
    qy = ay + (t * aby)
    return float(math.hypot(px - qx, py - qy))


def _point_to_line_distance(pt: Tuple[float, float], line: object) -> float:
    if not line or not getattr(line, "points", None):
        return float("inf")
    pts: List[Tuple[float, float]] = []
    for raw in line.points:
        try:
            pts.append((float(raw[0]), float(raw[1])))
        except Exception:
            continue
    if len(pts) < 2:
        return float("inf")
    return min(_dist_point_to_segment(pt, pts[i], pts[i + 1]) for i in range(len(pts) - 1))


def _nearest_point_on_segment(
    pt: Tuple[float, float],
    a: Tuple[float, float],
    b: Tuple[float, float],
) -> Tuple[float, float, float]:
    ax, ay = float(a[0]), float(a[1])
    bx, by = float(b[0]), float(b[1])
    px, py = float(pt[0]), float(pt[1])
    abx = bx - ax
    aby = by - ay
    denom = (abx * abx) + (aby * aby)
    if denom <= 1e-9:
        return ax, ay, 0.0
    t = ((px - ax) * abx + (py - ay) * aby) / denom
    t = max(0.0, min(1.0, t))
    qx = ax + (t * abx)
    qy = ay + (t * aby)
    return float(qx), float(qy), float(t)


@dataclass
class LineDef:
    line_id: str
    points: List[List[float]]
    bound: str = ""
    in_point: Optional[List[float]] = None

    def to_json(self) -> Dict:
        payload = {"id": self.line_id, "points": self.points, "bound": self.bound}
        if self.in_point and len(self.in_point) >= 2:
            payload["in_point"] = [float(self.in_point[0]), float(self.in_point[1])]
        return payload


DARK_DIALOG_STYLE = """
QDialog {
    background-color: #232b37;
}
QDialog QLabel {
    color: #eef4ff;
    background: transparent;
    font-weight: 600;
}
QDialog QLineEdit,
QDialog QSpinBox,
QDialog QDoubleSpinBox,
QDialog QComboBox,
QDialog QListWidget {
    background: #2f3948;
    color: #f4f7ff;
    border: 1px solid #49566b;
    border-radius: 4px;
    padding: 4px 8px;
}
QDialog QPushButton {
    background-color: #446b9e;
    color: #f4f7ff;
    border: 1px solid #31547e;
    border-radius: 4px;
    padding: 5px 12px;
    font-weight: 700;
    min-height: 28px;
}
QDialog QPushButton:hover {
    background-color: #5380bb;
}
QDialog QPushButton:pressed {
    background-color: #35597f;
}
"""


class MergeConfigDialog(QDialog):
    def __init__(self, parent=None, bounds: Optional[List[str]] = None):
        super().__init__(parent)
        self.setWindowTitle("병합 설정")
        self.resize(360, 260)
        self.setStyleSheet(DARK_DIALOG_STYLE)
        self.bounds = ["(라인 없음)"] + (bounds or [])
        layout = QVBoxLayout()
        form = QFormLayout()
        self.cb_src_line = QComboBox()
        self.cb_src_line.addItems(self.bounds)
        self.cb_tgt_line = QComboBox()
        self.cb_tgt_line.addItems(self.bounds)
        self.cb_movement_type = QComboBox()
        self.cb_movement_type.addItem("직진", "straight")
        self.cb_movement_type.addItem("좌회전", "left_turn")
        self.cb_movement_type.addItem("우회전", "right_turn")
        self.spin_dist = QDoubleSpinBox()
        self.spin_dist.setRange(0.0, 10000.0)
        self.spin_dist.setValue(50.0)
        self.spin_gap = QDoubleSpinBox()
        self.spin_gap.setRange(0.0, 120.0)
        self.spin_gap.setValue(3.0)
        self.spin_passes = QSpinBox()
        self.spin_passes.setRange(1, 10)
        self.spin_passes.setValue(2)
        form.addRow("출발선 선택:", self.cb_src_line)
        form.addRow("도착선 선택:", self.cb_tgt_line)
        form.addRow("movement 선택:", self.cb_movement_type)
        form.addRow("재연결 거리(px):", self.spin_dist)
        form.addRow("재연결 간격(s):", self.spin_gap)
        form.addRow("재연결 횟수:", self.spin_passes)
        layout.addLayout(form)
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)
        self.setLayout(layout)

    def get_config(self) -> Dict:
        return {
            "src_bound": self.cb_src_line.currentText(),
            "tgt_bound": self.cb_tgt_line.currentText(),
            "movement_type": str(self.cb_movement_type.currentData() or "straight"),
            "reconnect_dist": self.spin_dist.value(),
            "reconnect_gap": self.spin_gap.value(),
            "reconnect_passes": self.spin_passes.value(),
        }


class ExtrapConfigDialog(QDialog):
    def __init__(self, parent=None, bounds: Optional[List[str]] = None):
        super().__init__(parent)
        self.setWindowTitle("외삽 설정")
        self.resize(360, 190)
        self.setStyleSheet(DARK_DIALOG_STYLE)
        self.bounds = ["(라인 없음)"] + (bounds or [])
        layout = QVBoxLayout()
        form = QFormLayout()
        self.cb_target = QComboBox()
        self.cb_target.addItems(self.bounds)
        self.spin_horizon = QDoubleSpinBox()
        self.spin_horizon.setRange(0.0, 10000.0)
        self.spin_horizon.setValue(200.0)
        form.addRow("A 목표선 선택:", self.cb_target)
        form.addRow("외삽 길이(px):", self.spin_horizon)
        layout.addLayout(form)
        guide = QLabel("A 선을 기준으로 외삽한 뒤 B 선과 교차하는 지점을 찾습니다.")
        guide.setWordWrap(True)
        layout.addWidget(guide)
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)
        self.setLayout(layout)

    def get_config(self) -> Dict:
        return {"target_bound": self.cb_target.currentText(), "extrap_horizon": self.spin_horizon.value()}


class _AutoFitView(QGraphicsView):
    def __init__(self):
        super().__init__()
        self._fit_rect = None
        self._auto_fit = True
        self._click_cb = None
        self._zoom_cb = None
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setDragMode(QGraphicsView.DragMode.NoDrag)

    def set_fit_rect(self, rect) -> None:
        self._fit_rect = rect
        if rect is not None and self._auto_fit:
            self.fitInView(rect, Qt.KeepAspectRatioByExpanding)
            self._emit_zoom()

    def set_click_callback(self, cb) -> None:
        self._click_cb = cb

    def set_zoom_callback(self, cb) -> None:
        self._zoom_cb = cb

    def _emit_zoom(self) -> None:
        if self._zoom_cb is None:
            return
        try:
            self._zoom_cb(float(self.transform().m11()))
        except Exception:
            logger.debug("Suppressed error", exc_info=True)
            return

    def fit_to_rect(self) -> None:
        if self._fit_rect is None:
            return
        self._auto_fit = True
        self.fitInView(self._fit_rect, Qt.KeepAspectRatioByExpanding)
        self._emit_zoom()

    def reset_zoom(self) -> None:
        self._auto_fit = False
        self.resetTransform()
        self._emit_zoom()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._fit_rect is not None and self._auto_fit:
            self.fitInView(self._fit_rect, Qt.KeepAspectRatioByExpanding)
            self._emit_zoom()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.RightButton:
            self._auto_fit = False
            self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
            super().mousePressEvent(event)
            return
        if self._click_cb is None or event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        pos = self.mapToScene(event.pos())
        self._click_cb(pos.x(), pos.y())
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        super().mouseReleaseEvent(event)
        if event.button() == Qt.MouseButton.RightButton and self.dragMode() == QGraphicsView.DragMode.ScrollHandDrag:
            self.setDragMode(QGraphicsView.DragMode.NoDrag)

    def wheelEvent(self, event) -> None:
        delta = event.angleDelta().y()
        if delta == 0:
            super().wheelEvent(event)
            return
        self._auto_fit = False
        factor = 1.15 ** (delta / 120.0)
        self.scale(factor, factor)
        self._emit_zoom()
        event.accept()


def _load_sessions(db_path: Path) -> List[str]:
    if not db_path.exists():
        return []
    with sqlite3.connect(db_path) as conn:
        sessions: set[str] = set()
        for table in ("track_trajs", "tracks"):
            try:
                exists = conn.execute(
                    "select 1 from sqlite_master where type='table' and name=? limit 1", (table,)
                ).fetchone() is not None
            except Exception:
                exists = False
            if not exists:
                continue
            try:
                rows = conn.execute(
                    f"select distinct session_id from {table} where session_id is not null and session_id != '' order by session_id"
                ).fetchall()
                for row in rows:
                    if row and row[0]:
                        sessions.add(str(row[0]))
            except Exception:
                continue
    return sorted(sessions)


def _load_background(
    video_path: Optional[Path],
    resize_target: Optional[Tuple[int, int]],
) -> Tuple[Optional[QGraphicsPixmapItem], float, float]:
    video_path = _resolve_existing_path(video_path)
    if not video_path or not video_path.exists():
        return None, 0.0, 0.0
    cap = cv2.VideoCapture(str(video_path))
    ok, frame = cap.read()
    cap.release()
    if not ok or frame is None:
        return None, 0.0, 0.0
    if resize_target and resize_target[0] > 0 and resize_target[1] > 0:
        frame = cv2.resize(frame, resize_target)
    h, w = frame.shape[:2]
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    image = QImage(rgb.data, w, h, rgb.strides[0], QImage.Format.Format_RGB888).copy()
    item = QGraphicsPixmapItem(QPixmap.fromImage(image))
    item.setPos(0.0, 0.0)
    return item, float(w), float(h)


def _probe_video_size(video_path: Optional[Path]) -> Optional[Tuple[int, int]]:
    video_path = _resolve_existing_path(video_path)
    if not video_path or not Path(video_path).exists():
        return None
    try:
        cap = cv2.VideoCapture(str(video_path))
        if not cap.isOpened():
            cap.release()
            return None
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        cap.release()
        if w > 0 and h > 0:
            return w, h
    except Exception:
        logger.debug("Suppressed error", exc_info=True)
        return None
    return None


def _rescale_points(pts: List[List[float]], old_size: Tuple[int, int], new_size: Tuple[int, int]) -> List[List[float]]:
    ow, oh = float(old_size[0]), float(old_size[1])
    nw, nh = float(new_size[0]), float(new_size[1])
    if ow <= 0 or oh <= 0:
        return pts
    sx = nw / ow
    sy = nh / oh
    out: List[List[float]] = []
    for p in pts or []:
        if not isinstance(p, list) or len(p) < 2:
            continue
        try:
            out.append([float(p[0]) * sx, float(p[1]) * sy])
        except Exception:
            continue
    return out


def _read_lines(lines_path: Path) -> Tuple[List[LineDef], Optional[Tuple[int, int]]]:
    if not lines_path.exists():
        return [], None
    try:
        data = json.loads(lines_path.read_text(encoding="utf-8"))
        iw = data.get("image_width")
        ih = data.get("image_height")
        base = (int(iw), int(ih)) if iw and ih else None
        out: List[LineDef] = []
        for ln in data.get("lines", []):
            pts = ln.get("points") or []
            if len(pts) < 2:
                continue
            in_point = None
            raw_in_point = ln.get("in_point")
            if isinstance(raw_in_point, (list, tuple)) and len(raw_in_point) >= 2:
                try:
                    in_point = [float(raw_in_point[0]), float(raw_in_point[1])]
                except Exception:
                    in_point = None
            out.append(LineDef(
                line_id=str(ln.get("name") or ln.get("id") or "line"),
                points=[[float(p[0]), float(p[1])] for p in pts],
                bound=str(ln.get("bound") or ""),
                in_point=in_point,
            ))
        return out, base
    except Exception:
        logger.debug("Suppressed error", exc_info=True)
        return [], None


def _write_lines(lines_path: Path, lines: List[LineDef], image_size: Optional[Tuple[int, int]]) -> None:
    payload: Dict = {"line_set_id": "default", "lines": [ln.to_json() for ln in lines], "roi": []}
    if image_size:
        payload["image_width"] = int(image_size[0])
        payload["image_height"] = int(image_size[1])
    lines_path.parent.mkdir(parents=True, exist_ok=True)
    lines_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _resolve_existing_path(path_value: Optional[object]) -> Optional[Path]:
    text = str(path_value or "").strip()
    if not text:
        return None
    p = Path(text).expanduser()
    candidates = [p]
    try:
        candidates.append((Path.cwd() / p).resolve())
    except Exception:
        logger.debug("Suppressed error", exc_info=True)
    try:
        candidates.append((Path(__file__).resolve().parents[2] / p).resolve())
    except Exception:
        logger.debug("Suppressed error", exc_info=True)
    seen: set[str] = set()
    for cand in candidates:
        key = str(cand)
        if key in seen:
            continue
        seen.add(key)
        try:
            if cand.exists():
                return cand
        except Exception:
            continue
    return None


def _safe_filename_fragment(name: str) -> str:
    text = str(name or "").strip()
    if not text:
        return ""
    text = re.sub(r'[<>:"/\\|?*]', "_", text)
    text = re.sub(r"\s+", "_", text)
    return text[:120]


class _TrajectoryLoadWorker(QObject):
    batch_ready = Signal(object)
    finished = Signal(object)
    error = Signal(str)
    progress = Signal(str)

    def __init__(
        self,
        db_path: Path,
        session_id: Optional[str],
        slot_index: Optional[int],
        frame_step: int,
        max_tracks: int,
        token: int,
        filter_mode: Optional[Dict] = None,
        batch_size_tracks: int = 50,
    ) -> None:
        super().__init__()
        self.db_path = db_path
        self.session_id = session_id
        self.slot_index = slot_index
        self.frame_step = max(1, int(frame_step))
        self.max_tracks = max(0, int(max_tracks))
        self.token = int(token)
        self.filter_mode = filter_mode
        self.batch_size_tracks = max(10, int(batch_size_tracks))
        self._slot_interval_ms = 900_000

    def _interrupted(self) -> bool:
        try:
            return bool(QThread.currentThread().isInterruptionRequested())
        except Exception:
            logger.debug("Suppressed error", exc_info=True)
            return False

    def _intersects(self, a, b, c, d) -> bool:
        def ccw(p1, p2, p3):
            return (p3[1] - p1[1]) * (p2[0] - p1[0]) > (p2[1] - p1[1]) * (p3[0] - p1[0])
        return ccw(a, c, d) != ccw(b, c, d) and ccw(a, b, c) != ccw(a, b, d)

    def _passes_line(self, pts: List[Tuple[float, float]], line: object) -> bool:
        if not pts or len(pts) < 2 or not line or len(line.points) < 2:
            return False
        line_pts = [(float(p[0]), float(p[1])) for p in line.points]
        for idx in range(len(pts) - 1):
            a = pts[idx]
            b = pts[idx + 1]
            for j in range(len(line_pts) - 1):
                if self._intersects(a, b, line_pts[j], line_pts[j + 1]):
                    return True
        return False

    def _raycast_hit(self, px: float, py: float, vx: float, vy: float, target_line: object) -> bool:
        if not target_line or len(target_line.points) < 2:
            return False
        for seg_idx in range(len(target_line.points) - 1):
            ax, ay = float(target_line.points[seg_idx][0]), float(target_line.points[seg_idx][1])
            bx, by = float(target_line.points[seg_idx + 1][0]), float(target_line.points[seg_idx + 1][1])
            mx, my = (ax + bx) / 2.0, (ay + by) / 2.0
            if vx * (mx - px) + vy * (my - py) <= 0:
                continue
            dx_ = bx - ax
            dy_ = by - ay
            det = dx_ * vy - dy_ * vx
            if abs(det) < 1e-6:
                continue
            t = (dx_ * (ay - py) - dy_ * (ax - px)) / det
            u = (vx * (ay - py) - vy * (ax - px)) / det
            if t > 0 and -0.4 <= u <= 1.4:
                return True
        return False

    def _line_center(self, line: object) -> Optional[Tuple[float, float]]:
        if not line or not getattr(line, "points", None):
            return None
        pts = []
        for raw in line.points:
            try:
                pts.append((float(raw[0]), float(raw[1])))
            except Exception:
                continue
        if len(pts) < 2:
            return None
        return (
            sum(p[0] for p in pts) / float(len(pts)),
            sum(p[1] for p in pts) / float(len(pts)),
        )

    def _movement_ref_dir(self, src_line: object, tgt_line: object) -> Optional[Tuple[float, float]]:
        src_center = self._line_center(src_line)
        tgt_center = self._line_center(tgt_line)
        if src_center is None or tgt_center is None:
            return None
        dx = float(tgt_center[0] - src_center[0])
        dy = float(tgt_center[1] - src_center[1])
        norm = (dx * dx + dy * dy) ** 0.5
        if norm < 1e-9:
            return None
        return (dx / norm, dy / norm)

    def _line_endpoints(self, line: object) -> Optional[Tuple[Tuple[float, float], Tuple[float, float]]]:
        if not line or not getattr(line, "points", None):
            return None
        pts = []
        for raw in line.points:
            try:
                pts.append((float(raw[0]), float(raw[1])))
            except Exception:
                continue
        if len(pts) < 2:
            return None
        return (pts[0], pts[-1])

    def _line_intersection_point(
        self,
        a1: Tuple[float, float],
        a2: Tuple[float, float],
        b1: Tuple[float, float],
        b2: Tuple[float, float],
    ) -> Optional[Tuple[float, float]]:
        ax, ay = a1
        bx, by = a2
        cx, cy = b1
        dx, dy = b2
        denom = (bx - ax) * (dy - cy) - (by - ay) * (dx - cx)
        if abs(denom) < 1e-9:
            return None
        t = ((cx - ax) * (dy - cy) - (cy - ay) * (dx - cx)) / denom
        ix = ax + t * (bx - ax)
        iy = ay + t * (by - ay)
        return (float(ix), float(iy))

    def _movement_pivot(self, src_line: object, tgt_line: object) -> Optional[Tuple[float, float]]:
        src_ends = self._line_endpoints(src_line)
        tgt_ends = self._line_endpoints(tgt_line)
        if src_ends and tgt_ends:
            hit = self._line_intersection_point(src_ends[0], src_ends[1], tgt_ends[0], tgt_ends[1])
            if hit is not None:
                return hit
        src_center = self._line_center(src_line)
        tgt_center = self._line_center(tgt_line)
        if src_center is None or tgt_center is None:
            return None
        return ((src_center[0] + tgt_center[0]) / 2.0, (src_center[1] + tgt_center[1]) / 2.0)

    def _point_near(self, point: Tuple[float, float], target: Optional[Tuple[float, float]], radius_px: float) -> bool:
        if target is None:
            return True
        dx = float(point[0] - target[0])
        dy = float(point[1] - target[1])
        return (dx * dx + dy * dy) <= float(radius_px * radius_px)

    def _point_prefers_selected_line(
        self,
        point: Tuple[float, float],
        selected_line: object,
        other_lines: List[object],
        margin_px: float,
    ) -> bool:
        selected_dist = _point_to_line_distance(point, selected_line)
        if not math.isfinite(selected_dist):
            return False
        other_dists = [_point_to_line_distance(point, line) for line in other_lines]
        other_dists = [float(d) for d in other_dists if math.isfinite(d)]
        if not other_dists:
            return True
        return selected_dist <= (min(other_dists) + float(margin_px))

    def _point_near_movement_axis(
        self,
        point: Tuple[float, float],
        src_line: object,
        tgt_line: object,
        corridor_px: float,
        extend_px: float = 120.0,
    ) -> bool:
        src_center = self._line_center(src_line)
        tgt_center = self._line_center(tgt_line)
        if src_center is None or tgt_center is None:
            return True
        ax = float(src_center[0])
        ay = float(src_center[1])
        bx = float(tgt_center[0])
        by = float(tgt_center[1])
        abx = bx - ax
        aby = by - ay
        ab2 = abx * abx + aby * aby
        if ab2 < 1e-9:
            return True
        px = float(point[0])
        py = float(point[1])
        apx = px - ax
        apy = py - ay
        t = (apx * abx + apy * aby) / ab2
        seg_len = ab2 ** 0.5
        if seg_len > 1e-9:
            t_margin = float(extend_px) / seg_len
            if t < -t_margin or t > 1.0 + t_margin:
                return False
        proj_t = max(0.0, min(1.0, t))
        qx = ax + proj_t * abx
        qy = ay + proj_t * aby
        dx = px - qx
        dy = py - qy
        return (dx * dx + dy * dy) <= float(corridor_px * corridor_px)

    def _window_progresses_along_direction(
        self,
        pts: List[Tuple[float, float]],
        ref_dir: Optional[Tuple[float, float]],
        from_head: bool = False,
        window: int = 20,
        min_forward_ratio: float = 0.7,
        min_net_progress: float = 30.0,
    ) -> bool:
        if ref_dir is None:
            return True
        if not pts or len(pts) < 2:
            return False
        sample = pts[: max(2, int(window))] if from_head else pts[-max(2, int(window)) :]
        if len(sample) < 2:
            return False
        rx = float(ref_dir[0])
        ry = float(ref_dir[1])
        projs = [float(p[0]) * rx + float(p[1]) * ry for p in sample]
        deltas = [projs[i + 1] - projs[i] for i in range(len(projs) - 1)]
        if not deltas:
            return False
        forward_steps = sum(1 for d in deltas if d > 0.0)
        forward_ratio = float(forward_steps) / float(len(deltas))
        net_progress = projs[-1] - projs[0]
        return forward_ratio >= float(min_forward_ratio) and net_progress >= float(min_net_progress)

    def _same_direction(self, vx: float, vy: float, ref_dir: Optional[Tuple[float, float]], min_cos: float = 0.2) -> bool:
        if ref_dir is None:
            return True
        norm = (vx * vx + vy * vy) ** 0.5
        if norm < 1e-9:
            return False
        cos_v = (vx * float(ref_dir[0]) + vy * float(ref_dir[1])) / norm
        return cos_v >= float(min_cos)

    def _track_same_direction(
        self,
        pts: List[Tuple[float, float]],
        ref_dir: Optional[Tuple[float, float]],
        min_cos: float = 0.35,
        from_head: bool = False,
        window: int = 20,
    ) -> bool:
        if ref_dir is None:
            return True
        if not pts or len(pts) < 2:
            return False
        if from_head:
            end_idx = min(len(pts) - 1, max(1, int(window) - 1))
            dx = float(pts[end_idx][0] - pts[0][0])
            dy = float(pts[end_idx][1] - pts[0][1])
        else:
            start_idx = max(0, len(pts) - max(2, int(window)))
            dx = float(pts[-1][0] - pts[start_idx][0])
            dy = float(pts[-1][1] - pts[start_idx][1])
        return self._same_direction(dx, dy, ref_dir, min_cos=min_cos)

    def _turn_amount(self, pts: List[Tuple[float, float]]) -> float:
        _hx, _hy, head_vx, head_vy = self._get_head_vector(pts)
        _tx, _ty, tail_vx, tail_vy = self._get_stable_vector(pts)
        head_norm = (head_vx * head_vx + head_vy * head_vy) ** 0.5
        tail_norm = (tail_vx * tail_vx + tail_vy * tail_vy) ** 0.5
        if head_norm < 1e-9 or tail_norm < 1e-9:
            return 0.0
        cross = (head_vx * tail_vy - head_vy * tail_vx) / (head_norm * tail_norm)
        return float(cross)

    def _turn_matches_movement(self, turn_amount: float, movement_type: str, min_turn: float) -> bool:
        if movement_type == "left_turn":
            # Screen coordinates use +Y downward, so the signed turn is flipped
            # relative to the usual Cartesian convention.
            return float(turn_amount) <= -float(min_turn)
        if movement_type == "right_turn":
            return float(turn_amount) >= float(min_turn)
        return abs(float(turn_amount)) < float(min_turn)

    def _vector_to_dir_label(self, vx: float, vy: float) -> str:
        norm = (float(vx) * float(vx) + float(vy) * float(vy)) ** 0.5
        if norm < 1e-9:
            return "-"
        parts: List[str] = []
        x_ratio = abs(float(vx)) / norm
        y_ratio = abs(float(vy)) / norm
        if x_ratio >= 0.35:
            parts.append("우" if float(vx) > 0 else "좌")
        if y_ratio >= 0.35:
            parts.append("하" if float(vy) > 0 else "상")
        if not parts:
            if x_ratio >= y_ratio:
                parts.append("우" if float(vx) > 0 else "좌")
            else:
                parts.append("하" if float(vy) > 0 else "상")
        return "".join(parts)

    def _movement_label(self, movement_type: str) -> str:
        return {
            "straight": "직진",
            "left_turn": "좌회전",
            "right_turn": "우회전",
        }.get(str(movement_type or "straight"), str(movement_type or "-"))

    def _heading_score_to_line(self, px: float, py: float, vx: float, vy: float, target_line: object) -> float:
        center = self._line_center(target_line)
        if center is None:
            return -1.0
        tx = float(center[0] - px)
        ty = float(center[1] - py)
        v_norm = (vx * vx + vy * vy) ** 0.5
        t_norm = (tx * tx + ty * ty) ** 0.5
        if v_norm < 1e-9 or t_norm < 1e-9:
            return -1.0
        return float((vx * tx + vy * ty) / (v_norm * t_norm))

    def _get_stable_vector(self, pts: List[Tuple[float, float]]) -> Tuple[float, float, float, float]:
        if not pts or len(pts) < 2:
            return 0.0, 0.0, 0.0, 0.0
        total_dx = pts[-1][0] - pts[0][0]
        total_dy = pts[-1][1] - pts[0][1]
        if (total_dx * total_dx + total_dy * total_dy) ** 0.5 < 60.0:
            return 0.0, 0.0, 0.0, 0.0
        n_idx = max(0, len(pts) - 15)
        vx = pts[-1][0] - pts[n_idx][0]
        vy = pts[-1][1] - pts[n_idx][1]
        if (vx * vx + vy * vy) ** 0.5 < 5.0:
            vx = total_dx
            vy = total_dy
        return pts[-1][0], pts[-1][1], vx, vy

    def _get_head_vector(self, pts: List[Tuple[float, float]]) -> Tuple[float, float, float, float]:
        if not pts or len(pts) < 2:
            return 0.0, 0.0, 0.0, 0.0
        total_dx = pts[-1][0] - pts[0][0]
        total_dy = pts[-1][1] - pts[0][1]
        if (total_dx * total_dx + total_dy * total_dy) ** 0.5 < 60.0:
            return 0.0, 0.0, 0.0, 0.0
        end_idx = min(len(pts) - 1, 14)
        vx = pts[end_idx][0] - pts[0][0]
        vy = pts[end_idx][1] - pts[0][1]
        if (vx * vx + vy * vy) ** 0.5 < 5.0:
            vx = total_dx
            vy = total_dy
        return pts[0][0], pts[0][1], vx, vy

    def _is_heading_towards(self, pts: List[Tuple[float, float]], tgt_line: object) -> bool:
        px, py, vx, vy = self._get_stable_vector(pts)
        if vx == 0.0 and vy == 0.0:
            return False
        if self._raycast_hit(px, py, vx, vy, tgt_line):
            return True
        return self._heading_score_to_line(px, py, vx, vy, tgt_line) >= 0.15

    def _is_coming_from(self, pts: List[Tuple[float, float]], src_line: object) -> bool:
        px, py, vx, vy = self._get_head_vector(pts)
        if vx == 0.0 and vy == 0.0:
            return False
        if self._raycast_hit(px, py, -vx, -vy, src_line):
            return True
        return self._heading_score_to_line(px, py, -vx, -vy, src_line) >= 0.15

    @Slot()
    def run(self) -> None:
        try:
            if not self.db_path.exists():
                self.finished.emit({"token": self.token, "track_count": 0})
                return
            try:
                init_db(self.db_path)
            except Exception:
                logger.debug("Suppressed error", exc_info=True)
            with sqlite3.connect(self.db_path) as conn:
                has_track_trajs = conn.execute(
                    "select 1 from sqlite_master where type='table' and name='track_trajs' limit 1"
                ).fetchone() is not None
                if not has_track_trajs:
                    self.finished.emit({"token": self.token, "track_count": 0})
                    return
                query = (
                    "select track_id, coalesce(vehicle_type, class_name) as cls_name, traj "
                    "from track_trajs where traj is not null"
                )
                params: List[object] = []
                if self.session_id:
                    query += " and session_id = ?"
                    params.append(self.session_id)
                if self.slot_index is not None:
                    query += " and (start_ts_ms / ?) = ?"
                    params.extend([int(self._slot_interval_ms), int(self.slot_index)])
                query += " order by track_id"
                tracks_batch = []
                track_count = 0
                for tid, cls_name, blob in conn.execute(query, tuple(params)):
                    if self._interrupted():
                        return
                    pts_raw = decode_traj(blob) if blob is not None else []
                    full_pts: List[Tuple[float, float]] = []
                    pts: List[Tuple[float, float]] = []
                    for row in pts_raw:
                        if not isinstance(row, list) or len(row) < 4:
                            continue
                        try:
                            x = float(row[2])
                            y = float(row[3])
                        except Exception:
                            continue
                        full_pts.append((x, y))
                        try:
                            fid_i = int(row[0])
                        except Exception:
                            fid_i = 0
                        if self.frame_step > 1 and (fid_i % self.frame_step) != 0:
                            continue
                        pts.append((x, y))
                    if len(full_pts) < 2:
                        continue
                    if len(pts) < 2:
                        pts = list(full_pts)
                    group_label = ""
                    if self.filter_mode:
                        src = self.filter_mode.get("src", "")
                        tgt = self.filter_mode.get("tgt", "")
                        movement_type = str(self.filter_mode.get("movement_type", "straight") or "straight")
                        relaxed_merge = bool(self.filter_mode.get("relaxed_merge", False))
                        lines_map = self.filter_mode.get("lines_map", {})
                        src_line = lines_map.get(src)
                        tgt_line = lines_map.get(tgt) if tgt != "(라인 없음)" else None
                        ref_dir = self._movement_ref_dir(src_line, tgt_line) if (src_line and tgt_line) else None
                        pivot = self._movement_pivot(src_line, tgt_line) if (src_line and tgt_line) else None
                        _tail_x, _tail_y, tail_vx, tail_vy = self._get_stable_vector(full_pts)
                        _head_x, _head_y, head_vx, head_vy = self._get_head_vector(full_pts)
                        turn_amount = self._turn_amount(full_pts)
                        is_turn_mode = movement_type in ("left_turn", "right_turn")
                        if is_turn_mode:
                            pass_src = self._passes_line(full_pts, src_line) if src_line else False
                            pass_tgt = self._passes_line(full_pts, tgt_line) if tgt_line else False
                            other_hits = sum(
                                1
                                for other_name, other_line in lines_map.items()
                                if other_name not in (src, tgt) and self._passes_line(full_pts, other_line)
                            )
                            min_turn = 0.10 if relaxed_merge else 0.18
                            min_cos = 0.15 if relaxed_merge else 0.25
                            pivot_radius = 180.0 if relaxed_merge else 130.0
                            cond_a = (
                                pass_src
                                and (not pass_tgt)
                                and self._same_direction(tail_vx, tail_vy, ref_dir, min_cos=min_cos)
                                and self._turn_matches_movement(turn_amount, movement_type, min_turn)
                                and other_hits <= (2 if relaxed_merge else 1)
                                and self._point_near(full_pts[-1], pivot, pivot_radius)
                            )
                            cond_b = (
                                (not pass_src)
                                and pass_tgt
                                and self._same_direction(head_vx, head_vy, ref_dir, min_cos=min_cos)
                                and self._turn_matches_movement(turn_amount, movement_type, min_turn)
                                and other_hits <= (2 if relaxed_merge else 1)
                                and self._point_near(full_pts[0], pivot, pivot_radius)
                            )
                        else:
                            pass_src = self._passes_line(pts, src_line) if src_line else False
                            pass_tgt = self._passes_line(pts, tgt_line) if tgt_line else False
                            pass_other = any(
                                self._passes_line(pts, other_line)
                                for other_name, other_line in lines_map.items()
                                if other_name not in (src, tgt)
                            )
                            _tail_x_s, _tail_y_s, tail_vx_s, tail_vy_s = self._get_stable_vector(pts)
                            _head_x_s, _head_y_s, head_vx_s, head_vy_s = self._get_head_vector(pts)
                            dir_ok_a = self._track_same_direction(
                                pts,
                                ref_dir,
                                min_cos=0.40 if relaxed_merge else 0.50,
                                from_head=False,
                                window=20,
                            )
                            dir_ok_b = self._track_same_direction(
                                pts,
                                ref_dir,
                                min_cos=0.40 if relaxed_merge else 0.50,
                                from_head=True,
                                window=20,
                            )
                            dir_prog_a = self._window_progresses_along_direction(
                                pts,
                                ref_dir,
                                from_head=False,
                                window=20,
                                min_forward_ratio=0.65 if relaxed_merge else 0.75,
                                min_net_progress=20.0 if relaxed_merge else 35.0,
                            )
                            dir_prog_b = self._window_progresses_along_direction(
                                pts,
                                ref_dir,
                                from_head=True,
                                window=20,
                                min_forward_ratio=0.65 if relaxed_merge else 0.75,
                                min_net_progress=20.0 if relaxed_merge else 35.0,
                            )
                            cond_a = False
                            cond_b = False
                            if not pass_other:
                                if relaxed_merge:
                                    cond_a = (
                                        dir_ok_a
                                        and dir_prog_a
                                        and
                                        pass_src
                                        and (not pass_tgt)
                                        and self._same_direction(tail_vx_s, tail_vy_s, ref_dir, min_cos=0.35)
                                        and (tgt_line is None or self._is_heading_towards(pts, tgt_line))
                                    )
                                    cond_b = (
                                        dir_ok_b
                                        and dir_prog_b
                                        and
                                        (not pass_src)
                                        and pass_tgt
                                        and self._same_direction(head_vx_s, head_vy_s, ref_dir, min_cos=0.35)
                                        and (src_line is None or self._is_coming_from(pts, src_line))
                                    )
                                else:
                                    cond_a = (
                                        dir_ok_a
                                        and dir_prog_a
                                        and
                                        pass_src
                                        and (not pass_tgt)
                                        and self._same_direction(tail_vx_s, tail_vy_s, ref_dir, min_cos=0.45)
                                        and (tgt_line is None or self._is_heading_towards(pts, tgt_line))
                                    )
                                    if cond_a:
                                        for other_name, other_line in lines_map.items():
                                            if other_name in (src, tgt):
                                                continue
                                            if self._is_coming_from(pts, other_line):
                                                cond_a = False
                                                break
                                    cond_b = (
                                        dir_ok_b
                                        and dir_prog_b
                                        and
                                        (not pass_src)
                                        and pass_tgt
                                        and self._same_direction(head_vx_s, head_vy_s, ref_dir, min_cos=0.45)
                                        and (src_line is None or self._is_coming_from(pts, src_line))
                                    )
                                    if cond_b:
                                        for other_name, other_line in lines_map.items():
                                            if other_name in (src, tgt):
                                                continue
                                            if self._is_heading_towards(pts, other_line):
                                                cond_b = False
                                                break
                        if cond_a:
                            group_label = "A"
                        elif cond_b:
                            group_label = "B"
                        else:
                            continue
                    track_count += 1
                    if self.max_tracks and track_count > self.max_tracks:
                        break
                    if track_count % 200 == 0:
                        self.progress.emit(f"濡쒕뵫 以?.. tracks={track_count}")
                    tracks_batch.append((str(tid), pts, group_label, str(cls_name or "")))
                    if len(tracks_batch) >= self.batch_size_tracks:
                        self.batch_ready.emit(tracks_batch)
                        tracks_batch = []
                if tracks_batch:
                    self.batch_ready.emit(tracks_batch)
                self.finished.emit({"token": self.token, "track_count": track_count})
        except Exception as exc:
            self.error.emit(str(exc))
            self.finished.emit({"token": self.token, "track_count": 0})


class TrajectoryViewer2Window(QMainWindow):
    def __init__(
        self,
        db_path: Path,
        lines_path: Path | None = None,
        video_path: Path | None = None,
        resize: Tuple[int, int] | None = None,
        session_id: Optional[str] = None,
        config_path: Path | None = None,
    ):
        super().__init__()
        self.setWindowTitle("궤적 보기 2")
        fit_to_screen(self, 1350, 1000)
        self.config_path = config_path
        self._config_geometry_key = "trajectory2_window_geometry"
        self._config_splitter_key = "trajectory2_splitter_sizes"
        self._config_bottom_splitter_key = "trajectory2_bottom_splitter_sizes"
        self._config_opts_key = "trajectory_viewer2_options"
        self._restoring_options = False
        self._restore_session_id: Optional[str] = None
        self._restore_slot_index: Optional[int] = None
        self.resize_target = resize or (1312, 736)
        self.track_width = 3.5
        self._pending_line_points: List[Tuple[float, float]] = []
        self._pending_items: List[object] = []
        self._lines_raw: List[LineDef] = []
        self._lines: List[LineDef] = self._lines_raw
        self._line_items: List[QGraphicsPathItem] = []
        self._line_label_items: List[QGraphicsTextItem] = []
        self._track_items: List[QGraphicsPathItem] = []
        self._track_items_meta: List[Tuple[QGraphicsPathItem, str, List[Tuple[float, float]], str, str]] = []
        self._bg_item: Optional[QGraphicsPixmapItem] = None
        self._bg_size: Tuple[float, float] = (0.0, 0.0)
        self._lines_base_size_raw: Optional[Tuple[int, int]] = None
        self._lines_base_size: Optional[Tuple[int, int]] = None
        self._load_thread: Optional[QThread] = None
        self._load_worker: Optional[_TrajectoryLoadWorker] = None
        self._loading_token = 0
        self._color_idx = 0
        self._scale_sx = 1.0
        self._scale_sy = 1.0
        self._reload_pending = False
        self._bg_enabled = False
        self._apply_merge_filter: Optional[Dict] = None
        self._merge_cfg: Dict = {}
        self._merge_relaxed_mode = False
        self._extrap_cfg: Dict = {}
        self._merged_parent_track_ids: set[str] = set()
        self._merged_source_to_parent: Dict[str, str] = {}
        self._render_excluded_track_ids: set[str] = set()
        self._extrap_preview_items: List[object] = []
        self._setting_in_point_index: Optional[int] = None
        self._class_color_map = {
            "small_bus": "#00ff00", "소형버스": "#00ff00",
            "passenger_car": "#0000ff", "car": "#0000ff", "승용차": "#0000ff",
            "medium_truck": "#ff0000", "중형화물": "#ff0000",
            "large_truck": "#00ffff", "truck": "#00ffff", "대형화물": "#00ffff",
            "large_bus": "#ff00ff", "bus": "#ff00ff", "대형버스": "#ff00ff",
            "etc": "#ffff00", "기타": "#ffff00",
            "small_truck": "#800080", "소형화물": "#800080",
        }

        self.db_input = QLineEdit(str(db_path))
        self.session_combo = QComboBox()
        self.slot_combo = QComboBox()
        self.video_input = QLineEdit(str(video_path) if video_path else "")
        self.lines_input = QLineEdit(str(lines_path) if lines_path else "config/lines.json")
        self.reload_btn = QPushButton("새로고침")
        self.quit_btn = QPushButton("창 종료")
        self.quit_btn.setStyleSheet("QPushButton { color: #ff4d4d; font-weight: 800; }")
        self.refresh_sessions_btn = QPushButton("세션 새로고침")
        self.refresh_slots_btn = QPushButton("슬롯 새로고침")
        self.pick_video_btn = QPushButton("영상 선택")
        self.pick_db_btn = QPushButton("DB 선택")
        self.view_db_btn = QPushButton("DB 보기")
        self.pick_lines_btn = QPushButton("Lines 선택")
        self.view_postprocess_btn = QPushButton("후처리 보기")
        self.width_spin = QDoubleSpinBox()
        self.width_spin.setRange(0.5, 12.0)
        self.width_spin.setSingleStep(0.5)
        self.width_spin.setValue(self.track_width)
        self.width_spin.setFixedWidth(88)
        self.zoom_label = QLabel("zoom: 100%")
        self.fit_btn = QPushButton("화면 맞춤")
        self.reset_zoom_btn = QPushButton("100%")
        self.bg_toggle_btn = QPushButton("배경 켜기")
        self.frame_step_spin = QSpinBox()
        self.frame_step_spin.setRange(1, 120)
        self.frame_step_spin.setValue(5)
        self.frame_step_spin.setFixedWidth(78)
        self.max_tracks_spin = QSpinBox()
        self.max_tracks_spin.setRange(0, 20000)
        self.max_tracks_spin.setValue(1500)
        self.max_tracks_spin.setFixedWidth(110)
        self.btn_config_merge = QPushButton("병합 설정")
        self.btn_config_extrap = QPushButton("외삽 설정")
        self.btn_preview_merge = QPushButton("병합 미리보기")
        self.btn_preview_merge.setEnabled(False)
        self.btn_run_merge = QPushButton("병합 실행")
        self.btn_cancel_merge = QPushButton("병합 취소")
        self.btn_cancel_merge.setEnabled(False)
        self.btn_merge_relaxed = QPushButton("병합 완화")
        self.btn_merge_relaxed.setCheckable(True)
        self.btn_merge_debug = QPushButton("병합 디버그")
        self.btn_merge_debug.setCheckable(True)
        self.btn_preview_extrap = QPushButton("외삽 미리보기")
        self.btn_preview_extrap.setEnabled(False)
        self.btn_run_extrap = QPushButton("외삽 실행")
        self.btn_run_extrap.setEnabled(False)
        self.btn_cancel_extrap = QPushButton("외삽 취소")
        self.btn_cancel_extrap.setEnabled(False)
        self.btn_manual_pick = QPushButton("수동 선택")
        self.btn_manual_pick.setCheckable(True)
        self.btn_manual_merge = QPushButton("선택2개 병합")
        self.btn_manual_extrap_fwd = QPushButton("선택1개 앞외삽")
        self.btn_manual_extrap_back = QPushButton("선택1개 뒤외삽")
        self.btn_manual_clear = QPushButton("선택 초기화")
        self.view_db_btn.setFixedWidth(104)
        self.btn_config_merge.setFixedWidth(104)
        self.btn_config_extrap.setFixedWidth(104)
        self.btn_preview_merge.setText("병합 미리보기")
        self.btn_preview_merge.setFixedWidth(118)
        self.btn_run_merge.setFixedWidth(96)
        self.btn_cancel_merge.setFixedWidth(96)
        self.btn_merge_relaxed.setFixedWidth(118)
        self.btn_merge_debug.setFixedWidth(104)
        self.btn_preview_extrap.setFixedWidth(125)
        self.btn_run_extrap.setFixedWidth(96)
        self.btn_cancel_extrap.setFixedWidth(96)
        self.btn_manual_pick.setFixedWidth(96)
        self.btn_manual_merge.setFixedWidth(126)
        self.btn_manual_extrap_fwd.setFixedWidth(118)
        self.btn_manual_extrap_back.setFixedWidth(118)
        self.btn_manual_clear.setFixedWidth(88)
        self.fit_btn.setFixedWidth(92)
        self.reset_zoom_btn.setFixedWidth(78)
        self.bg_toggle_btn.setFixedWidth(118)
        self.view_db_btn.setFixedWidth(118)
        self.view_postprocess_btn.setFixedWidth(118)
        self.reload_btn.setFixedWidth(96)
        self.quit_btn.setFixedWidth(118)
        self.line_id_input = QLineEdit()
        self.line_id_input.setPlaceholderText("line_1")
        self.bound_input = QComboBox()
        self.bound_input.addItems(["", "east_bound", "west_bound", "south_bound", "north_bound", "in_bound", "out_bound"])
        self.bound_input.setEditable(True)
        self.line_add_btn = QPushButton("라인 추가")
        self.line_delete_btn = QPushButton("라인 선택 삭제")
        self.line_set_inpoint_btn = QPushButton("in점지정")
        self.line_clear_inpoint_btn = QPushButton("in점초기화")
        self.line_clear_pts_btn = QPushButton("포인트초기화")
        self.line_reset_btn = QPushButton("라인 전체 초기화")
        self.line_save_btn = QPushButton("Lines 저장")
        self.line_load_btn = QPushButton("Lines 불러오기")
        self.lines_list = QListWidget()
        self.status_label = QLabel("")
        self.view = _AutoFitView()
        self.view.setAlignment(Qt.AlignCenter)
        self.view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.view.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.view.set_click_callback(self._on_view_click)
        self.view.set_zoom_callback(self._on_zoom_changed)
        self.scene = QGraphicsScene()
        self.view.setScene(self.scene)
        self._merge_debug_mode = False
        self._manual_pick_mode = False
        self._manual_selected_track_ids: List[str] = []
        self._line_draw_mode = False
        self._build_ui()
        self._wire_signals()
        self._restore_window_state()
        self._restore_options()
        self._refresh_sessions(select=session_id)
        QTimer.singleShot(0, self._reload_all)

    def _on_zoom_changed(self, scale_x: float) -> None:
        try:
            pct = int(round(float(scale_x) * 100.0))
        except Exception:
            pct = 100
        self.zoom_label.setText(f"zoom: {pct}%")

    def _wrap(self, layout: QHBoxLayout) -> QWidget:
        w = QWidget()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        w.setLayout(layout)
        return w

    def _build_ui(self) -> None:
        self.splitter = QSplitter(Qt.Orientation.Vertical)
        self.splitter.setChildrenCollapsible(False)
        top = QWidget()
        top_layout = QVBoxLayout()
        top_layout.setContentsMargins(6, 6, 6, 6)
        top_layout.addWidget(self.view, stretch=1)
        top.setLayout(top_layout)
        traj_box = QGroupBox("궤적/영상 설정")
        traj_grid = QGridLayout()
        def cap(text: str) -> QLabel:
            lbl = QLabel(text)
            lbl.setProperty("role", "caption")
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            return lbl
        db_row = QHBoxLayout(); db_row.addWidget(self.db_input, stretch=1); db_row.addWidget(self.pick_db_btn)
        sess_row = QHBoxLayout(); sess_row.addWidget(self.session_combo, stretch=1); sess_row.addWidget(self.refresh_sessions_btn)
        slot_row = QHBoxLayout(); slot_row.addWidget(self.slot_combo, stretch=1); slot_row.addWidget(self.refresh_slots_btn)
        video_row = QHBoxLayout(); video_row.addWidget(self.video_input, stretch=1); video_row.addWidget(self.pick_video_btn)
        traj_grid.addWidget(cap("DB 경로"), 0, 0); traj_grid.addWidget(self._wrap(db_row), 0, 1)
        traj_grid.addWidget(cap("session_id"), 0, 2); traj_grid.addWidget(self._wrap(sess_row), 0, 3)
        traj_grid.addWidget(cap("슬롯(15분)"), 1, 0); traj_grid.addWidget(self._wrap(slot_row), 1, 1)
        traj_grid.addWidget(cap("영상 경로"), 1, 2); traj_grid.addWidget(self._wrap(video_row), 1, 3)
        perf_row = QHBoxLayout()
        perf_row.addWidget(cap("트랙 폭")); perf_row.addWidget(self.width_spin)
        perf_row.addWidget(cap("프레임 간격")); perf_row.addWidget(self.frame_step_spin)
        perf_row.addWidget(cap("최대 트랙 수")); perf_row.addWidget(self.max_tracks_spin)
        traj_grid.addWidget(self._wrap(perf_row), 2, 0, 1, 4)
        merge_row = QHBoxLayout()
        merge_row.addWidget(self.btn_config_merge)
        merge_row.addWidget(self.btn_merge_relaxed)
        merge_row.addWidget(self.btn_merge_debug)
        merge_row.addWidget(self.btn_preview_merge)
        merge_row.addWidget(self.btn_run_merge)
        merge_row.addWidget(self.btn_cancel_merge)
        merge_group = QGroupBox("병합")
        merge_group.setLayout(merge_row)
        extrap_row = QHBoxLayout()
        extrap_row.addWidget(self.btn_config_extrap)
        extrap_row.addWidget(self.btn_preview_extrap)
        extrap_row.addWidget(self.btn_run_extrap)
        extrap_row.addWidget(self.btn_cancel_extrap)
        extrap_group = QGroupBox("외삽")
        extrap_group.setLayout(extrap_row)
        manual_row = QHBoxLayout()
        manual_row.addWidget(self.btn_manual_pick)
        manual_row.addWidget(self.btn_manual_merge)
        manual_row.addWidget(self.btn_manual_extrap_fwd)
        manual_row.addWidget(self.btn_manual_extrap_back)
        manual_row.addWidget(self.btn_manual_clear)
        manual_group = QGroupBox("수동 보정")
        manual_group.setLayout(manual_row)
        groups_row = QHBoxLayout()
        groups_row.addWidget(merge_group, stretch=1)
        groups_row.addWidget(extrap_group, stretch=1)
        traj_grid.addWidget(self._wrap(groups_row), 3, 0, 1, 4)
        groups_row2 = QHBoxLayout()
        groups_row2.addWidget(manual_group, stretch=1)
        traj_grid.addWidget(self._wrap(groups_row2), 4, 0, 1, 4)
        traj_grid.addItem(QSpacerItem(0, 0, QSizePolicy.Minimum, QSizePolicy.Expanding), 5, 0, 1, 4)
        view_row = QHBoxLayout()
        view_row.addWidget(self.fit_btn)
        view_row.addWidget(self.reset_zoom_btn)
        view_row.addWidget(self.bg_toggle_btn)
        view_row.addWidget(self.view_db_btn)
        view_row.addWidget(self.view_postprocess_btn)
        misc_row = QHBoxLayout()
        misc_row.addWidget(self.reload_btn)
        misc_row.addWidget(self.zoom_label)
        misc_row.addWidget(self.quit_btn)
        zoom_row = QHBoxLayout()
        zoom_row.addWidget(self._wrap(view_row))
        zoom_row.addStretch()
        zoom_row.addWidget(self._wrap(misc_row))
        traj_grid.addWidget(self._wrap(zoom_row), 5, 0, 1, 4)
        traj_box.setLayout(traj_grid)
        line_box = QGroupBox("라인 설정")
        line_grid = QGridLayout()
        lines_row = QHBoxLayout(); lines_row.addWidget(self.lines_input, stretch=1); lines_row.addWidget(self.pick_lines_btn)
        line_grid.addWidget(cap("Lines JSON"), 0, 0); line_grid.addWidget(self._wrap(lines_row), 0, 1, 1, 3)
        line_grid.addWidget(cap("라인 ID"), 1, 0); line_grid.addWidget(self.line_id_input, 1, 1)
        line_grid.addWidget(cap("bound"), 1, 2); line_grid.addWidget(self.bound_input, 1, 3)
        left_v = QVBoxLayout(); left_v.addWidget(cap("라인 목록")); left_v.addWidget(self.lines_list, stretch=1)
        right_grid = QGridLayout()
        right_grid.addWidget(self.line_add_btn, 0, 0); right_grid.addWidget(self.line_delete_btn, 0, 1)
        right_grid.addWidget(self.line_set_inpoint_btn, 1, 0); right_grid.addWidget(self.line_clear_inpoint_btn, 1, 1)
        right_grid.addWidget(self.line_clear_pts_btn, 2, 0); right_grid.addWidget(self.line_reset_btn, 2, 1)
        right_grid.addWidget(self.line_load_btn, 3, 0); right_grid.addWidget(self.line_save_btn, 3, 1)
        list_row = QHBoxLayout(); list_row.addLayout(left_v); list_row.addLayout(right_grid)
        line_grid.addWidget(self._wrap(list_row), 2, 0, 1, 4)
        line_box.setLayout(line_grid)
        self.bottom_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.bottom_splitter.setChildrenCollapsible(False)
        # 하단 설정 패널은 창이 작을 때 잘리지 않도록 각각 스크롤로 감싼다.
        self.bottom_splitter.addWidget(wrap_in_scroll(traj_box))
        self.bottom_splitter.addWidget(wrap_in_scroll(line_box))
        self.bottom_splitter.setSizes([700, 500])
        self.splitter.addWidget(top)
        self.splitter.addWidget(self.bottom_splitter)
        self.splitter.setSizes([760, 240])
        container = QWidget()
        layout = QVBoxLayout()
        layout.addWidget(self.splitter, stretch=1)
        layout.addWidget(self.status_label)
        container.setLayout(layout)
        self.setCentralWidget(container)

    def _wire_signals(self) -> None:
        self.refresh_sessions_btn.clicked.connect(lambda: self._refresh_sessions(select=self.session_combo.currentText() or None))
        self.refresh_slots_btn.clicked.connect(self._refresh_slots)
        self.reload_btn.clicked.connect(self._reset_filter_mode)
        self.quit_btn.clicked.connect(self.close)
        self.width_spin.valueChanged.connect(self._on_width_changed)
        self.fit_btn.clicked.connect(self.view.fit_to_rect)
        self.reset_zoom_btn.clicked.connect(self.view.reset_zoom)
        self.bg_toggle_btn.clicked.connect(self._toggle_background)
        self.btn_config_merge.clicked.connect(self._on_config_merge)
        self.btn_merge_relaxed.clicked.connect(self._toggle_merge_relaxed_mode)
        self.btn_merge_debug.clicked.connect(self._toggle_merge_debug_mode)
        self.btn_config_extrap.clicked.connect(self._on_config_extrap)
        self.btn_preview_merge.clicked.connect(self._on_preview_merge)
        self.btn_run_merge.clicked.connect(self._on_execute_merge)
        self.btn_cancel_merge.clicked.connect(self._on_cancel_merge)
        self.btn_preview_extrap.clicked.connect(self._on_preview_extrap)
        self.btn_run_extrap.clicked.connect(self._on_execute_extrap)
        self.btn_cancel_extrap.clicked.connect(self._on_cancel_extrap)
        self.btn_manual_pick.clicked.connect(self._toggle_manual_pick_mode)
        self.btn_manual_merge.clicked.connect(self._on_execute_manual_merge)
        self.btn_manual_extrap_fwd.clicked.connect(lambda: self._on_execute_manual_extrap(backward=False))
        self.btn_manual_extrap_back.clicked.connect(lambda: self._on_execute_manual_extrap(backward=True))
        self.btn_manual_clear.clicked.connect(self._clear_manual_selection)
        self.frame_step_spin.valueChanged.connect(self._on_perf_changed)
        self.max_tracks_spin.valueChanged.connect(self._on_perf_changed)
        self.pick_db_btn.clicked.connect(self._pick_db)
        self.view_db_btn.clicked.connect(self._open_db_viewer)
        self.view_postprocess_btn.clicked.connect(self._open_postprocess_viewer)
        self.pick_video_btn.clicked.connect(self._pick_video)
        self.pick_lines_btn.clicked.connect(self._pick_lines)
        self.line_add_btn.clicked.connect(self._add_line_from_pending)
        self.line_delete_btn.clicked.connect(self._delete_selected_line)
        self.line_set_inpoint_btn.clicked.connect(self._begin_set_in_point)
        self.line_clear_inpoint_btn.clicked.connect(self._clear_selected_in_point)
        self.line_clear_pts_btn.clicked.connect(self._clear_pending_points)
        self.line_reset_btn.clicked.connect(self._reset_lines)
        self.line_save_btn.clicked.connect(self._save_lines)
        self.line_load_btn.clicked.connect(self._load_lines_from_path)
        self.lines_list.currentRowChanged.connect(self._on_line_selected)
        self.session_combo.currentTextChanged.connect(self._on_session_changed)
        self.slot_combo.currentTextChanged.connect(self._on_slot_changed)
        self.db_input.editingFinished.connect(self._persist_options)
        self.lines_input.editingFinished.connect(self._persist_options)
        self.video_input.editingFinished.connect(self._persist_options)

    def _restore_window_state(self) -> None:
        if not self.config_path or not self.config_path.exists():
            return
        try:
            cfg = load_app_config(self.config_path)
            geo = cfg.get(self._config_geometry_key)
            if isinstance(geo, str) and geo:
                self._apply_geometry_str(geo)
            sizes = cfg.get(self._config_splitter_key)
            if isinstance(sizes, list) and all(isinstance(x, int) for x in sizes):
                self.splitter.setSizes([int(x) for x in sizes])
            bottom_sizes = cfg.get(self._config_bottom_splitter_key)
            if isinstance(bottom_sizes, list) and all(isinstance(x, int) for x in bottom_sizes):
                self.bottom_splitter.setSizes([int(x) for x in bottom_sizes])
        except Exception:
            logger.debug("Suppressed error", exc_info=True)
            return

    def closeEvent(self, event) -> None:
        if not self._cancel_loader():
            event.ignore()
            return
        self._persist_options()
        self._persist_window_state()
        super().closeEvent(event)

    def _restore_options(self) -> None:
        if not self.config_path or not self.config_path.exists():
            return
        try:
            cfg = load_app_config(self.config_path)
            opts = cfg.get(self._config_opts_key, {})
            if not isinstance(opts, dict):
                return
        except Exception:
            logger.debug("Suppressed error", exc_info=True)
            return
        self._restoring_options = True
        try:
            if isinstance(opts.get("db_path"), str) and opts.get("db_path"):
                self.db_input.setText(opts["db_path"])
            if isinstance(opts.get("lines_path"), str) and opts.get("lines_path"):
                self.lines_input.setText(opts["lines_path"])
                self._load_lines_from_path()
            if isinstance(opts.get("video_path"), str):
                self.video_input.setText(opts["video_path"])
            try:
                self.track_width = float(opts.get("track_width", self.track_width))
                self.width_spin.setValue(self.track_width)
            except Exception:
                logger.debug("Suppressed error", exc_info=True)
            try:
                self.frame_step_spin.setValue(int(opts.get("frame_step", int(self.frame_step_spin.value()))))
            except Exception:
                logger.debug("Suppressed error", exc_info=True)
            try:
                self.max_tracks_spin.setValue(int(opts.get("max_tracks", int(self.max_tracks_spin.value()))))
            except Exception:
                logger.debug("Suppressed error", exc_info=True)
            self._bg_enabled = bool(opts.get("bg_enabled", False))
            self.bg_toggle_btn.setText("배경 보임" if self._bg_enabled else "배경 숨김")
            merge_cfg = opts.get("merge_cfg")
            if isinstance(merge_cfg, dict):
                self._merge_cfg = dict(merge_cfg)
            self._merge_relaxed_mode = bool(opts.get("merge_relaxed_mode", False))
            self.btn_merge_relaxed.setChecked(self._merge_relaxed_mode)
            extrap_cfg = opts.get("extrap_cfg")
            if isinstance(extrap_cfg, dict):
                self._extrap_cfg = dict(extrap_cfg)
                self.btn_preview_extrap.setEnabled(True)
                self.btn_run_extrap.setEnabled(True)
                self.btn_cancel_extrap.setEnabled(True)
            sid = opts.get("session_id")
            self._restore_session_id = str(sid) if isinstance(sid, str) and sid else None
            sidx = opts.get("slot_index")
            self._restore_slot_index = int(sidx) if isinstance(sidx, int) else None
        finally:
            self._restoring_options = False

    def _persist_options(self) -> None:
        if self._restoring_options or not self.config_path:
            return
        try:
            cfg = load_app_config(self.config_path) if self.config_path.exists() else {}
            opts = cfg.get(self._config_opts_key, {})
            if not isinstance(opts, dict):
                opts = {}
            opts["db_path"] = self.db_input.text().strip()
            opts["lines_path"] = self.lines_input.text().strip()
            opts["video_path"] = self.video_input.text().strip()
            opts["track_width"] = float(self.track_width)
            opts["frame_step"] = int(self.frame_step_spin.value())
            opts["max_tracks"] = int(self.max_tracks_spin.value())
            opts["bg_enabled"] = bool(self._bg_enabled)
            opts["merge_cfg"] = dict(self._merge_cfg or {})
            opts["merge_relaxed_mode"] = bool(self._merge_relaxed_mode)
            opts["extrap_cfg"] = dict(self._extrap_cfg or {})
            sess_raw = self.session_combo.currentText().strip()
            opts["session_id"] = "" if (not sess_raw or sess_raw == "(선택)") else sess_raw
            data = self.slot_combo.currentData()
            opts["slot_index"] = int(data) if data is not None else None
            cfg[self._config_opts_key] = opts
            save_app_config(self.config_path, cfg)
        except Exception:
            logger.debug("Suppressed error", exc_info=True)
            return

    def _persist_window_state(self) -> None:
        if not self.config_path:
            return
        try:
            cfg = load_app_config(self.config_path) if self.config_path.exists() else {}
            g = self.geometry()
            cfg[self._config_geometry_key] = f"{g.width()}x{g.height()}+{g.x()}+{g.y()}"
            cfg[self._config_splitter_key] = [int(x) for x in self.splitter.sizes()]
            cfg[self._config_bottom_splitter_key] = [int(x) for x in self.bottom_splitter.sizes()]
            save_app_config(self.config_path, cfg)
        except Exception:
            logger.debug("Suppressed error", exc_info=True)
            return

    def _set_reload_pending(self, pending: bool, reason: str = "") -> None:
        self._reload_pending = bool(pending)
        self.reload_btn.setText("새로고침*" if self._reload_pending else "새로고침")
        if self._reload_pending and reason:
            self.status_label.setText(f"새로고침 대기: {reason} 변경됨. 새로고침 해주세요.")

    def _rerender_lines_overlay_only(self) -> None:
        for it in list(self._line_items):
            try:
                self.scene.removeItem(it)
            except Exception:
                logger.debug("Suppressed error", exc_info=True)
        for it in list(self._line_label_items):
            try:
                self.scene.removeItem(it)
            except Exception:
                logger.debug("Suppressed error", exc_info=True)
        self._line_items = []
        self._line_label_items = []
        rect = self.scene.sceneRect()
        self._render_lines_overlay_simple(float(rect.width()), float(rect.height()))

    def _rebuild_scene_from_cache(self) -> None:
        cached_meta = [(str(tid), list(pts), str(group or ""), str(cls_name or "")) for _gitem, tid, pts, group, cls_name in self._track_items_meta]
        video_path = _resolve_existing_path(self.video_input.text().strip()) if self.video_input.text().strip() else None
        try:
            self._ensure_base_matches_video(video_path)
        except Exception:
            logger.debug("Suppressed error", exc_info=True)
        bg_item, bg_w, bg_h = _load_background(video_path, self.resize_target) if self._bg_enabled else (None, 0.0, 0.0)
        self._bg_item = bg_item
        self._bg_size = (bg_w, bg_h)
        self.scene.clear()
        self._line_items = []
        self._line_label_items = []
        self._track_items = []
        self._track_items_meta = []
        if bg_item is not None:
            bg_item.setZValue(0)
            self.scene.addItem(bg_item)
        base = self._lines_base_size
        if not (base and base[0] > 0 and base[1] > 0):
            probed = _probe_video_size(video_path)
            if probed:
                base = probed
                self._lines_base_size = probed
        if self.resize_target and self.resize_target[0] > 0 and self.resize_target[1] > 0:
            scene_w, scene_h = float(self.resize_target[0]), float(self.resize_target[1])
        elif bg_w > 0 and bg_h > 0:
            scene_w, scene_h = bg_w, bg_h
        elif base and base[0] > 0 and base[1] > 0:
            scene_w, scene_h = float(base[0]), float(base[1])
        else:
            scene_w, scene_h = 900.0, 650.0
        self.scene.setSceneRect(0, 0, scene_w, scene_h)
        self._scale_sx = float(scene_w) / float(base[0]) if base and base[0] > 0 else 1.0
        self._scale_sy = float(scene_h) / float(base[1]) if base and base[1] > 0 else 1.0
        self._render_lines_overlay_simple(scene_w, scene_h)
        self.view.set_fit_rect(self.scene.sceneRect())
        self._color_idx = 0
        self._on_tracks_batch(list(cached_meta), self._loading_token)

    def _apply_geometry_str(self, s: str) -> None:
        m = re.match(r"^(\d+)x(\d+)\+(-?\d+)\+(-?\d+)$", s.strip())
        if not m:
            return
        # 저장된 크기가 현재 화면보다 크면(모니터 교체 등) 화면 안으로 줄인다.
        fit_to_screen(self, int(m.group(1)), int(m.group(2)))
        self.move(int(m.group(3)), int(m.group(4)))

    def _show_msg(self, title: str, text: str, is_warning: bool = False) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle(title)
        dlg.setModal(True)
        dlg.setMinimumWidth(360)
        dlg.setMaximumWidth(560)
        dlg.setStyleSheet(DARK_DIALOG_STYLE)
        layout = QVBoxLayout()
        layout.setContentsMargins(16, 14, 16, 12)
        layout.setSpacing(12)
        body = QLabel(str(text))
        body.setWordWrap(True)
        body.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        body.setTextInteractionFlags(Qt.TextSelectableByMouse)
        if is_warning:
            body.setStyleSheet("color: #ffd6d6;")
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        btns.accepted.connect(dlg.accept)
        layout.addWidget(body)
        layout.addWidget(btns)
        dlg.setLayout(layout)
        dlg.exec()

    def _toggle_merge_relaxed_mode(self, checked: bool) -> None:
        self._merge_relaxed_mode = bool(checked)
        self.btn_merge_relaxed.setChecked(self._merge_relaxed_mode)
        self._persist_options()
        mode_text = "ON" if self._merge_relaxed_mode else "OFF"
        self.status_label.setText(f"병합 적용 모드: {mode_text}")
        if self._apply_merge_filter:
            self._reload_all()

    def _toggle_merge_debug_mode(self, checked: bool) -> None:
        self._merge_debug_mode = bool(checked)
        self.btn_merge_debug.setChecked(self._merge_debug_mode)
        self.status_label.setText("병합 디버그 ON - 상세 로그" if self._merge_debug_mode else "병합 디버그 OFF")

    def _toggle_manual_pick_mode(self, checked: bool) -> None:
        self._manual_pick_mode = bool(checked)
        self.btn_manual_pick.setChecked(self._manual_pick_mode)
        self.status_label.setText("수동 선택 ON - 작업 준비" if self._manual_pick_mode else "수동 선택 OFF")

    def _clear_manual_selection(self) -> None:
        self._manual_selected_track_ids = []
        self._apply_merged_highlight_styles()
        self.status_label.setText("수동 선택 해제")

    def _toggle_manual_track_selection(self, tid: str) -> None:
        tid_s = str(tid)
        current = [str(x) for x in list(self._manual_selected_track_ids or [])]
        if tid_s in current:
            current = [x for x in current if x != tid_s]
        else:
            current.append(tid_s)
            if len(current) > 2:
                current = current[-2:]
        self._manual_selected_track_ids = current
        self._apply_merged_highlight_styles()
        self.status_label.setText(f"수동 선택: {', '.join(current) if current else '-'}")

    def _pick_track_meta_at(self, scene_x: float, scene_y: float) -> Optional[Tuple[QGraphicsPathItem, str, List[Tuple[float, float]], str, str]]:
        rect = QRectF(float(scene_x) - 8.0, float(scene_y) - 8.0, 16.0, 16.0)
        try:
            items = self.scene.items(rect, Qt.ItemSelectionMode.IntersectsItemShape)
        except Exception:
            items = self.scene.items(rect)
        item_ids = {id(it) for it in items}
        for meta in self._track_items_meta:
            if id(meta[0]) in item_ids:
                return meta
        if not self._track_items_meta:
            return None
        bx = float(scene_x) / float(self._scale_sx or 1.0)
        by = float(scene_y) / float(self._scale_sy or 1.0)
        best_meta = None
        best_dist = None
        for meta in self._track_items_meta:
            pts = meta[2]
            if not pts:
                continue
            dist = min((float(px) - bx) ** 2 + (float(py) - by) ** 2 for px, py in pts)
            if best_dist is None or dist < best_dist:
                best_dist = dist
                best_meta = meta
        return best_meta

    def _build_merge_debug_text(self, tid: str, pts: List[Tuple[float, float]], group: str, cls_name: str) -> str:
        if not self._merge_cfg:
            return "병합 설정이 없습니다."
        lines_map = self._get_line_map()
        src = str(self._merge_cfg.get("src_bound", "") or "")
        tgt = str(self._merge_cfg.get("tgt_bound", "") or "")
        movement_type = str(self._merge_cfg.get("movement_type", "straight") or "straight")
        relaxed_merge = bool(self._merge_relaxed_mode)
        src_line = lines_map.get(src)
        tgt_line = lines_map.get(tgt) if tgt != "(?꾩껜 ?쇱씤)" else None
        ref_dir = self._movement_ref_dir(src_line, tgt_line) if (src_line and tgt_line) else None
        pivot = self._movement_pivot(src_line, tgt_line) if (src_line and tgt_line) else None
        pass_src = self._passes_line(pts, src_line) if src_line else False
        pass_tgt = self._passes_line(pts, tgt_line) if tgt_line else False
        passed_ids = ", ".join(self._track_passed_line_ids(pts)) or "-"
        other_hits = [
            other_name
            for other_name, other_line in lines_map.items()
            if other_name not in (src, tgt) and self._passes_line(pts, other_line)
        ]
        _tx, _ty, tail_vx, tail_vy = self._get_stable_vector(pts)
        _hx, _hy, head_vx, head_vy = self._get_head_vector(pts)
        tail_dir = self._vector_to_dir_label(tail_vx, tail_vy)
        head_dir = self._vector_to_dir_label(head_vx, head_vy)
        dir_ok_a = self._track_same_direction(pts, ref_dir, min_cos=0.40 if relaxed_merge else 0.50, from_head=False, window=20)
        dir_ok_b = self._track_same_direction(pts, ref_dir, min_cos=0.40 if relaxed_merge else 0.50, from_head=True, window=20)
        dir_prog_a = self._window_progresses_along_direction(pts, ref_dir, from_head=False, window=20, min_forward_ratio=0.65 if relaxed_merge else 0.75, min_net_progress=20.0 if relaxed_merge else 35.0)
        dir_prog_b = self._window_progresses_along_direction(pts, ref_dir, from_head=True, window=20, min_forward_ratio=0.65 if relaxed_merge else 0.75, min_net_progress=20.0 if relaxed_merge else 35.0)
        turn_amount = self._turn_amount(pts)
        min_turn = 0.10 if relaxed_merge else 0.18
        movement_match = self._turn_matches_movement(turn_amount, movement_type, min_turn)
        cond_a = False
        cond_b = False
        if movement_type in ("left_turn", "right_turn"):
            cond_a = pass_src and (not pass_tgt) and self._same_direction(tail_vx, tail_vy, ref_dir, min_cos=0.15 if relaxed_merge else 0.25) and movement_match and len(other_hits) <= (2 if relaxed_merge else 1) and self._point_near(pts[-1], pivot, 180.0 if relaxed_merge else 130.0)
            cond_b = (not pass_src) and pass_tgt and self._same_direction(head_vx, head_vy, ref_dir, min_cos=0.15 if relaxed_merge else 0.25) and movement_match and len(other_hits) <= (2 if relaxed_merge else 1) and self._point_near(pts[0], pivot, 180.0 if relaxed_merge else 130.0)
        else:
            pass_other = len(other_hits) > 0
            if not pass_other:
                if relaxed_merge:
                    cond_a = (
                        pass_src
                        and (not pass_tgt)
                        and dir_ok_a
                        and dir_prog_a
                        and self._same_direction(tail_vx, tail_vy, ref_dir, min_cos=0.35)
                        and (tgt_line is None or self._is_heading_towards(pts, tgt_line))
                    )
                    cond_b = (
                        (not pass_src)
                        and pass_tgt
                        and dir_ok_b
                        and dir_prog_b
                        and self._same_direction(head_vx, head_vy, ref_dir, min_cos=0.35)
                        and (src_line is None or self._is_coming_from(pts, src_line))
                    )
                else:
                    cond_a = pass_src and (not pass_tgt) and dir_ok_a and dir_prog_a and self._same_direction(tail_vx, tail_vy, ref_dir, min_cos=0.45) and (tgt_line is None or self._is_heading_towards(pts, tgt_line))
                    if cond_a:
                        for other_name, other_line in lines_map.items():
                            if other_name in (src, tgt):
                                continue
                            if self._is_coming_from(pts, other_line):
                                cond_a = False
                                break
                    cond_b = (not pass_src) and pass_tgt and dir_ok_b and dir_prog_b and self._same_direction(head_vx, head_vy, ref_dir, min_cos=0.45) and (src_line is None or self._is_coming_from(pts, src_line))
                    if cond_b:
                        for other_name, other_line in lines_map.items():
                            if other_name in (src, tgt):
                                continue
                            if self._is_heading_towards(pts, other_line):
                                cond_b = False
                                break
        recomputed_group = "A" if cond_a else ("B" if cond_b else "-")
        return "\n".join([
            f"track_id={tid}  class={cls_name or '-'}  rendered_group={group or '-'}  recomputed_group={recomputed_group}",
            f"movement={movement_type}({self._movement_label(movement_type)})  relaxed={relaxed_merge}  movement_match={movement_match}",
            f"passed_lines={passed_ids}",
            f"pass_src={pass_src}  pass_tgt={pass_tgt}  other_hits={len(other_hits)} [{', '.join(other_hits) or '-'}]",
            f"tail_vec=({tail_vx:.1f}, {tail_vy:.1f}) [{tail_dir}]  head_vec=({head_vx:.1f}, {head_vy:.1f}) [{head_dir}]  turn={turn_amount:.3f}",
            f"dir_ok_a={dir_ok_a}  dir_prog_a={dir_prog_a}  heading_tgt={self._is_heading_towards(pts, tgt_line) if tgt_line else False}",
            f"dir_ok_b={dir_ok_b}  dir_prog_b={dir_prog_b}  coming_src={self._is_coming_from(pts, src_line) if src_line else False}",
            f"final_cond_a={cond_a}  final_cond_b={cond_b}",
        ])

    def _refresh_sessions(self, select: Optional[str] = None) -> None:
        sessions = _load_sessions(Path(self.db_input.text().strip() or "output/tracks.sqlite"))
        self.session_combo.blockSignals(True)
        self.session_combo.clear()
        self.session_combo.addItem("(선택)")
        for s in sessions:
            self.session_combo.addItem(s)
        if select and select in sessions:
            self.session_combo.setCurrentText(select)
        elif sessions:
            self.session_combo.setCurrentText(sessions[-1])
        self.session_combo.blockSignals(False)
        if self._restore_session_id:
            idx = self.session_combo.findText(self._restore_session_id)
            if idx >= 0:
                self.session_combo.setCurrentIndex(idx)
            self._restore_session_id = None
        self._refresh_slots()

    def _refresh_slots(self) -> None:
        db_path = Path(self.db_input.text().strip() or "output/tracks.sqlite")
        session_id = None if self.session_combo.currentIndex() <= 0 else self.session_combo.currentText().strip()
        slots: List[int] = []
        if db_path.exists():
            try:
                with sqlite3.connect(db_path) as conn:
                    sql = "select distinct (start_ts_ms / 900000) as slot from track_trajs where start_ts_ms is not null"
                    params: List[object] = []
                    if session_id:
                        sql += " and session_id = ?"
                        params.append(session_id)
                    sql += " order by slot"
                    rows = conn.execute(sql, tuple(params)).fetchall()
                    slots = [int(r[0]) for r in rows if r and r[0] is not None]
            except Exception:
                slots = []
        self.slot_combo.blockSignals(True)
        self.slot_combo.clear()
        self.slot_combo.addItem("(선택)", None)
        for s in slots[:5000]:
            start_min = int(s * 15)
            end_min = start_min + 15
            self.slot_combo.addItem(f"{s} ({start_min}~{end_min}遺?", int(s))
        self.slot_combo.blockSignals(False)
        if self._restore_slot_index is not None:
            idx2 = self.slot_combo.findData(int(self._restore_slot_index))
            if idx2 >= 0:
                self.slot_combo.setCurrentIndex(idx2)
            self._restore_slot_index = None

    def _load_lines_from_path(self) -> None:
        self._lines_raw, self._lines_base_size_raw = _read_lines(Path(self.lines_input.text().strip() or "config/lines.json"))
        self._lines = self._lines_raw
        self._lines_base_size = self._lines_base_size_raw
        self._clear_pending_points()
        self._refresh_lines_list()

    def _get_current_image_size_int(self) -> Optional[Tuple[int, int]]:
        if self._lines_base_size and self._lines_base_size[0] > 0 and self._lines_base_size[1] > 0:
            return (int(self._lines_base_size[0]), int(self._lines_base_size[1]))
        video_path = Path(self.video_input.text().strip()) if self.video_input.text().strip() else None
        return _probe_video_size(video_path)

    def _ensure_base_matches_video(self, video_path: Optional[Path]) -> None:
        probed = _probe_video_size(video_path)
        if not probed:
            return
        new_w, new_h = int(probed[0]), int(probed[1])
        base = self._lines_base_size_raw
        if not (base and base[0] > 0 and base[1] > 0):
            self._lines_base_size_raw = (new_w, new_h)
            return
        old_w, old_h = int(base[0]), int(base[1])
        if old_w == new_w and old_h == new_h:
            return
        for ln in self._lines_raw:
            ln.points = _rescale_points(ln.points, (old_w, old_h), (new_w, new_h))
            if ln.in_point and len(ln.in_point) >= 2:
                scaled = _rescale_points([[float(ln.in_point[0]), float(ln.in_point[1])]], (old_w, old_h), (new_w, new_h))
                if scaled and len(scaled[0]) >= 2:
                    ln.in_point = [float(scaled[0][0]), float(scaled[0][1])]
        if self._pending_line_points:
            tmp = [[float(x), float(y)] for x, y in self._pending_line_points]
            tmp2 = _rescale_points(tmp, (old_w, old_h), (new_w, new_h))
            self._pending_line_points = [(float(p[0]), float(p[1])) for p in tmp2 if len(p) >= 2]
        self._lines_base_size_raw = (new_w, new_h)
        self._refresh_lines_list()

    def _save_lines(self) -> None:
        try:
            lines_path = Path(self.lines_input.text().strip() or "config/lines.json")
            video_path = Path(self.video_input.text().strip()) if self.video_input.text().strip() else None
            self._ensure_base_matches_video(video_path)
            _write_lines(lines_path, self._lines, self._lines_base_size_raw)
            self._show_msg("Lines 저장", f"저장됨:\n{lines_path}")
        except Exception as exc:
            self._show_msg("저장 오류", str(exc), True)

    def _on_config_merge(self) -> None:
        dlg = MergeConfigDialog(self, [ln.line_id for ln in self._lines if ln.line_id])
        if self._merge_cfg:
            try:
                idx_src = dlg.cb_src_line.findText(str(self._merge_cfg.get("src_bound", "")))
                if idx_src >= 0:
                    dlg.cb_src_line.setCurrentIndex(idx_src)
                idx_tgt = dlg.cb_tgt_line.findText(str(self._merge_cfg.get("tgt_bound", "")))
                if idx_tgt >= 0:
                    dlg.cb_tgt_line.setCurrentIndex(idx_tgt)
                movement_type = str(self._merge_cfg.get("movement_type", "straight") or "straight")
                idx_mv = dlg.cb_movement_type.findData(movement_type)
                if idx_mv >= 0:
                    dlg.cb_movement_type.setCurrentIndex(idx_mv)
                dlg.spin_dist.setValue(float(self._merge_cfg.get("reconnect_dist", 50.0)))
                dlg.spin_gap.setValue(float(self._merge_cfg.get("reconnect_gap", 3.0)))
                dlg.spin_passes.setValue(int(self._merge_cfg.get("reconnect_passes", 2)))
            except Exception:
                logger.debug("Suppressed error", exc_info=True)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._merge_cfg = dlg.get_config()
            self._merge_relaxed_mode = False
            self.btn_merge_relaxed.setChecked(False)
            self._persist_options()
            self._on_run_merge()
            self.btn_preview_merge.setEnabled(True)
            self.btn_cancel_merge.setEnabled(True)

    def _on_config_extrap(self) -> None:
        dlg = ExtrapConfigDialog(self, [ln.line_id for ln in self._lines if ln.line_id])
        if self._extrap_cfg:
            try:
                idx_tgt = dlg.cb_target.findText(str(self._extrap_cfg.get("target_bound", "")))
                if idx_tgt >= 0:
                    dlg.cb_target.setCurrentIndex(idx_tgt)
                dlg.spin_horizon.setValue(float(self._extrap_cfg.get("extrap_horizon", 200.0)))
            except Exception:
                logger.debug("Suppressed error", exc_info=True)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._extrap_cfg = dlg.get_config()
            self.btn_preview_extrap.setEnabled(True)
            self.btn_run_extrap.setEnabled(True)
            self.btn_cancel_extrap.setEnabled(True)
            self._persist_options()
            self._show_msg("외삽 설정", f"외삽 설정이 저장되었습니다.\n{self._extrap_cfg}")

    def _get_line_map(self) -> Dict[str, LineDef]:
        return {ln.line_id: ln for ln in self._lines if ln.line_id}

    def _get_extrap_direction(self) -> Tuple[Optional[LineDef], Optional[LineDef]]:
        lines_map = self._get_line_map()
        src_id = str(self._merge_cfg.get("src_bound", "") or "").strip()
        tgt_id = str(self._extrap_cfg.get("target_bound", "") or self._merge_cfg.get("tgt_bound", "") or "").strip()
        return lines_map.get(src_id), lines_map.get(tgt_id)

    def _intersects(self, a, b, c, d) -> bool:
        def ccw(p1, p2, p3):
            return (p3[1] - p1[1]) * (p2[0] - p1[0]) > (p2[1] - p1[1]) * (p3[0] - p1[0])
        return ccw(a, c, d) != ccw(b, c, d) and ccw(a, b, c) != ccw(a, b, d)

    def _passes_line(self, pts: List[Tuple[float, float]], line: object) -> bool:
        if not pts or len(pts) < 2 or not line or len(line.points) < 2:
            return False
        line_pts = [(float(p[0]), float(p[1])) for p in line.points]
        for idx in range(len(pts) - 1):
            a = pts[idx]
            b = pts[idx + 1]
            for j in range(len(line_pts) - 1):
                if self._intersects(a, b, line_pts[j], line_pts[j + 1]):
                    return True
        return False

    def _raycast_hit(self, px: float, py: float, vx: float, vy: float, target_line: object) -> bool:
        if not target_line or len(target_line.points) < 2:
            return False
        for seg_idx in range(len(target_line.points) - 1):
            ax, ay = float(target_line.points[seg_idx][0]), float(target_line.points[seg_idx][1])
            bx, by = float(target_line.points[seg_idx + 1][0]), float(target_line.points[seg_idx + 1][1])
            mx, my = (ax + bx) / 2.0, (ay + by) / 2.0
            if vx * (mx - px) + vy * (my - py) <= 0:
                continue
            dx_ = bx - ax
            dy_ = by - ay
            det = dx_ * vy - dy_ * vx
            if abs(det) < 1e-6:
                continue
            t = (dx_ * (ay - py) - dy_ * (ax - px)) / det
            u = (vx * (ay - py) - vy * (ax - px)) / det
            if t > 0 and -0.4 <= u <= 1.4:
                return True
        return False

    def _line_center(self, line: object) -> Optional[Tuple[float, float]]:
        if not line or not getattr(line, "points", None):
            return None
        pts = []
        for raw in line.points:
            try:
                pts.append((float(raw[0]), float(raw[1])))
            except Exception:
                continue
        if len(pts) < 2:
            return None
        return (
            sum(p[0] for p in pts) / float(len(pts)),
            sum(p[1] for p in pts) / float(len(pts)),
        )

    def _movement_ref_dir(self, src_line: object, tgt_line: object) -> Optional[Tuple[float, float]]:
        src_center = self._line_center(src_line)
        tgt_center = self._line_center(tgt_line)
        if src_center is None or tgt_center is None:
            return None
        dx = float(tgt_center[0] - src_center[0])
        dy = float(tgt_center[1] - src_center[1])
        norm = (dx * dx + dy * dy) ** 0.5
        if norm < 1e-9:
            return None
        return (dx / norm, dy / norm)

    def _line_endpoints(self, line: object) -> Optional[Tuple[Tuple[float, float], Tuple[float, float]]]:
        if not line or not getattr(line, "points", None):
            return None
        pts = []
        for raw in line.points:
            try:
                pts.append((float(raw[0]), float(raw[1])))
            except Exception:
                continue
        if len(pts) < 2:
            return None
        return (pts[0], pts[-1])

    def _line_intersection_point(
        self,
        a1: Tuple[float, float],
        a2: Tuple[float, float],
        b1: Tuple[float, float],
        b2: Tuple[float, float],
    ) -> Optional[Tuple[float, float]]:
        ax, ay = a1
        bx, by = a2
        cx, cy = b1
        dx, dy = b2
        denom = (bx - ax) * (dy - cy) - (by - ay) * (dx - cx)
        if abs(denom) < 1e-9:
            return None
        t = ((cx - ax) * (dy - cy) - (cy - ay) * (dx - cx)) / denom
        return (float(ax + t * (bx - ax)), float(ay + t * (by - ay)))

    def _movement_pivot(self, src_line: object, tgt_line: object) -> Optional[Tuple[float, float]]:
        src_ends = self._line_endpoints(src_line)
        tgt_ends = self._line_endpoints(tgt_line)
        if src_ends and tgt_ends:
            hit = self._line_intersection_point(src_ends[0], src_ends[1], tgt_ends[0], tgt_ends[1])
            if hit is not None:
                return hit
        src_center = self._line_center(src_line)
        tgt_center = self._line_center(tgt_line)
        if src_center is None or tgt_center is None:
            return None
        return ((src_center[0] + tgt_center[0]) / 2.0, (src_center[1] + tgt_center[1]) / 2.0)

    def _point_near(self, point: Tuple[float, float], target: Optional[Tuple[float, float]], radius_px: float) -> bool:
        if target is None:
            return True
        dx = float(point[0] - target[0])
        dy = float(point[1] - target[1])
        return (dx * dx + dy * dy) <= float(radius_px * radius_px)

    def _point_near_movement_axis(
        self,
        point: Tuple[float, float],
        src_line: object,
        tgt_line: object,
        corridor_px: float,
        extend_px: float = 120.0,
    ) -> bool:
        src_center = self._line_center(src_line)
        tgt_center = self._line_center(tgt_line)
        if src_center is None or tgt_center is None:
            return True
        ax = float(src_center[0])
        ay = float(src_center[1])
        bx = float(tgt_center[0])
        by = float(tgt_center[1])
        abx = bx - ax
        aby = by - ay
        ab2 = abx * abx + aby * aby
        if ab2 < 1e-9:
            return True
        px = float(point[0])
        py = float(point[1])
        apx = px - ax
        apy = py - ay
        t = (apx * abx + apy * aby) / ab2
        seg_len = ab2 ** 0.5
        if seg_len > 1e-9:
            t_margin = float(extend_px) / seg_len
            if t < -t_margin or t > 1.0 + t_margin:
                return False
        proj_t = max(0.0, min(1.0, t))
        qx = ax + proj_t * abx
        qy = ay + proj_t * aby
        dx = px - qx
        dy = py - qy
        return (dx * dx + dy * dy) <= float(corridor_px * corridor_px)

    def _point_prefers_selected_line(
        self,
        point: Tuple[float, float],
        selected_line: object,
        other_lines: List[object],
        margin_px: float,
    ) -> bool:
        selected_dist = _point_to_line_distance(point, selected_line)
        if not math.isfinite(selected_dist):
            return False
        other_dists = [_point_to_line_distance(point, line) for line in other_lines]
        other_dists = [float(d) for d in other_dists if math.isfinite(d)]
        if not other_dists:
            return True
        return selected_dist <= (min(other_dists) + float(margin_px))

    def _same_direction(self, vx: float, vy: float, ref_dir: Optional[Tuple[float, float]], min_cos: float = 0.2) -> bool:
        if ref_dir is None:
            return True
        norm = (vx * vx + vy * vy) ** 0.5
        if norm < 1e-9:
            return False
        cos_v = (vx * float(ref_dir[0]) + vy * float(ref_dir[1])) / norm
        return cos_v >= float(min_cos)

    def _window_progresses_along_direction(
        self,
        pts: List[Tuple[float, float]],
        ref_dir: Optional[Tuple[float, float]],
        from_head: bool = False,
        window: int = 20,
        min_forward_ratio: float = 0.7,
        min_net_progress: float = 30.0,
    ) -> bool:
        if ref_dir is None:
            return True
        if not pts or len(pts) < 2:
            return False
        sample = pts[: max(2, int(window))] if from_head else pts[-max(2, int(window)) :]
        if len(sample) < 2:
            return False
        rx = float(ref_dir[0])
        ry = float(ref_dir[1])
        projs = [float(p[0]) * rx + float(p[1]) * ry for p in sample]
        deltas = [projs[i + 1] - projs[i] for i in range(len(projs) - 1)]
        if not deltas:
            return False
        forward_steps = sum(1 for d in deltas if d > 0.0)
        forward_ratio = float(forward_steps) / float(len(deltas))
        net_progress = projs[-1] - projs[0]
        return forward_ratio >= float(min_forward_ratio) and net_progress >= float(min_net_progress)

    def _track_same_direction(
        self,
        pts: List[Tuple[float, float]],
        ref_dir: Optional[Tuple[float, float]],
        min_cos: float = 0.35,
        from_head: bool = False,
        window: int = 20,
    ) -> bool:
        if ref_dir is None:
            return True
        if not pts or len(pts) < 2:
            return False
        if from_head:
            end_idx = min(len(pts) - 1, max(1, int(window) - 1))
            dx = float(pts[end_idx][0] - pts[0][0])
            dy = float(pts[end_idx][1] - pts[0][1])
        else:
            start_idx = max(0, len(pts) - max(2, int(window)))
            dx = float(pts[-1][0] - pts[start_idx][0])
            dy = float(pts[-1][1] - pts[start_idx][1])
        return self._same_direction(dx, dy, ref_dir, min_cos=min_cos)

    def _get_stable_vector(self, pts: List[Tuple[float, float]]) -> Tuple[float, float, float, float]:
        if not pts or len(pts) < 2:
            return 0.0, 0.0, 0.0, 0.0
        total_dx = pts[-1][0] - pts[0][0]
        total_dy = pts[-1][1] - pts[0][1]
        if (total_dx * total_dx + total_dy * total_dy) ** 0.5 < 60.0:
            return 0.0, 0.0, 0.0, 0.0
        n_idx = max(0, len(pts) - 15)
        vx = pts[-1][0] - pts[n_idx][0]
        vy = pts[-1][1] - pts[n_idx][1]
        if (vx * vx + vy * vy) ** 0.5 < 5.0:
            vx = total_dx
            vy = total_dy
        return pts[-1][0], pts[-1][1], vx, vy

    def _get_head_vector(self, pts: List[Tuple[float, float]]) -> Tuple[float, float, float, float]:
        if not pts or len(pts) < 2:
            return 0.0, 0.0, 0.0, 0.0
        total_dx = pts[-1][0] - pts[0][0]
        total_dy = pts[-1][1] - pts[0][1]
        if (total_dx * total_dx + total_dy * total_dy) ** 0.5 < 60.0:
            return 0.0, 0.0, 0.0, 0.0
        end_idx = min(len(pts) - 1, 14)
        vx = pts[end_idx][0] - pts[0][0]
        vy = pts[end_idx][1] - pts[0][1]
        if (vx * vx + vy * vy) ** 0.5 < 5.0:
            vx = total_dx
            vy = total_dy
        return pts[0][0], pts[0][1], vx, vy

    def _turn_amount(self, pts: List[Tuple[float, float]]) -> float:
        _hx, _hy, head_vx, head_vy = self._get_head_vector(pts)
        _tx, _ty, tail_vx, tail_vy = self._get_stable_vector(pts)
        head_norm = (head_vx * head_vx + head_vy * head_vy) ** 0.5
        tail_norm = (tail_vx * tail_vx + tail_vy * tail_vy) ** 0.5
        if head_norm < 1e-9 or tail_norm < 1e-9:
            return 0.0
        return float((head_vx * tail_vy - head_vy * tail_vx) / (head_norm * tail_norm))

    def _turn_matches_movement(self, turn_amount: float, movement_type: str, min_turn: float) -> bool:
        if movement_type == "left_turn":
            return float(turn_amount) <= -float(min_turn)
        if movement_type == "right_turn":
            return float(turn_amount) >= float(min_turn)
        return abs(float(turn_amount)) < float(min_turn)

    def _is_heading_towards(self, pts: List[Tuple[float, float]], tgt_line: object) -> bool:
        px, py, vx, vy = self._get_stable_vector(pts)
        if vx == 0.0 and vy == 0.0:
            return False
        return self._raycast_hit(px, py, vx, vy, tgt_line)

    def _is_coming_from(self, pts: List[Tuple[float, float]], src_line: object) -> bool:
        _px, _py, vx, vy = self._get_stable_vector(pts)
        if vx == 0.0 and vy == 0.0:
            return False
        px_start, py_start = pts[0][0], pts[0][1]
        return self._raycast_hit(px_start, py_start, -vx, -vy, src_line)

    def _load_saved_merge_track_ids(self, db_path: Path, session_id: Optional[str]) -> set[str]:
        if not session_id:
            return set()
        try:
            merge_map = load_effective_track_merge_map(db_path, session_id)
        except Exception:
            logger.debug("Suppressed error", exc_info=True)
            return set()
        saved: set[str] = set()
        for src, dst in dict(merge_map or {}).items():
            if src:
                saved.add(str(src))
            if dst:
                saved.add(str(dst))
        return saved

    def _load_saved_virtual_track_ids(self, db_path: Path, session_id: Optional[str]) -> set[str]:
        if not session_id or not db_path.exists():
            return set()
        out: set[str] = set()
        try:
            with sqlite3.connect(db_path) as conn:
                row = conn.execute(
                    "select 1 from sqlite_master where type='table' and name='track_virtual_events' limit 1"
                ).fetchone()
                if not row:
                    return set()
                for (track_id,) in conn.execute(
                    "select distinct track_id from track_virtual_events where session_id = ?",
                    (str(session_id),),
                ):
                    if track_id is not None:
                        out.add(str(track_id))
        except Exception:
            logger.debug("Suppressed error", exc_info=True)
            return set()
        return out

    def _iter_line_segments(self, line: Optional[LineDef]) -> List[Tuple[Tuple[float, float], Tuple[float, float]]]:
        if line is None or len(line.points) < 2:
            return []
        segs: List[Tuple[Tuple[float, float], Tuple[float, float]]] = []
        for idx in range(len(line.points) - 1):
            p1 = line.points[idx]
            p2 = line.points[idx + 1]
            segs.append(((float(p1[0]), float(p1[1])), (float(p2[0]), float(p2[1]))))
        return segs

    def _segments_intersect(
        self,
        a1: Tuple[float, float],
        a2: Tuple[float, float],
        b1: Tuple[float, float],
        b2: Tuple[float, float],
    ) -> bool:
        def ccw(p1, p2, p3) -> bool:
            return (p3[1] - p1[1]) * (p2[0] - p1[0]) > (p2[1] - p1[1]) * (p3[0] - p1[0])

        min_ax, max_ax = sorted((a1[0], a2[0]))
        min_ay, max_ay = sorted((a1[1], a2[1]))
        min_bx, max_bx = sorted((b1[0], b2[0]))
        min_by, max_by = sorted((b1[1], b2[1]))
        if max_ax < min_bx or max_bx < min_ax or max_ay < min_by or max_by < min_ay:
            return False
        return ccw(a1, b1, b2) != ccw(a2, b1, b2) and ccw(a1, a2, b1) != ccw(a1, a2, b2)

    def _track_passes_line(self, pts: List[Tuple[float, float]], line: Optional[LineDef]) -> bool:
        segs = self._iter_line_segments(line)
        if len(pts) < 2 or not segs:
            return False
        for idx in range(len(pts) - 1):
            a1 = (float(pts[idx][0]), float(pts[idx][1]))
            a2 = (float(pts[idx + 1][0]), float(pts[idx + 1][1]))
            for b1, b2 in segs:
                if self._segments_intersect(a1, a2, b1, b2):
                    return True
        return False

    def _track_passed_line_ids(self, pts: List[Tuple[float, float]]) -> List[str]:
        passed: List[str] = []
        for line_id, line in self._get_line_map().items():
            if self._track_passes_line(pts, line):
                passed.append(str(line_id))
        return passed

    def _stable_tail_vector(self, pts: List[Tuple[float, float]], tail_points: int = 12) -> Optional[Tuple[float, float, float, float]]:
        if len(pts) < 2:
            return None
        n_idx = max(0, len(pts) - max(2, int(tail_points)))
        px, py = float(pts[-1][0]), float(pts[-1][1])
        vx = px - float(pts[n_idx][0])
        vy = py - float(pts[n_idx][1])
        if math.hypot(vx, vy) < 5.0:
            vx = px - float(pts[0][0])
            vy = py - float(pts[0][1])
        if math.hypot(vx, vy) < 5.0:
            return None
        return px, py, vx, vy

    def _stable_head_vector(self, pts: List[Tuple[float, float]], head_points: int = 12) -> Optional[Tuple[float, float, float, float]]:
        if len(pts) < 2:
            return None
        n_idx = min(len(pts) - 1, max(1, int(head_points) - 1))
        px, py = float(pts[0][0]), float(pts[0][1])
        vx = px - float(pts[n_idx][0])
        vy = py - float(pts[n_idx][1])
        if math.hypot(vx, vy) < 5.0:
            vx = px - float(pts[-1][0])
            vy = py - float(pts[-1][1])
        if math.hypot(vx, vy) < 5.0:
            return None
        return px, py, vx, vy

    def _ray_hit_line(
        self,
        px: float,
        py: float,
        vx: float,
        vy: float,
        line: Optional[LineDef],
        max_dist: float,
    ) -> Optional[Tuple[float, float, float]]:
        segs = self._iter_line_segments(line)
        if not segs:
            return None
        best: Optional[Tuple[float, float, float]] = None
        v_norm = float(math.hypot(vx, vy))
        if v_norm <= 1e-6:
            return None
        for (ax, ay), (bx, by) in segs:
            dx_ = float(bx - ax)
            dy_ = float(by - ay)
            det = dx_ * float(vy) - dy_ * float(vx)
            if abs(det) < 1e-6:
                continue
            t = (dx_ * (ay - py) - dy_ * (ax - px)) / det
            u = (vx * (ay - py) - vy * (ax - px)) / det
            if t <= 0 or u < -1e-6 or u > 1.0 + 1e-6:
                continue
            dist = float(math.hypot(vx, vy)) * float(t)
            if max_dist > 0 and dist > float(max_dist):
                continue
            hit_x = px + float(vx) * float(t)
            hit_y = py + float(vy) * float(t)
            if best is None or dist < best[0]:
                best = (dist, float(hit_x), float(hit_y))
        if best is not None:
            return best

        # Fallback: if the ray does not exactly intersect the target line, allow
        # the nearest forward point on the target line when it stays within a
        # reasonable corridor around the ray direction.
        ux = float(vx) / v_norm
        uy = float(vy) / v_norm
        corridor_px = max(24.0, min(90.0, float(max_dist) * 0.18 if max_dist > 0 else 60.0))
        origin = (float(px), float(py))
        for (ax, ay), (bx, by) in segs:
            qx, qy, _t = _nearest_point_on_segment(origin, (ax, ay), (bx, by))
            dx = float(qx) - float(px)
            dy = float(qy) - float(py)
            along = (dx * ux) + (dy * uy)
            if along <= 0.0:
                continue
            lateral = abs((dx * uy) - (dy * ux))
            if lateral > corridor_px:
                continue
            dist = float(math.hypot(dx, dy))
            if max_dist > 0 and dist > float(max_dist):
                continue
            if best is None or dist < best[0]:
                best = (dist, float(qx), float(qy))
        return best

    def _line_normal(self, line: Optional[LineDef]) -> Optional[Tuple[float, float]]:
        if line is None or len(line.points) < 2:
            return None
        segs = self._iter_line_segments(line)
        if not segs:
            return None
        p1 = segs[0][0]
        p2 = segs[-1][1]
        dx = float(p2[0]) - float(p1[0])
        dy = float(p2[1]) - float(p1[1])
        norm = math.hypot(dx, dy)
        if norm < 1e-6:
            return None
        nx = -dy / norm
        ny = dx / norm
        in_point = getattr(line, "in_point", None)
        if isinstance(in_point, (list, tuple)) and len(in_point) >= 2:
            try:
                ix = float(in_point[0])
                iy = float(in_point[1])
                mx = sum(float(a[0] + b[0]) for a, b in segs) / float(len(segs) * 2)
                my = sum(float(a[1] + b[1]) for a, b in segs) / float(len(segs) * 2)
                side = (ix - mx) * nx + (iy - my) * ny
                if side < 0:
                    nx = -nx
                    ny = -ny
            except Exception:
                logger.debug("Suppressed error", exc_info=True)
        return (float(nx), float(ny))

    def _compute_inout(self, vx: float, vy: float, line: Optional[LineDef]) -> str:
        normal = self._line_normal(line)
        if normal is None:
            return "unk"
        return "in" if (float(vx) * normal[0] + float(vy) * normal[1]) >= 0 else "out"

    def _collect_extrap_candidates(self) -> List[Dict[str, object]]:
        src_line, tgt_line = self._get_extrap_direction()
        if src_line is None or tgt_line is None:
            return []
        db_path = Path(self.db_input.text().strip() or "output/tracks.sqlite")
        session_id = None if self.session_combo.currentIndex() <= 0 else self.session_combo.currentText().strip()
        excluded_merge_ids = self._load_saved_merge_track_ids(db_path, session_id)
        excluded_virtual_ids = self._load_saved_virtual_track_ids(db_path, session_id)
        horizon = float(self._extrap_cfg.get("extrap_horizon", 200.0) or 0.0)
        candidates: List[Dict[str, object]] = []
        for gitem, tid, pts, group, cls_name in list(self._track_items_meta):
            if len(pts) < 2:
                continue
            if str(tid) in excluded_merge_ids:
                continue
            if str(tid) in excluded_virtual_ids:
                continue
            passed_ids = self._track_passed_line_ids(pts)
            passed_set = set(passed_ids)
            if len(passed_set) != 1:
                continue

            role = ""
            target_line: Optional[LineDef] = None
            vec: Optional[Tuple[float, float, float, float]] = None
            method = ""
            if str(src_line.line_id) in passed_set and str(tgt_line.line_id) not in passed_set:
                role = "A"
                target_line = tgt_line
                vec = self._stable_tail_vector(pts)
                method = "viewer2_extrap_forward"
            elif str(tgt_line.line_id) in passed_set and str(src_line.line_id) not in passed_set:
                role = "B"
                target_line = src_line
                vec = self._stable_head_vector(pts)
                method = "viewer2_extrap_backward"
            else:
                continue

            if vec is None or target_line is None:
                continue
            px, py, vx, vy = vec
            hit = self._ray_hit_line(px, py, vx, vy, target_line, horizon)
            if hit is None:
                continue
            dist_px, hit_x, hit_y = hit
            draw_x, draw_y = _extend_hit_endpoint((float(px), float(py)), (float(hit_x), float(hit_y)))
            candidates.append(
                {
                    "role": role,
                    "method": method,
                    "item": gitem,
                    "track_id": str(tid),
                    "pts": list(pts),
                    "cls_name": str(cls_name or ""),
                    "passed_line_id": next(iter(passed_set)),
                    "target_line_id": str(target_line.line_id),
                    "target_bound": str(target_line.bound or ""),
                    "start_x": float(px),
                    "start_y": float(py),
                    "hit_x": float(draw_x),
                    "hit_y": float(draw_y),
                    "cross_x": float(hit_x),
                    "cross_y": float(hit_y),
                    "dist_px": float(dist_px),
                    "vx": float(vx),
                    "vy": float(vy),
                }
            )
        return candidates

    def _clear_extrap_preview(self) -> None:
        for item in list(self._extrap_preview_items):
            try:
                self.scene.removeItem(item)
            except Exception:
                logger.debug("Suppressed error", exc_info=True)
        self._extrap_preview_items = []

    def _apply_extrap_preview(self, candidates: List[Dict[str, object]]) -> None:
        self._clear_extrap_preview()
        self._apply_merged_highlight_styles()
        for cand in candidates:
            try:
                gitem = cand["item"]
                if str(cand.get("role")) == "B":
                    pen = QPen(QColor(255, 176, 64, 245))
                else:
                    pen = QPen(QColor(255, 240, 80, 245))
                pen.setWidthF(max(self.track_width + 1.2, 2.5))
                gitem.setPen(pen)
                gitem.setZValue(1.25)
            except Exception:
                logger.debug("Suppressed error", exc_info=True)
            sx = float(self._scale_sx or 1.0)
            sy = float(self._scale_sy or 1.0)
            x1 = float(cand["start_x"]) * sx
            y1 = float(cand["start_y"]) * sy
            x2 = float(cand["hit_x"]) * sx
            y2 = float(cand["hit_y"]) * sy
            seg = QGraphicsLineItem(x1, y1, x2, y2)
            seg_pen = QPen(QColor("#ff9640") if str(cand.get("role")) == "B" else QColor("#39d3ff"))
            seg_pen.setWidthF(2.4)
            seg_pen.setStyle(Qt.PenStyle.DashLine)
            seg.setPen(seg_pen)
            seg.setZValue(2.4)
            self.scene.addItem(seg)
            self._extrap_preview_items.append(seg)
            dot = QGraphicsEllipseItem(x2 - 4.0, y2 - 4.0, 8.0, 8.0)
            dot_pen = QPen(QColor("#ff9640") if str(cand.get("role")) == "B" else QColor("#39d3ff"))
            dot_pen.setWidthF(1.8)
            dot.setPen(dot_pen)
            dot.setBrush(QColor("#ff9640") if str(cand.get("role")) == "B" else QColor("#39d3ff"))
            dot.setZValue(2.5)
            self.scene.addItem(dot)
            self._extrap_preview_items.append(dot)

    def _on_preview_extrap(self) -> None:
        if not self._merge_cfg or not self._extrap_cfg:
            self._show_msg("외삽 설정", "외삽 설정이 먼저 필요합니다.", True)
            return
        candidates = self._collect_extrap_candidates()
        self._apply_extrap_preview(candidates)
        a_count = sum(1 for cand in candidates if str(cand.get("role")) == "A")
        b_count = sum(1 for cand in candidates if str(cand.get("role")) == "B")
        self.status_label.setText(f"외삽 미리보기: A->{a_count}개 B->{b_count}개")

    def _merged_track_member_ids(self, rep_tid: str) -> List[str]:
        rep = str(rep_tid)
        members = {rep}
        merge_map = dict(self._merged_source_to_parent or {})
        for src in list(merge_map.keys()):
            cur = str(src)
            seen: set[str] = set()
            while cur in merge_map and cur not in seen:
                seen.add(cur)
                nxt = str(merge_map[cur])
                if nxt == rep:
                    members.add(str(src))
                    break
                cur = nxt
        return sorted(members)

    def _load_merged_raw_points(self, db_path: Path, session_id: str, rep_tid: str) -> Tuple[str, List[List[float]]]:
        members = self._merged_track_member_ids(rep_tid)
        if not members:
            return "", []
        rows: List[Tuple[float, float, float, float]] = []
        camera_id = ""
        with sqlite3.connect(db_path) as conn:
            chunk = ",".join("?" for _ in members)
            sql = f"select track_id, camera_id, traj from track_trajs where session_id = ? and track_id in ({chunk})"
            params: List[object] = [str(session_id)] + list(members)
            for _tid, cam, blob in conn.execute(sql, tuple(params)):
                raw = decode_traj(blob) or []
                if raw and not camera_id:
                    camera_id = str(cam or "")
                for row in raw:
                    if not isinstance(row, list) or len(row) < 4:
                        continue
                    try:
                        rows.append((float(row[0]), float(row[1]), float(row[2]), float(row[3])))
                    except Exception:
                        continue
        rows.sort(key=lambda item: (item[1], item[0]))
        merged = [[float(fid), float(ts), float(x), float(y)] for fid, ts, x, y in rows]
        return camera_id, merged

    def _build_merged_raw_cache(
        self,
        db_path: Path,
        session_id: str,
        rep_tids: List[str],
    ) -> Dict[str, Tuple[str, List[List[float]]]]:
        rep_ids = sorted({str(tid) for tid in rep_tids if str(tid)})
        if not rep_ids:
            return {}
        member_to_rep: Dict[str, str] = {}
        all_member_ids: set[str] = set()
        for rep_tid in rep_ids:
            members = self._merged_track_member_ids(rep_tid)
            for member_id in members:
                all_member_ids.add(str(member_id))
                member_to_rep[str(member_id)] = str(rep_tid)

        rows_by_rep: Dict[str, List[Tuple[float, float, float, float]]] = {rep_tid: [] for rep_tid in rep_ids}
        camera_by_rep: Dict[str, str] = {rep_tid: "" for rep_tid in rep_ids}
        member_ids = sorted(all_member_ids)
        if not member_ids:
            return {}

        with sqlite3.connect(db_path) as conn:
            chunk_size = 900
            for idx in range(0, len(member_ids), chunk_size):
                chunk = member_ids[idx:idx + chunk_size]
                placeholders = ",".join("?" for _ in chunk)
                sql = f"select track_id, camera_id, traj from track_trajs where session_id = ? and track_id in ({placeholders})"
                params: List[object] = [str(session_id)] + list(chunk)
                for member_tid, cam, blob in conn.execute(sql, tuple(params)):
                    rep_tid = member_to_rep.get(str(member_tid))
                    if not rep_tid:
                        continue
                    raw = decode_traj(blob) or []
                    if raw and not camera_by_rep.get(rep_tid):
                        camera_by_rep[rep_tid] = str(cam or "")
                    for row in raw:
                        if not isinstance(row, list) or len(row) < 4:
                            continue
                        try:
                            rows_by_rep.setdefault(rep_tid, []).append((float(row[0]), float(row[1]), float(row[2]), float(row[3])))
                        except Exception:
                            continue

        out: Dict[str, Tuple[str, List[List[float]]]] = {}
        for rep_tid in rep_ids:
            rows = rows_by_rep.get(rep_tid) or []
            rows.sort(key=lambda item: (item[1], item[0]))
            out[rep_tid] = (
                str(camera_by_rep.get(rep_tid) or ""),
                [[float(fid), float(ts), float(x), float(y)] for fid, ts, x, y in rows],
            )
        return out

    def _estimate_extrap_event(
        self,
        db_path: Path,
        session_id: str,
        rep_tid: str,
        target_line: Optional[LineDef],
        horizon: float,
        backward: bool = False,
    ) -> Optional[Dict[str, object]]:
        camera_id, raw_pts = self._load_merged_raw_points(db_path, session_id, rep_tid)
        if len(raw_pts) < 2:
            return None
        xy_pts = [(float(row[2]), float(row[3])) for row in raw_pts]
        vec = self._stable_head_vector(xy_pts) if backward else self._stable_tail_vector(xy_pts)
        if vec is None:
            return None
        px, py, vx, vy = vec
        hit = self._ray_hit_line(px, py, vx, vy, target_line, horizon)
        if hit is None:
            return None
        dist_px, hit_x, hit_y = hit
        draw_x, draw_y = _extend_hit_endpoint((float(px), float(py)), (float(hit_x), float(hit_y)))
        if backward:
            prev = raw_pts[1]
            cur = raw_pts[0]
        else:
            prev = raw_pts[-2]
            cur = raw_pts[-1]
        seg_len = float(math.hypot(float(cur[2]) - float(prev[2]), float(cur[3]) - float(prev[3])))
        dt_ms = abs(float(cur[1]) - float(prev[1]))
        extra_ms = dt_ms * (float(dist_px) / seg_len) if seg_len > 1e-6 and dt_ms > 1e-6 else 0.0
        ts_ms = int(round(float(cur[1]) - extra_ms if backward else float(cur[1]) + extra_ms))
        return {
            "camera_id": str(camera_id or ""),
            "track_id": str(rep_tid),
            "ts_ms": int(ts_ms),
            "line_id": str(target_line.line_id if target_line else ""),
            "bound": str(target_line.bound if target_line else ""),
            "inout": self._compute_inout(vx, vy, target_line),
            "src_point_x": float(px),
            "src_point_y": float(py),
            "hit_x": float(draw_x),
            "hit_y": float(draw_y),
            "cross_x": float(hit_x),
            "cross_y": float(hit_y),
            "horizon": float(horizon),
        }

    def _estimate_extrap_event_from_raw(
        self,
        camera_id: str,
        raw_pts: List[List[float]],
        rep_tid: str,
        target_line: Optional[LineDef],
        horizon: float,
        backward: bool = False,
    ) -> Optional[Dict[str, object]]:
        if len(raw_pts) < 2:
            return None
        xy_pts = [(float(row[2]), float(row[3])) for row in raw_pts]
        vec = self._stable_head_vector(xy_pts) if backward else self._stable_tail_vector(xy_pts)
        if vec is None:
            return None
        px, py, vx, vy = vec
        hit = self._ray_hit_line(px, py, vx, vy, target_line, horizon)
        if hit is None:
            return None
        dist_px, hit_x, hit_y = hit
        draw_x, draw_y = _extend_hit_endpoint((float(px), float(py)), (float(hit_x), float(hit_y)))
        if backward:
            prev = raw_pts[1]
            cur = raw_pts[0]
        else:
            prev = raw_pts[-2]
            cur = raw_pts[-1]
        seg_len = float(math.hypot(float(cur[2]) - float(prev[2]), float(cur[3]) - float(prev[3])))
        dt_ms = abs(float(cur[1]) - float(prev[1]))
        extra_ms = dt_ms * (float(dist_px) / seg_len) if seg_len > 1e-6 and dt_ms > 1e-6 else 0.0
        ts_ms = int(round(float(cur[1]) - extra_ms if backward else float(cur[1]) + extra_ms))
        return {
            "camera_id": str(camera_id or ""),
            "track_id": str(rep_tid),
            "ts_ms": int(ts_ms),
            "line_id": str(target_line.line_id if target_line else ""),
            "bound": str(target_line.bound if target_line else ""),
            "inout": self._compute_inout(vx, vy, target_line),
            "src_point_x": float(px),
            "src_point_y": float(py),
            "hit_x": float(draw_x),
            "hit_y": float(draw_y),
            "cross_x": float(hit_x),
            "cross_y": float(hit_y),
            "horizon": float(horizon),
        }

    def _on_execute_extrap(self) -> None:
        if not self._merge_cfg or not self._extrap_cfg:
            self._show_msg("외삽 설정", "외삽 설정이 먼저 필요합니다.", True)
            return
        if self.session_combo.currentIndex() <= 0:
            self._show_msg("외삽 실패", "외삽 실행: 먼저 session_id를 선택해야 합니다.", True)
            return
        session_id = self.session_combo.currentText().strip()
        _src_line, tgt_line = self._get_extrap_direction()
        if _src_line is None or tgt_line is None:
            self._show_msg("외삽 실패", "외삽 실행: 소스/목표선을 모두 선택해야 합니다.", True)
            return
        candidates = self._collect_extrap_candidates()
        if not candidates:
            self._show_msg("외삽 실패", "외삽 후보가 없습니다.", False)
            return
        db_path = Path(self.db_input.text().strip() or "output/tracks.sqlite")
        init_db(db_path)
        horizon = float(self._extrap_cfg.get("extrap_horizon", 200.0) or 0.0)
        rows: List[Tuple[object, ...]] = []
        now = datetime.now().isoformat(timespec="seconds")
        raw_cache = self._build_merged_raw_cache(db_path, session_id, [str(cand["track_id"]) for cand in candidates])
        saved_a = 0
        saved_b = 0
        for cand in candidates:
            target_line = _src_line if str(cand.get("role")) == "B" else tgt_line
            camera_id, raw_pts = raw_cache.get(str(cand["track_id"]), ("", []))
            event = self._estimate_extrap_event_from_raw(
                camera_id,
                raw_pts,
                str(cand["track_id"]),
                target_line,
                horizon,
                backward=str(cand.get("role")) == "B",
            )
            if event is None:
                continue
            if str(cand.get("role")) == "B":
                saved_b += 1
            else:
                saved_a += 1
            rows.append(
                (
                    str(session_id),
                    str(event["camera_id"]),
                    str(event["track_id"]),
                    int(event["ts_ms"]),
                    str(event["line_id"]),
                    str(event["bound"]),
                    str(event["inout"]),
                    str(cand.get("method") or "viewer2_extrap_track_endpoint"),
                    float(event["horizon"]),
                    float(event["src_point_x"]),
                    float(event["src_point_y"]),
                    float(event["hit_x"]),
                    float(event["hit_y"]),
                    now,
                    json.dumps({"source": "trajectory_viewer2"}, ensure_ascii=False),
                )
            )
        if not rows:
            self._show_msg("외삽 실패", "외삽할 대상이 없습니다.", False)
            return
        with sqlite3.connect(db_path) as conn:
            delete_keys = sorted({(str(row[2]), str(row[7])) for row in rows})
            for track_id, method in delete_keys:
                conn.execute(
                    "delete from track_virtual_events where session_id = ? and track_id = ? and method = ?",
                    (str(session_id), str(track_id), str(method)),
                )
            conn.executemany(
                """
                insert or replace into track_virtual_events
                (session_id, camera_id, track_id, ts_ms, line_id, bound, inout, method, horizon, src_point_x, src_point_y, hit_x, hit_y, created_at, extra)
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            conn.commit()
        self._apply_extrap_preview(candidates)
        cand_a = sum(1 for cand in candidates if str(cand.get("role")) == "A")
        cand_b = sum(1 for cand in candidates if str(cand.get("role")) == "B")
        self.status_label.setText(
            f"외삽 결과: 후보 A={cand_a}, B={cand_b} -> 저장 A={saved_a}, B={saved_b}, 삭제={len(rows)}"
        )

    def _on_cancel_extrap(self) -> None:
        if self.session_combo.currentIndex() <= 0:
            self._show_msg("외삽 취소", "외삽 취소: 먼저 session_id를 선택해야 합니다.", True)
            return
        db_path = Path(self.db_input.text().strip() or "output/tracks.sqlite")
        session_id = self.session_combo.currentText().strip()
        if not db_path.exists():
            self._show_msg("외삽 취소", f"DB 파일을 찾을 수 없습니다.\n{db_path}", True)
            return
        reply = QMessageBox.question(
            self,
            "외삽 취소 확인",
            f"현재 session에 저장된 외삽 결과를 모두 삭제하시겠습니까?\n\nsession_id: {session_id}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        removed = 0
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "select 1 from sqlite_master where type='table' and name='track_virtual_events' limit 1"
            ).fetchone()
            if row:
                cur = conn.execute(
                    "delete from track_virtual_events where session_id = ?",
                    (str(session_id),),
                )
                removed += int(cur.rowcount or 0)
            conn.commit()
        self._clear_extrap_preview()
        self._extrap_cfg = {}
        self.btn_preview_extrap.setEnabled(False)
        self.btn_run_extrap.setEnabled(False)
        self.btn_cancel_extrap.setEnabled(False)
        self._persist_options()
        self._reload_all()
        self.status_label.setText(f"외삽 취소: {removed}개 삭제")

    def _on_run_merge(self) -> None:
        if not self._merge_cfg:
            self._show_msg("병합 설정", "먼저 병합 설정을 확인하세요.", True)
            return
        src = self._merge_cfg.get("src_bound", "")
        tgt = self._merge_cfg.get("tgt_bound", "")
        if src == "(라인 없음)" or tgt == "(라인 없음)":
            self._show_msg("병합 설정", "출발선 또는 도착선이 선택되지 않았습니다.", True)
            return
        lines_map = {ln.line_id: ln for ln in self._lines if ln.line_id}
        if src not in lines_map or tgt not in lines_map:
            self._show_msg("병합 설정", "선택한 라인을 찾을 수 없습니다.", True)
            return
        self._apply_merge_filter = {
            "src": src,
            "tgt": tgt,
            "movement_type": str(self._merge_cfg.get("movement_type", "straight") or "straight"),
            "lines_map": lines_map,
            "relaxed_merge": bool(self._merge_relaxed_mode),
        }
        move_label = {"straight": "직진", "left_turn": "좌회전", "right_turn": "우회전"}.get(
            str(self._merge_cfg.get("movement_type", "straight") or "straight"),
            "吏곸쭊",
        )
        self.status_label.setText(f"병합 필터 적용: '{src}' -> '{tgt}' [{move_label}]")
        self._reload_all()

    def _find_merge_pairs(self, group_a, group_b, dist_thresh, gap_thresh_sec, db_path_str, session_id: Optional[str] = None):
        tid_to_ts = {}
        tid_to_pairs = {}
        tids = list(set([str(m[1]) for m in group_a] + [str(m[1]) for m in group_b]))
        if tids:
            try:
                with sqlite3.connect(db_path_str) as conn:
                    chunk_size = 900
                    for i in range(0, len(tids), chunk_size):
                        chunk = tids[i:i + chunk_size]
                        places = ",".join("?" for _ in chunk)
                        sql = f"select track_id, traj from track_trajs where track_id in ({places})"
                        params: List[object] = list(chunk)
                        if session_id:
                            sql += " and session_id = ?"
                            params.append(str(session_id))
                        rows = conn.execute(sql, tuple(params)).fetchall()
                        for tid, blob in rows:
                            pts_raw = decode_traj(blob)
                            if pts_raw and len(pts_raw) >= 2:
                                tid_s = str(tid)
                                tid_to_ts[tid_s] = (float(pts_raw[0][1]), float(pts_raw[-1][1]))
                                tid_to_pairs[tid_s] = {
                                    "start": (
                                        (float(pts_raw[0][2]), float(pts_raw[0][3])),
                                        (float(pts_raw[1][2]), float(pts_raw[1][3])),
                                        float(pts_raw[0][1]) / 1000.0,
                                        float(pts_raw[1][1]) / 1000.0,
                                    ),
                                    "end": (
                                        (float(pts_raw[-2][2]), float(pts_raw[-2][3])),
                                        (float(pts_raw[-1][2]), float(pts_raw[-1][3])),
                                        float(pts_raw[-2][1]) / 1000.0,
                                        float(pts_raw[-1][1]) / 1000.0,
                                    ),
                                }
            except Exception:
                logger.debug("Suppressed error", exc_info=True)
        gap_thresh_ms = float(gap_thresh_sec) * 1000.0
        if bool(self._merge_relaxed_mode):
            dist_thresh = float(dist_thresh) * 2.2
            gap_thresh_ms = float(gap_thresh_ms) * 2.0
        candidates = []
        for i, (_gitem_a, tid_a, pts_a) in enumerate(group_a):
            if not pts_a:
                continue
            end_ax, end_ay = pts_a[-1]
            t_end_a = tid_to_ts.get(str(tid_a), (None, None))[1]
            for j, (_gitem_b, tid_b, pts_b) in enumerate(group_b):
                if not pts_b:
                    continue
                d = ((end_ax - pts_b[0][0]) ** 2 + (end_ay - pts_b[0][1]) ** 2) ** 0.5
                if d > dist_thresh:
                    continue
                t_start_b = tid_to_ts.get(str(tid_b), (None, None))[0]
                gap_ms = 0.0
                if t_end_a is not None and t_start_b is not None:
                    gap_ms = t_start_b - t_end_a
                    if not (-1500 <= gap_ms <= gap_thresh_ms + 1000):
                        continue
                effective_d = _effective_merge_distance(
                    (tid_to_pairs.get(str(tid_a)) or {}).get("end"),
                    (tid_to_pairs.get(str(tid_b)) or {}).get("start"),
                    max(0.0, float(gap_ms) / 1000.0),
                )
                if math.isfinite(effective_d):
                    d = min(float(d), float(effective_d))
                if d > dist_thresh:
                    continue
                cost = d + max(0, gap_ms) / 1000.0 * 5.0
                candidates.append((cost, i, j, d))
        candidates.sort(key=lambda x: x[0])
        paired_a = set()
        paired_b = set()
        results = []
        for _cost, i, j, d in candidates:
            if i in paired_a or j in paired_b:
                continue
            paired_a.add(i)
            paired_b.add(j)
            results.append((i, j, d))
        return results

    def _ensure_merged_tracks_table(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS merged_tracks (
              merge_id TEXT PRIMARY KEY,
              session_id TEXT,
              src_track_id TEXT,
              tgt_track_id TEXT,
              src_line TEXT,
              tgt_line TEXT,
              vehicle_type TEXT,
              start_ts_ms INTEGER,
              end_ts_ms INTEGER,
              traj BLOB,
              merge_dist_px REAL,
              created_at TEXT
            )
            """
        )

    def _on_preview_merge(self) -> None:
        if not self._merge_cfg:
            self._show_msg("병합 설정", "먼저 병합 설정을 확인하세요.", True)
            return
        self._clear_extrap_preview()
        db_path = Path(self.db_input.text().strip() or "output/tracks.sqlite")
        session_id = None if self.session_combo.currentIndex() <= 0 else self.session_combo.currentText().strip()
        excluded_merge_ids = self._load_saved_merge_track_ids(db_path, session_id)
        group_a = [(gitem, tid, pts) for gitem, tid, pts, group, _cls in self._track_items_meta if group == "A" and str(tid) not in excluded_merge_ids]
        group_b = [(gitem, tid, pts) for gitem, tid, pts, group, _cls in self._track_items_meta if group == "B" and str(tid) not in excluded_merge_ids]
        if not group_a or not group_b:
            self._show_msg("병합 미리보기", f"A={len(group_a)}개 B={len(group_b)}개인 병합 후보가 없습니다.", False)
            return
        dist_thresh = float(self._merge_cfg.get("reconnect_dist", 50.0))
        gap_thresh = float(self._merge_cfg.get("reconnect_gap", 3.0))
        db_path_str = str(db_path)
        pairs = self._find_merge_pairs(group_a, group_b, dist_thresh, gap_thresh, db_path_str, session_id=session_id)
        for i, j, _d in pairs:
            pen_a = QPen(QColor(255, 220, 0, 240), max(3.0, self.track_width * 2.5))
            pen_a.setCosmetic(True)
            group_a[i][0].setPen(pen_a)
            pen_b = QPen(QColor(0, 230, 120, 240), max(3.0, self.track_width * 2.5))
            pen_b.setCosmetic(True)
            group_b[j][0].setPen(pen_b)
        self.status_label.setText(f"병합 후보 미리보기: {len(pairs)}개")

    def _on_execute_merge(self) -> None:
        if not self._merge_cfg:
            self._show_msg("병합 설정", "먼저 병합 설정을 확인하세요.", True)
            return
        self._clear_extrap_preview()
        db_path = Path(self.db_input.text().strip() or "output/tracks.sqlite")
        session_id = self.session_combo.currentText().strip()
        excluded_merge_ids = self._load_saved_merge_track_ids(db_path, session_id if self.session_combo.currentIndex() > 0 else None)
        group_a = [(gitem, tid, pts) for gitem, tid, pts, group, _cls in self._track_items_meta if group == "A" and str(tid) not in excluded_merge_ids]
        group_b = [(gitem, tid, pts) for gitem, tid, pts, group, _cls in self._track_items_meta if group == "B" and str(tid) not in excluded_merge_ids]
        if not group_a or not group_b:
            self._show_msg("병합 실패", "병합 가능한 A/B 그룹이 없습니다.", False)
            return
        db_path_str = str(db_path)
        if not session_id or session_id == "(선택)":
            self._show_msg("병합 실패", "병합 실행: 먼저 session_id를 선택하세요.", True)
            return
        pairs = self._find_merge_pairs(
            group_a, group_b,
            float(self._merge_cfg.get("reconnect_dist", 50.0)),
            float(self._merge_cfg.get("reconnect_gap", 3.0)),
            db_path_str,
            session_id=session_id,
        )
        saved_count = 0
        visual_pairs: List[Tuple[str, str]] = []
        for i, j, best_dist in pairs:
            tid_a = str(group_a[i][1])
            tid_b = str(group_b[j][1])
            try:
                save_manual_track_merge(
                    Path(db_path_str),
                    session_id,
                    source_track_id=tid_b,
                    merged_track_id=tid_a,
                    score=999.0,
                    time_gap_sec=float(self._merge_cfg.get("reconnect_gap", 3.0)),
                    end_start_dist=float(best_dist),
                    direction_score=1.0,
                    class_match=0,
                )
                saved_count += 1
                visual_pairs.append((tid_a, tid_b))
            except Exception as exc:
                self._show_msg("DB 오류", str(exc), True)
                return
        if visual_pairs:
            self._apply_visual_merges(visual_pairs)
        self._apply_saved_merge_map(Path(db_path_str), session_id)
        self.status_label.setText(f"병합 저장: {saved_count}개")

    def _build_track_path(self, pts: List[Tuple[float, float]]) -> QPainterPath:
        path = QPainterPath()
        if not pts:
            return path
        sx = float(self._scale_sx or 1.0)
        sy = float(self._scale_sy or 1.0)
        x0, y0 = pts[0]
        path.moveTo(float(x0) * sx, float(y0) * sy)
        for x, y in pts[1:]:
            path.lineTo(float(x) * sx, float(y) * sy)
        return path

    def _combine_track_points(
        self,
        parent_pts: List[Tuple[float, float]],
        child_pts: List[Tuple[float, float]],
    ) -> List[Tuple[float, float]]:
        merged = list(parent_pts or [])
        if not merged:
            return list(child_pts or [])
        if not child_pts:
            return merged
        if merged[-1] != child_pts[0]:
            merged.append(child_pts[0])
        merged.extend(list(child_pts))
        return merged

    def _apply_visual_merges(self, pairs: List[Tuple[str, str]]) -> None:
        if not pairs:
            return
        meta_by_tid: Dict[str, Tuple[QGraphicsPathItem, str, List[Tuple[float, float]], str, str]] = {
            str(tid): meta for meta in self._track_items_meta for tid in [meta[1]]
        }
        removed_item_ids: set[int] = set()
        for parent_tid, child_tid in pairs:
            parent_meta = meta_by_tid.get(str(parent_tid))
            child_meta = meta_by_tid.get(str(child_tid))
            if parent_meta is None or child_meta is None:
                continue
            parent_item, _ptid, parent_pts, parent_group, parent_cls = parent_meta
            child_item, _ctid, child_pts, _child_group, _child_cls = child_meta
            combined_pts = self._combine_track_points(parent_pts, child_pts)
            parent_item.setPath(self._build_track_path(combined_pts))
            parent_item.setZValue(1.1)
            meta_by_tid[str(parent_tid)] = (parent_item, str(parent_tid), combined_pts, parent_group, parent_cls)
            try:
                self.scene.removeItem(child_item)
            except Exception:
                logger.debug("Suppressed error", exc_info=True)
            removed_item_ids.add(id(child_item))
            meta_by_tid.pop(str(child_tid), None)
        self._track_items_meta = list(meta_by_tid.values())
        self._track_items = [it for it in self._track_items if id(it) not in removed_item_ids]

    def _apply_saved_merge_map(self, db_path: Path, session_id: Optional[str]) -> int:
        if not session_id:
            self._merged_source_to_parent = {}
            self._merged_parent_track_ids = set()
            self._refresh_merged_pairs_list()
            return 0
        try:
            merge_map = load_effective_track_merge_map(db_path, session_id)
        except Exception:
            self._merged_source_to_parent = {}
            self._merged_parent_track_ids = set()
            self._refresh_merged_pairs_list()
            return 0
        if not merge_map:
            self._merged_source_to_parent = {}
            self._merged_parent_track_ids = set()
            self._refresh_merged_pairs_list()
            return 0
        self._merged_source_to_parent = {str(src): str(dst) for src, dst in merge_map.items() if src and dst}
        self._merged_parent_track_ids = {str(dst) for dst in self._merged_source_to_parent.values()}
        self._refresh_merged_pairs_list()
        applied = 0
        pending = [(str(parent), str(child)) for child, parent in merge_map.items() if child and parent]
        for _ in range(max(1, len(pending) + 1)):
            if not pending:
                break
            next_pending: List[Tuple[str, str]] = []
            progress = False
            current_ids = {str(meta[1]) for meta in self._track_items_meta}
            for parent_tid, child_tid in pending:
                if parent_tid in current_ids and child_tid in current_ids:
                    self._apply_visual_merges([(parent_tid, child_tid)])
                    applied += 1
                    progress = True
                else:
                    next_pending.append((parent_tid, child_tid))
            pending = next_pending
            if not progress:
                break
        self._apply_merged_highlight_styles()
        return applied

    def _refresh_merged_pairs_list(self) -> None:
        return

    def _apply_merged_highlight_styles(self) -> None:
        merged_parents = set(getattr(self, "_merged_parent_track_ids", set()) or set())
        if not self._track_items_meta:
            return
        for gitem, tid, _pts, group, cls_name in list(self._track_items_meta):
            try:
                if str(tid) in merged_parents:
                    pen = QPen(QColor("#00ff88"))
                    pen.setWidthF(max(self.track_width + 1.8, 3.0))
                    pen.setStyle(Qt.PenStyle.DashDotLine)
                    gitem.setPen(pen)
                    gitem.setZValue(1.2)
                elif group in ("A", "B"):
                    gitem.setPen(self._make_group_pen(group))
                else:
                    pen = self._class_pen(cls_name)
                    if pen is not None:
                        gitem.setPen(pen)
            except Exception:
                continue
        selected = set(str(x) for x in list(self._manual_selected_track_ids or []))
        for gitem, tid, _pts, _group, _cls_name in list(self._track_items_meta):
            if str(tid) not in selected:
                continue
            try:
                pen = QPen(QColor("#ff4dd2"))
                pen.setWidthF(max(self.track_width + 2.2, 3.4))
                gitem.setPen(pen)
                gitem.setZValue(1.35)
            except Exception:
                continue

    def _load_track_raw_points(self, db_path: Path, session_id: str, tid: str) -> Tuple[str, List[List[float]]]:
        return self._load_merged_raw_points(db_path, session_id, str(tid))

    def _track_time_bounds(self, raw_pts: List[List[float]]) -> Tuple[Optional[float], Optional[float]]:
        if not raw_pts:
            return None, None
        try:
            return float(raw_pts[0][1]) / 1000.0, float(raw_pts[-1][1]) / 1000.0
        except Exception:
            logger.debug("Suppressed error", exc_info=True)
            return None, None

    def _endpoint_distance(self, raw_a: List[List[float]], raw_b: List[List[float]]) -> float:
        try:
            ax, ay = float(raw_a[-1][2]), float(raw_a[-1][3])
            bx, by = float(raw_b[0][2]), float(raw_b[0][3])
            return float(math.hypot(ax - bx, ay - by))
        except Exception:
            logger.debug("Suppressed error", exc_info=True)
            return 0.0

    def _on_execute_manual_merge(self) -> None:
        if self.session_combo.currentIndex() <= 0:
            self._show_msg("강제 병합", "먼저 session_id를 선택해야 합니다.", True)
            return
        selected = list(self._manual_selected_track_ids or [])
        if len(selected) != 2:
            self._show_msg("강제 병합", "수동 선택 모드에서 궤적 2개를 선택하세요.", True)
            return
        db_path = Path(self.db_input.text().strip() or "output/tracks.sqlite")
        session_id = self.session_combo.currentText().strip()
        tid1, tid2 = str(selected[0]), str(selected[1])
        _cam1, raw1 = self._load_track_raw_points(db_path, session_id, tid1)
        _cam2, raw2 = self._load_track_raw_points(db_path, session_id, tid2)
        if len(raw1) < 2 or len(raw2) < 2:
            self._show_msg("강제 병합", "선택 궤적 raw points를 불러올 수 없습니다.", True)
            return
        s1, e1 = self._track_time_bounds(raw1)
        s2, e2 = self._track_time_bounds(raw2)
        parent_tid, child_tid = tid1, tid2
        parent_raw, child_raw = raw1, raw2
        if e2 is not None and s1 is not None and e2 <= s1:
            parent_tid, child_tid = tid2, tid1
            parent_raw, child_raw = raw2, raw1
        gap_sec = max(0.0, float((self._track_time_bounds(child_raw)[0] or 0.0) - (self._track_time_bounds(parent_raw)[1] or 0.0)))
        dist_px = self._endpoint_distance(parent_raw, child_raw)
        save_manual_track_merge(
            db_path,
            session_id,
            source_track_id=str(child_tid),
            merged_track_id=str(parent_tid),
            score=999.0,
            time_gap_sec=float(gap_sec),
            end_start_dist=float(dist_px),
            direction_score=1.0,
            class_match=0,
        )
        self._apply_saved_merge_map(db_path, session_id)
        self._clear_manual_selection()
        self._reload_all()
        self.status_label.setText(f"강제 병합 완료: {child_tid} -> {parent_tid}")

    def _save_manual_virtual_event(
        self,
        db_path: Path,
        session_id: str,
        track_id: str,
        event: Dict[str, object],
        method: str,
    ) -> None:
        init_db(db_path)
        now = datetime.now().isoformat(timespec="seconds")
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "delete from track_virtual_events where session_id = ? and track_id = ? and method = ?",
                (str(session_id), str(track_id), str(method)),
            )
            conn.execute(
                """
                insert or replace into track_virtual_events
                (session_id, camera_id, track_id, ts_ms, line_id, bound, inout, method, horizon, src_point_x, src_point_y, hit_x, hit_y, created_at, extra)
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(session_id),
                    str(event.get("camera_id") or ""),
                    str(track_id),
                    int(event.get("ts_ms") or 0),
                    str(event.get("line_id") or ""),
                    str(event.get("bound") or ""),
                    str(event.get("inout") or "unk"),
                    str(method),
                    float(event.get("horizon") or 0.0),
                    float(event.get("src_point_x") or 0.0),
                    float(event.get("src_point_y") or 0.0),
                    float(event.get("hit_x") or 0.0),
                    float(event.get("hit_y") or 0.0),
                    now,
                    json.dumps({"source": "trajectory_viewer2_manual"}, ensure_ascii=False),
                ),
            )
            conn.commit()

    def _on_execute_manual_extrap(self, backward: bool = False) -> None:
        if self.session_combo.currentIndex() <= 0:
            self._show_msg("강제 외삽", "먼저 session_id를 선택해야 합니다.", True)
            return
        selected = list(self._manual_selected_track_ids or [])
        if len(selected) != 1:
            self._show_msg("강제 외삽", "수동 선택 모드에서 궤적 1개를 선택하세요.", True)
            return
        src_line, tgt_line = self._get_extrap_direction()
        target_line = src_line if backward else tgt_line
        if target_line is None:
            self._show_msg("강제 외삽", "외삽 설정이 먼저 완료되어야 합니다.", True)
            return
        db_path = Path(self.db_input.text().strip() or "output/tracks.sqlite")
        session_id = self.session_combo.currentText().strip()
        tid = str(selected[0])
        camera_id, raw_pts = self._load_track_raw_points(db_path, session_id, tid)
        if len(raw_pts) < 2:
            self._show_msg("강제 외삽", "선택 궤적 raw points를 불러올 수 없습니다.", True)
            return
        horizon = float(self._extrap_cfg.get("extrap_horizon", 200.0) or 0.0)
        event = self._estimate_extrap_event_from_raw(
            camera_id,
            raw_pts,
            tid,
            target_line,
            horizon,
            backward=bool(backward),
        )
        if event is None:
            self._show_msg("외삽 실패", "현재 선택된 궤적의 방향을 계산할 수 없어 외삽을 수행할 수 없습니다.", True)
            return
        method = "viewer2_manual_extrap_backward" if backward else "viewer2_manual_extrap_forward"
        self._save_manual_virtual_event(db_path, session_id, tid, event, method)
        self._clear_manual_selection()
        self.status_label.setText(f"강제 외삽 완료: {tid} -> {event.get('line_id')}")

    def _on_cancel_merge(self) -> None:
        if self.session_combo.currentIndex() <= 0:
            self._show_msg("병합 취소", "먼저 session_id를 선택해야 합니다.", True)
            return
        db_path = Path(self.db_input.text().strip() or "output/tracks.sqlite")
        session_id = self.session_combo.currentText().strip()
        if not db_path.exists():
            self._show_msg("蹂묓빀 痍⑥냼", f"DB 파일을 찾을 수 없습니다.\n{db_path}", True)
            return
        reply = QMessageBox.question(
            self,
            "蹂묓빀 痍⑥냼",
            f"현재 session에 저장된 병합 결과를 모두 삭제하시겠습니까?\n\nsession_id: {session_id}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        removed = 0
        with sqlite3.connect(db_path) as conn:
            for table in (
                "track_merge_map_auto",
                "track_merge_map_manual",
                "track_merge_exclude_manual",
                "track_merge_map",
                "merged_tracks",
            ):
                row = conn.execute(
                    "select 1 from sqlite_master where type='table' and name=? limit 1",
                    (table,),
                ).fetchone()
                if not row:
                    continue
                cur = conn.execute(
                    f"delete from {table} where session_id = ?",
                    (str(session_id),),
                )
                removed += int(cur.rowcount or 0)
            row = conn.execute(
                "select 1 from sqlite_master where type='table' and name='track_merge_runs' limit 1"
            ).fetchone()
            if row:
                cur = conn.execute(
                    "delete from track_merge_runs where session_id = ?",
                    (str(session_id),),
                )
                removed += int(cur.rowcount or 0)
            conn.commit()
        self._apply_merge_filter = None
        self._merge_cfg = {}
        self._clear_extrap_preview()
        self.btn_preview_merge.setEnabled(False)
        self.btn_cancel_merge.setEnabled(False)
        self._merged_source_to_parent = {}
        self._merged_parent_track_ids = set()
        self._refresh_merged_pairs_list()
        self._persist_options()
        self._reload_all()
        self.status_label.setText(f"병합 취소: {removed}개 삭제")

    def _on_session_changed(self, _txt: str) -> None:
        if self._restoring_options:
            return
        self._persist_options()
        self._set_reload_pending(True, "session")

    def _on_slot_changed(self, _txt: str) -> None:
        if self._restoring_options:
            return
        self._persist_options()
        self._set_reload_pending(True, "slot")

    def _on_perf_changed(self, _val: int) -> None:
        if self._restoring_options:
            return
        self._persist_options()
        self._set_reload_pending(True, "화면 설정")

    def _pick_db(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "DB ?좏깮", str(Path(self.db_input.text() or "output").resolve()), "SQLite (*.sqlite *.db);;All Files (*)")
        if path:
            self.db_input.setText(path)
            self._refresh_sessions(select=self.session_combo.currentText() or None)
            self._persist_options()
            self._set_reload_pending(True, "DB")

    def _open_db_viewer(self) -> None:
        db_path = _resolve_existing_path(self.db_input.text().strip()) or Path(self.db_input.text().strip() or "output/tracks.sqlite")
        if not db_path.exists():
            self._show_msg("DB 없음", f"DB 파일을 찾을 수 없습니다.\n{db_path}", True)
            return
        session_id = self.session_combo.currentText().strip()
        if not session_id or session_id == "(선택)":
            session_id = None
        dlg = DbSessionViewerDialog(db_path=db_path, session_id=session_id, parent=self)
        dlg.show()
        dlg.raise_()
        dlg.exec()

    def _open_postprocess_viewer(self) -> None:
        db_path = _resolve_existing_path(self.db_input.text().strip()) or Path(self.db_input.text().strip() or "output/tracks.sqlite")
        if not db_path.exists():
            self._show_msg("DB 없음", f"DB 파일을 찾을 수 없습니다.\n{db_path}", True)
            return
        session_id = self.session_combo.currentText().strip()
        if not session_id or session_id == "(선택)":
            session_id = None
        dlg = DbSessionViewerDialog(
            db_path=db_path,
            session_id=session_id,
            parent=self,
            table_candidates=(
                "track_merge_runs",
                "track_merge_map",
                "track_merge_map_auto",
                "track_merge_map_manual",
                "track_merge_exclude_manual",
                "track_virtual_events",
                "track_line_events",
                "track_line_summary",
                "merged_tracks",
            ),
            title="후처리 DB 보기",
        )
        dlg.show()
        dlg.raise_()
        dlg.exec()

    def _pick_video(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "영상 선택", str(Path(self.video_input.text() or ".").resolve()), "Video Files (*.mp4 *.avi *.mov *.mkv);;All Files (*)")
        if path:
            resolved = _resolve_existing_path(path) or Path(path)
            self.video_input.setText(str(resolved))
            self._ensure_base_matches_video(resolved)
            self._bg_enabled = True
            self.bg_toggle_btn.setText("배경 보임")
            self._rebuild_scene_from_cache()
            self._persist_options()

    def _pick_lines(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Lines JSON 선택", str(Path(self.lines_input.text() or "config").resolve()), "JSON Files (*.json);;All Files (*)")
        if path:
            self.lines_input.setText(path)
            self._load_lines_from_path()
            self._rerender_lines_overlay_only()
            self._persist_options()

    def _on_width_changed(self, val: float) -> None:
        self.track_width = float(val)
        for it in list(self._track_items):
            try:
                pen = it.pen()
                pen.setWidthF(self.track_width)
                it.setPen(pen)
            except Exception:
                continue
        self._persist_options()

    def _reload_all(self) -> None:
        db_path = Path(self.db_input.text().strip() or "output/tracks.sqlite")
        if not db_path.exists():
            self.status_label.setText(f"DB 없음: {db_path}")
            self.scene.clear()
            return
        self._set_reload_pending(False)
        self._load_lines_from_path()
        self._render_scene()

    def _reset_filter_mode(self) -> None:
        self._apply_merge_filter = None
        self._reload_all()

    def _clear_pending_points(self) -> None:
        self._pending_line_points = []
        self._setting_in_point_index = None
        for it in list(self._pending_items):
            try:
                self.scene.removeItem(it)
            except Exception:
                logger.debug("Suppressed error", exc_info=True)
        self._pending_items = []

    def _on_view_click(self, x: float, y: float) -> None:
        bx = float(x) / float(self._scale_sx or 1.0)
        by = float(y) / float(self._scale_sy or 1.0)
        if self._setting_in_point_index is not None:
            row = int(self._setting_in_point_index)
            self._setting_in_point_index = None
            if 0 <= row < len(self._lines):
                self._lines[row].in_point = [float(bx), float(by)]
                self._refresh_lines_list()
                self._rerender_lines_overlay_only()
                self.status_label.setText(f"in점 지정: {self._lines[row].line_id}")
            return
        if self._merge_debug_mode:
            meta = self._pick_track_meta_at(x, y)
            if meta is None:
                self.status_label.setText("병합 디버그: 궤적을 선택하세요")
                return
            _gitem, tid, pts, group, cls_name = meta
            self._show_msg("병합 디버그", self._build_merge_debug_text(str(tid), pts, str(group or ""), str(cls_name or "")))
            return
        if self._manual_pick_mode:
            meta = self._pick_track_meta_at(x, y)
            if meta is None:
                self.status_label.setText("수동 선택: 궤적 없음")
                return
            self._toggle_manual_track_selection(str(meta[1]))
            return
        self._pending_line_points.append((bx, by))
        sx = bx * float(self._scale_sx or 1.0)
        sy = by * float(self._scale_sy or 1.0)
        dot_pen = QPen(QColor("#ffd166"))
        dot_pen.setWidthF(2.0)
        dot = QGraphicsEllipseItem(sx - 4, sy - 4, 8, 8)
        dot.setPen(dot_pen)
        self.scene.addItem(dot)
        self._pending_items.append(dot)
        if len(self._pending_line_points) >= 2:
            x1, y1 = self._pending_line_points[-2]
            x2, y2 = self._pending_line_points[-1]
            dash_pen = QPen(QColor("#ffd166"))
            dash_pen.setWidthF(2.5)
            dash_pen.setStyle(Qt.PenStyle.DashLine)
            tmp_line = QGraphicsLineItem(
                x1 * float(self._scale_sx or 1.0), y1 * float(self._scale_sy or 1.0),
                x2 * float(self._scale_sx or 1.0), y2 * float(self._scale_sy or 1.0),
            )
            tmp_line.setPen(dash_pen)
            self.scene.addItem(tmp_line)
            self._pending_items.append(tmp_line)
            self.status_label.setText(f"라인 점 추가 중: {len(self._pending_line_points)}개")

    def _add_line_from_pending(self) -> None:
        if len(self._pending_line_points) < 2:
            self._show_msg("라인점 부족", "최소 2개의 포인트를 선택해야 합니다.", True)
            return
        line_id = self.line_id_input.text().strip() or f"line_{len(self._lines) + 1}"
        bound = self.bound_input.currentText().strip()
        self._lines.append(LineDef(line_id=line_id, points=[[float(x), float(y)] for x, y in self._pending_line_points], bound=bound, in_point=None))
        self._clear_pending_points()
        self._refresh_lines_list()
        self._rerender_lines_overlay_only()

    def _refresh_lines_list(self) -> None:
        self.lines_list.clear()
        for ln in self._lines:
            text = f"{ln.line_id}"
            if ln.bound:
                text += f" ({ln.bound})"
            if ln.in_point and len(ln.in_point) >= 2:
                text += " [in]"
            self.lines_list.addItem(QListWidgetItem(text))

    def _on_line_selected(self, row: int) -> None:
        if 0 <= row < len(self._lines):
            line = self._lines[row]
            self.line_id_input.setText(str(line.line_id or ""))
            self.bound_input.setCurrentText(str(line.bound or ""))

    def _begin_set_in_point(self) -> None:
        row = self.lines_list.currentRow()
        if row < 0 or row >= len(self._lines):
            self._show_msg("in점 지정", "라인 목록에서 먼저 라인을 선택하세요.", True)
            return
        self._clear_pending_points()
        self._setting_in_point_index = int(row)
        self.status_label.setText(f"in점 지정 중: {self._lines[row].line_id} - in 방향을 클릭하세요")

    def _clear_selected_in_point(self) -> None:
        row = self.lines_list.currentRow()
        if row < 0 or row >= len(self._lines):
            self._show_msg("in점 초기화", "라인 목록에서 먼저 라인을 선택하세요.", True)
            return
        self._lines[row].in_point = None
        self._setting_in_point_index = None
        self._refresh_lines_list()
        self._rerender_lines_overlay_only()
        self.status_label.setText(f"in점 초기화: {self._lines[row].line_id}")

    def _delete_selected_line(self) -> None:
        row = self.lines_list.currentRow()
        if 0 <= row < len(self._lines):
            self._lines.pop(row)
            self._refresh_lines_list()
            self._rerender_lines_overlay_only()

    def _reset_lines(self) -> None:
        self._lines_raw = []
        self._lines = self._lines_raw
        self._lines_base_size_raw = self._get_current_image_size_int()
        self._lines_base_size = self._lines_base_size_raw
        self._clear_pending_points()
        self._refresh_lines_list()
        self._rerender_lines_overlay_only()

    def _is_thread_alive(self, th: Optional[QThread]) -> bool:
        if th is None:
            return False
        try:
            if not isValid(th):
                return False
            return bool(th.isRunning())
        except RuntimeError:
            return False
        except Exception:
            logger.debug("Suppressed error", exc_info=True)
            return False

    def _clear_finished_loader(self) -> None:
        th = self._load_thread
        if not self._is_thread_alive(th):
            self._load_thread = None
            self._load_worker = None

    def _cancel_loader(self) -> bool:
        th = self._load_thread
        if th is None:
            return True
        try:
            if self._is_thread_alive(th):
                th.requestInterruption()
                th.quit()
                if not th.wait(5000):
                    try:
                        th.terminate()
                    except Exception:
                        logger.debug("Suppressed error", exc_info=True)
                    th.wait(1500)
        except Exception:
            logger.debug("Suppressed error", exc_info=True)
        if self._is_thread_alive(th):
            return False
        if self._load_thread is th:
            self._load_thread = None
            self._load_worker = None
        return True

    def _sender_token(self) -> Optional[int]:
        try:
            snd = self.sender()
            return int(getattr(snd, "token", 0)) if snd is not None else None
        except Exception:
            logger.debug("Suppressed error", exc_info=True)
            return None

    @Slot(object)
    def _on_tracks_batch_queued(self, batch: object) -> None:
        token = self._sender_token()
        if token is not None:
            self._on_tracks_batch(batch, token)

    @Slot(str)
    def _on_worker_progress(self, msg: str) -> None:
        token = self._sender_token()
        if token == self._loading_token:
            self.status_label.setText(str(msg))

    @Slot(str)
    def _on_worker_error(self, msg: str) -> None:
        token = self._sender_token()
        if token == self._loading_token:
            self.status_label.setText(f"[ERROR] {msg}")

    @Slot(object)
    def _on_tracks_finished_queued(self, meta: object) -> None:
        token = self._sender_token()
        if token is not None:
            self._on_tracks_finished(meta, token)

    def _make_track_pens(self) -> List[QPen]:
        colors = ["#3e7dd1", "#d35f5f", "#4cbfa6", "#c4a000", "#ad7fa8", "#729fcf", "#e67e22", "#27ae60"]
        pens = [QPen(QColor(c)) for c in colors]
        for pen in pens:
            pen.setWidthF(self.track_width)
        return pens

    def _class_pen(self, cls_name: str) -> Optional[QPen]:
        color = self._class_color_map.get(str(cls_name or "").strip().lower())
        if not color:
            return None
        pen = QPen(QColor(color))
        pen.setWidthF(self.track_width)
        return pen

    def _make_group_pen(self, group: str) -> QPen:
        if group == "A":
            pen = QPen(QColor(60, 170, 255, 220), max(0.5, self.track_width))
        elif group == "B":
            pen = QPen(QColor(255, 120, 60, 220), max(0.5, self.track_width))
        else:
            pen = self._make_track_pens()[self._color_idx % len(self._make_track_pens())]
        pen.setCosmetic(True)
        return pen

    def _render_lines_overlay_simple(self, scene_w: float, scene_h: float) -> None:
        line_pen = QPen(QColor("#ffb347"))
        line_pen.setWidthF(3.0)
        in_pen = QPen(QColor("#7CFC8A"))
        in_pen.setWidthF(2.0)
        base = self._lines_base_size
        if base and base[0] > 0 and base[1] > 0:
            sx = float(scene_w) / float(base[0])
            sy = float(scene_h) / float(base[1])
        else:
            sx = sy = 1.0
        for ln in self._lines:
            pts = ln.points
            if len(pts) < 2:
                continue
            path = QPainterPath()
            path.moveTo(QPointF(float(pts[0][0]) * sx, float(pts[0][1]) * sy))
            for p in pts[1:]:
                path.lineTo(QPointF(float(p[0]) * sx, float(p[1]) * sy))
            item = QGraphicsPathItem(path)
            item.setPen(line_pen)
            item.setZValue(2)
            self.scene.addItem(item)
            self._line_items.append(item)
            label = QGraphicsTextItem(str(ln.line_id or ""))
            label.setDefaultTextColor(QColor("#ffe066"))
            label.setZValue(3)
            p1 = QPointF(float(pts[0][0]) * sx, float(pts[0][1]) * sy)
            p2 = QPointF(float(pts[1][0]) * sx, float(pts[1][1]) * sy)
            rect = label.boundingRect()
            label.setPos((p1.x() + p2.x()) / 2.0 - rect.width() / 2.0, (p1.y() + p2.y()) / 2.0 - rect.height() - 4.0)
            self.scene.addItem(label)
            self._line_label_items.append(label)
            if ln.in_point and len(ln.in_point) >= 2:
                ix = float(ln.in_point[0]) * sx
                iy = float(ln.in_point[1]) * sy
                dot = QGraphicsEllipseItem(ix - 5.0, iy - 5.0, 10.0, 10.0)
                dot.setPen(in_pen)
                dot.setBrush(QColor("#7CFC8A"))
                dot.setZValue(3)
                self.scene.addItem(dot)
                self._line_items.append(dot)
                in_label = QGraphicsTextItem("IN")
                in_label.setDefaultTextColor(QColor("#7CFC8A"))
                in_label.setZValue(3)
                in_label.setPos(ix + 6.0, iy - 14.0)
                self.scene.addItem(in_label)
                self._line_label_items.append(in_label)

    def _on_tracks_batch(self, batch: object, token: int) -> None:
        if token != self._loading_token or not isinstance(batch, list):
            return
        pens = self._make_track_pens()
        excluded_ids = set(getattr(self, "_render_excluded_track_ids", set()) or set())
        for item in batch:
            try:
                tid, pts, group, cls_name = item
            except Exception:
                continue
            if str(tid) in excluded_ids:
                continue
            if not isinstance(pts, list) or len(pts) < 2:
                continue
            if len(pts) < 2:
                continue
            pen = self._make_group_pen(group) if group in ("A", "B") else (self._class_pen(cls_name) or pens[self._color_idx % len(pens)])
            self._color_idx += 1
            path = QPainterPath()
            x0, y0 = pts[0]
            path.moveTo(float(x0) * float(self._scale_sx or 1.0), float(y0) * float(self._scale_sy or 1.0))
            for x, y in pts[1:]:
                path.lineTo(float(x) * float(self._scale_sx or 1.0), float(y) * float(self._scale_sy or 1.0))
            gitem = QGraphicsPathItem(path)
            gitem.setPen(pen)
            gitem.setZValue(1)
            gitem.setCacheMode(QGraphicsPathItem.CacheMode.DeviceCoordinateCache)
            self.scene.addItem(gitem)
            self._track_items.append(gitem)
            self._track_items_meta.append((gitem, str(tid), pts, str(group or ""), str(cls_name or "")))

    def _on_tracks_finished(self, meta: object, token: int) -> None:
        if token != self._loading_token:
            return
        track_cnt = int(meta.get("track_count") or 0) if isinstance(meta, dict) else 0
        db_path = Path(self.db_input.text().strip() or "output/tracks.sqlite")
        session_raw = self.session_combo.currentText().strip()
        session_id = None if (not session_raw or session_raw == "(선택)") else session_raw
        merged_applied = self._apply_saved_merge_map(db_path, session_id)
        self.status_label.setText(
            f"DB={db_path.name} session_id={session_id or '(선택)'}  tracks={track_cnt}  "
            f"merged_view={merged_applied}  lines={len(self._lines)}  (frame_step={self.frame_step_spin.value()})"
        )

    def _render_scene_async(self) -> None:
        db_path = Path(self.db_input.text().strip() or "output/tracks.sqlite")
        video_path = _resolve_existing_path(self.video_input.text().strip()) if self.video_input.text().strip() else None
        try:
            self._ensure_base_matches_video(video_path)
        except Exception:
            logger.debug("Suppressed error", exc_info=True)
        if not db_path.exists():
            self.status_label.setText(f"DB 없음: {db_path}")
            self.scene.clear()
            return
        self._cancel_loader()
        bg_item, bg_w, bg_h = _load_background(video_path, self.resize_target) if self._bg_enabled else (None, 0.0, 0.0)
        self._bg_item = bg_item
        self._bg_size = (bg_w, bg_h)
        self.scene.clear()
        self._extrap_preview_items = []
        self._line_items = []
        self._line_label_items = []
        self._track_items = []
        self._track_items_meta = []
        if bg_item is not None:
            bg_item.setZValue(0)
            self.scene.addItem(bg_item)
        elif self._bg_enabled and video_path is not None:
            self.status_label.setText(f"배경 이미지 로드 실패: {video_path}")
        base = self._lines_base_size
        if not (base and base[0] > 0 and base[1] > 0):
            probed = _probe_video_size(video_path)
            if probed:
                base = probed
                self._lines_base_size = probed
        if self.resize_target and self.resize_target[0] > 0 and self.resize_target[1] > 0:
            scene_w, scene_h = float(self.resize_target[0]), float(self.resize_target[1])
        elif bg_w > 0 and bg_h > 0:
            scene_w, scene_h = bg_w, bg_h
        elif base and base[0] > 0 and base[1] > 0:
            scene_w, scene_h = float(base[0]), float(base[1])
        else:
            scene_w, scene_h = 900.0, 650.0
        self.scene.setSceneRect(0, 0, scene_w, scene_h)
        self._scale_sx = float(scene_w) / float(base[0]) if base and base[0] > 0 else 1.0
        self._scale_sy = float(scene_h) / float(base[1]) if base and base[1] > 0 else 1.0
        self._render_lines_overlay_simple(scene_w, scene_h)
        self.view.set_fit_rect(self.scene.sceneRect())
        self._loading_token += 1
        token = self._loading_token
        self._color_idx = 0
        session_id2 = None if self.session_combo.currentIndex() <= 0 else self.session_combo.currentText().strip()
        if self._apply_merge_filter and session_id2:
            self._render_excluded_track_ids = (
                self._load_saved_merge_track_ids(db_path, session_id2)
                | self._load_saved_virtual_track_ids(db_path, session_id2)
            )
        else:
            self._render_excluded_track_ids = set()
        slot_index2 = self.slot_combo.currentData()
        try:
            slot_index2 = int(slot_index2) if slot_index2 is not None else None
        except Exception:
            slot_index2 = None
        worker = _TrajectoryLoadWorker(
            db_path=db_path,
            session_id=session_id2,
            slot_index=slot_index2,
            frame_step=int(self.frame_step_spin.value()),
            max_tracks=int(self.max_tracks_spin.value()),
            token=token,
            filter_mode=self._apply_merge_filter,
            batch_size_tracks=50,
        )
        thread = QThread()
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.batch_ready.connect(self._on_tracks_batch_queued, Qt.ConnectionType.QueuedConnection)
        worker.progress.connect(self._on_worker_progress, Qt.ConnectionType.QueuedConnection)
        worker.error.connect(self._on_worker_error, Qt.ConnectionType.QueuedConnection)
        worker.finished.connect(self._on_tracks_finished_queued, Qt.ConnectionType.QueuedConnection)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._clear_finished_loader, Qt.ConnectionType.QueuedConnection)
        self._load_thread = thread
        self._load_worker = worker
        self.status_label.setText("로딩 중... (데이터가 많으면 시간이 더 걸릴 수 있습니다)")
        thread.start()

    def _render_scene(self) -> None:
        self._render_scene_async()

    def _toggle_background(self) -> None:
        self._bg_enabled = not self._bg_enabled
        self.bg_toggle_btn.setText("배경 보임" if self._bg_enabled else "배경 숨김")
        self._rebuild_scene_from_cache()
        self._persist_options()



