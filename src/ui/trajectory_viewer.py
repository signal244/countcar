import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Dict, List, Optional, Tuple

import cv2
from PySide6.QtCore import QObject, QPointF, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QColor, QImage, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
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

from src.config.loader import load_app_config, save_app_config
from src.db.schema import init_db
from src.db.writer import decode_traj


@dataclass
class LineDef:
    line_id: str
    points: List[List[float]]  # [[x1,y1],[x2,y2]]
    bound: str = ""

    def to_json(self) -> Dict:
        return {"id": self.line_id, "points": self.points, "bound": self.bound}


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
        if self._click_cb is None:
            super().mousePressEvent(event)
            return
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        if self.dragMode() == QGraphicsView.DragMode.ScrollHandDrag:
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
        steps = delta / 120.0
        factor = 1.15 ** steps
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
                        sessions.add(str(r[0]))
            except Exception:
                continue
    return sorted(sessions)


def _load_trajectories(
    db_path: Path,
    session_id: Optional[str],
    frame_step: int = 1,
    max_tracks: int = 0,
) -> Tuple[Dict[str, List[Tuple[float, float]]], Tuple[float, float, float, float]]:
    tracks: Dict[str, List[Tuple[float, float]]] = {}
    minx = miny = float("inf")
    maxx = maxy = float("-inf")

    query = """
        select track_id, center_x, center_y, frame_id
        from tracks
        where center_x is not null and center_y is not null
    """
    params: List[object] = []
    if session_id:
        query += " and session_id = ?"
        params.append(session_id)
    if frame_step and int(frame_step) > 1:
        query += " and (frame_id % ?) = 0"
        params.append(int(frame_step))
    query += " order by track_id, frame_id"

    with sqlite3.connect(db_path) as conn:
        current_track = None
        track_count = 0
        for tid, x, y, _fid in conn.execute(query, tuple(params)):
            tid = str(tid)
            if current_track != tid:
                current_track = tid
                track_count += 1
                if max_tracks and track_count > int(max_tracks):
                    break
            try:
                tx = float(x)
                ty = float(y)
            except (TypeError, ValueError):
                continue
            tracks.setdefault(tid, []).append((tx, ty))
            minx = min(minx, tx)
            maxx = max(maxx, tx)
            miny = min(miny, ty)
            maxy = max(maxy, ty)

    if minx == float("inf"):
        minx = miny = 0.0
        maxx = maxy = 1.0
    return tracks, (minx, maxx, miny, maxy)


class _TrajectoryLoadWorker(QObject):
    batch_ready = Signal(object)  # List[Tuple[str, List[Tuple[float, float]]]]
    finished = Signal(object)  # Dict meta
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
        batch_size_tracks: int = 50,
    ) -> None:
        super().__init__()
        self.db_path = db_path
        self.session_id = session_id
        self.slot_index = slot_index
        self.frame_step = max(1, int(frame_step))
        self.max_tracks = max(0, int(max_tracks))
        self.token = int(token)
        self.batch_size_tracks = max(10, int(batch_size_tracks))
        self._slot_interval_ms = 900_000  # 15 minutes

    def _interrupted(self) -> bool:
        try:
            return bool(QThread.currentThread().isInterruptionRequested())
        except Exception:
            return False

    @Slot()
    def run(self) -> None:
        try:
            if not self.db_path.exists():
                self.finished.emit({"token": self.token, "track_count": 0})
                return

            # Ensure indexes exist (runs off the UI thread).
            try:
                init_db(self.db_path)
            except Exception:
                pass

            # track_trajs(트랙 단위/압축 궤적) 테이블이 있으면 우선 사용 (대용량 DB에서 훨씬 빠름)
            try:
                with sqlite3.connect(self.db_path) as conn:
                    has_track_trajs = (
                        conn.execute(
                            "select 1 from sqlite_master where type='table' and name='track_trajs' limit 1"
                        ).fetchone()
                        is not None
                    )
                    if has_track_trajs:
                        query2 = "select track_id, traj from track_trajs where traj is not null"
                        params2: List[object] = []
                        if self.session_id:
                            query2 += " and session_id = ?"
                            params2.append(self.session_id)
                        if self.slot_index is not None:
                            query2 += " and (start_ts_ms / ?) = ?"
                            params2.extend([int(self._slot_interval_ms), int(self.slot_index)])
                        query2 += " order by track_id"

                        tracks_batch2: List[Tuple[str, List[Tuple[float, float]]]] = []
                        track_count2 = 0
                        for tid, blob in conn.execute(query2, tuple(params2)):
                            if self._interrupted():
                                return
                            tid_s = str(tid)
                            pts_raw = decode_traj(blob) if blob is not None else []
                            pts: List[Tuple[float, float]] = []
                            for row in pts_raw:
                                if not isinstance(row, list) or len(row) < 4:
                                    continue
                                try:
                                    fid_i = int(row[0])
                                except Exception:
                                    fid_i = 0
                                if self.frame_step > 1 and (fid_i % self.frame_step) != 0:
                                    continue
                                try:
                                    pts.append((float(row[2]), float(row[3])))
                                except Exception:
                                    continue
                            if len(pts) < 2:
                                continue

                            track_count2 += 1
                            if self.max_tracks and track_count2 > self.max_tracks:
                                break
                            if track_count2 % 200 == 0:
                                self.progress.emit(f"로딩 중.. tracks={track_count2}")
                            tracks_batch2.append((tid_s, pts))
                            if len(tracks_batch2) >= self.batch_size_tracks:
                                self.batch_ready.emit(tracks_batch2)
                                tracks_batch2 = []

                        if tracks_batch2:
                            self.batch_ready.emit(tracks_batch2)
                        self.finished.emit({"token": self.token, "track_count": track_count2})
                        return
            except Exception:
                pass

            query = """
                select track_id, center_x, center_y, frame_id
                from tracks
                where center_x is not null and center_y is not null
            """
            params: List[object] = []
            if self.session_id:
                query += " and session_id = ?"
                params.append(self.session_id)
            if self.slot_index is not None:
                query += " and (timestamp_ms / ?) = ?"
                params.extend([int(self._slot_interval_ms), int(self.slot_index)])
            query += " order by track_id, frame_id"

            tracks_batch: List[Tuple[str, List[Tuple[float, float]]]] = []
            current_track: Optional[str] = None
            current_pts: List[Tuple[float, float]] = []
            track_count = 0

            with sqlite3.connect(self.db_path) as conn:
                cur = conn.execute(query, tuple(params))
                for tid, x, y, fid in cur:
                    if self._interrupted():
                        return
                    try:
                        tid_s = str(tid)
                    except Exception:
                        continue
                    try:
                        fid_i = int(fid)
                    except Exception:
                        fid_i = 0
                    if self.frame_step > 1 and (fid_i % self.frame_step) != 0:
                        continue
                    try:
                        tx = float(x)
                        ty = float(y)
                    except (TypeError, ValueError):
                        continue

                    if current_track != tid_s:
                        if current_track is not None and len(current_pts) >= 2:
                            tracks_batch.append((current_track, current_pts))
                        current_track = tid_s
                        current_pts = []
                        track_count += 1
                        if self.max_tracks and track_count > self.max_tracks:
                            break
                        if track_count % 200 == 0:
                            self.progress.emit(f"로딩 중... tracks={track_count}")
                        if len(tracks_batch) >= self.batch_size_tracks:
                            self.batch_ready.emit(tracks_batch)
                            tracks_batch = []

                    current_pts.append((tx, ty))

            if current_track is not None and len(current_pts) >= 2:
                tracks_batch.append((current_track, current_pts))
            if tracks_batch:
                self.batch_ready.emit(tracks_batch)

            self.finished.emit({"token": self.token, "track_count": track_count})
        except Exception as exc:  # noqa: BLE001
            self.error.emit(str(exc))
            self.finished.emit({"token": self.token, "track_count": 0})


def _load_background(video_path: Optional[Path], resize_target: Optional[Tuple[int, int]]) -> Tuple[Optional[QGraphicsPixmapItem], float, float]:
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
    image = QImage(rgb.data, w, h, rgb.strides[0], QImage.Format.Format_RGB888)
    pix = QPixmap.fromImage(image)
    item = QGraphicsPixmapItem(pix)
    return item, float(w), float(h)


def _probe_video_size(video_path: Optional[Path]) -> Optional[Tuple[int, int]]:
    if not video_path:
        return None
    try:
        if not Path(video_path).exists():
            return None
    except Exception:
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
        return None
    return None


def _rescale_points(
    pts: List[List[float]],
    old_size: Tuple[int, int],
    new_size: Tuple[int, int],
) -> List[List[float]]:
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
        base = None
        if iw and ih:
            base = (int(iw), int(ih))
        out: List[LineDef] = []
        for ln in data.get("lines", []):
            pts = ln.get("points") or []
            if len(pts) < 2:
                continue
            out.append(
                LineDef(
                    line_id=str(ln.get("name") or ln.get("id") or "line"),
                    points=[[float(pts[0][0]), float(pts[0][1])], [float(pts[1][0]), float(pts[1][1])]],
                    bound=str(ln.get("bound") or ""),
                )
            )
        return out, base
    except Exception:
        return [], None


def _write_lines(lines_path: Path, lines: List[LineDef], image_size: Optional[Tuple[int, int]]) -> None:
    payload: Dict = {"line_set_id": "default", "lines": [ln.to_json() for ln in lines], "roi": []}
    if image_size:
        payload["image_width"] = int(image_size[0])
        payload["image_height"] = int(image_size[1])
    lines_path.parent.mkdir(parents=True, exist_ok=True)
    lines_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


class TrajectoryViewerWindow(QMainWindow):
    """궤적 보기 + 세션/영상 선택 + 분석라인 편집."""

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
        self.setWindowTitle("궤적 보기")
        self.resize(1350, 1000)
        # 메인 UI보다 글자 크기를 한 단계 낮춤(궤적보기는 컨트롤 밀집도가 높음)
        try:
            f = self.font()
            pt = float(f.pointSizeF() if f.pointSizeF() > 0 else f.pointSize())
            if pt > 1:
                f.setPointSizeF(max(6.0, pt - 1.0))
                self.setFont(f)
        except Exception:
            pass
        self.config_path = config_path
        self._config_geometry_key = "trajectory_window_geometry"
        self._config_splitter_key = "trajectory_splitter_sizes"
        self._config_opts_key = "trajectory_viewer_options"
        self._restoring_options = False
        self._restore_session_id: Optional[str] = None
        self._restore_slot_index: Optional[int] = None

        self.resize_target = resize or (1312, 736)
        self.track_width = 3.5
        # 라인 편집: 저장은 "원본(base) 좌표", 화면 표시/클릭은 "scene(표시) 좌표"
        self._pending_line_points: List[Tuple[float, float]] = []  # base coords
        self._pending_items: List[object] = []
        self._lines: List[LineDef] = []
        self._line_items: List[QGraphicsLineItem] = []
        self._line_label_items: List[QGraphicsTextItem] = []
        self._track_items: List[QGraphicsPathItem] = []
        self._bg_item: Optional[QGraphicsPixmapItem] = None
        self._bg_size: Tuple[float, float] = (0.0, 0.0)
        self._lines_base_size: Optional[Tuple[int, int]] = None
        self._load_thread: Optional[QThread] = None
        self._load_worker: Optional[_TrajectoryLoadWorker] = None
        self._loading_token = 0
        self._color_idx = 0
        self._scale_sx = 1.0
        self._scale_sy = 1.0

        self.db_input = QLineEdit(str(db_path))
        self.session_combo = QComboBox()
        self.slot_combo = QComboBox()
        self.video_input = QLineEdit(str(video_path) if video_path else "")
        self.lines_input = QLineEdit(str(lines_path) if lines_path else "config/lines.json")

        self.reload_btn = QPushButton("새로고침")
        self.quit_btn = QPushButton("프로그램 종료")
        # 강조(빨간 글씨 + 진하게)
        self.quit_btn.setStyleSheet("QPushButton { color: #ff4d4d; font-weight: 800; }")
        self.refresh_sessions_btn = QPushButton("목록 갱신")
        self.refresh_slots_btn = QPushButton("슬롯 목록 갱신")
        self.pick_video_btn = QPushButton("영상 선택")
        self.pick_db_btn = QPushButton("DB 선택")
        self.pick_lines_btn = QPushButton("Lines 선택")

        self.width_spin = QDoubleSpinBox()
        self.width_spin.setRange(0.5, 12.0)
        self.width_spin.setSingleStep(0.5)
        self.width_spin.setValue(self.track_width)
        self.zoom_label = QLabel("zoom: 100%")
        self.fit_btn = QPushButton("전체 맞춤")
        self.reset_zoom_btn = QPushButton("100%")
        self.bg_toggle_btn = QPushButton("이미지 끄기")
        # 기본은 OFF (대용량 DB에서 렌더링/줌 작업이 더 가벼움)
        self._bg_enabled = False
        self.bg_toggle_btn.setText("이미지 켜기")
        self.frame_step_spin = QSpinBox()
        self.frame_step_spin.setRange(1, 120)
        self.frame_step_spin.setValue(5)
        self.max_tracks_spin = QSpinBox()
        self.max_tracks_spin.setRange(0, 20000)
        self.max_tracks_spin.setValue(1500)

        self.line_id_input = QLineEdit()
        self.line_id_input.setPlaceholderText("line_1")
        self.bound_input = QComboBox()
        self.bound_input.addItems(["", "north_bound", "south_bound", "east_bound", "west_bound"])
        self.bound_input.setCurrentText("north_bound")
        self.line_add_btn = QPushButton("라인 추가(2포인트)")
        self.line_delete_btn = QPushButton("선택 라인 삭제")
        self.line_clear_pts_btn = QPushButton("포인트 초기화")
        self.line_reset_btn = QPushButton("라인 전체 삭제")
        self.line_save_btn = QPushButton("Lines 저장")
        self.line_load_btn = QPushButton("Lines 불러오기")
        self.lines_list = QListWidget()
        self.lines_list.setMinimumHeight(220)
        # 라인 목록이 길어질 때 마우스 휠로 끝까지 볼 수 있게 스크롤바 활성화
        self.lines_list.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.lines_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self.status_label = QLabel("")

        self.view = _AutoFitView()
        self.view.setAlignment(Qt.AlignCenter)
        self.view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.view.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.view.setRenderHints(self.view.renderHints())
        self.view.set_click_callback(self._on_view_click)
        self.view.set_zoom_callback(self._on_zoom_changed)

        self.scene = QGraphicsScene()
        self.view.setScene(self.scene)

        self._build_ui()
        self._apply_style()
        self._wire_signals()

        self._restore_window_state()
        self._restore_options()

        self._refresh_sessions(select=session_id)
        # 창을 먼저 띄운 뒤 로딩 시작 (DB가 크면 즉시 로딩 시 UI가 멈추는 것처럼 보임)
        QTimer.singleShot(0, self._reload_all)
        QTimer.singleShot(0, lambda: self.view.set_fit_rect(self.scene.sceneRect()))

    def _on_zoom_changed(self, scale_x: float) -> None:
        try:
            pct = int(round(float(scale_x) * 100.0))
        except Exception:
            pct = 100
        self.zoom_label.setText(f"zoom: {pct}%")

    def _build_ui(self) -> None:
        self.splitter = QSplitter(Qt.Orientation.Vertical)
        self.splitter.setChildrenCollapsible(False)
        # 세로로 창을 늘릴 때 "위 이미지 뷰"는 크기가 크게 변하지 않고,
        # 아래 패널(메뉴/리스트)이 공간을 더 쓰도록 한다.
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)

        top = QWidget()
        top_layout = QVBoxLayout()
        top_layout.setContentsMargins(6, 6, 6, 6)
        top_layout.addWidget(self.view, stretch=1)
        top.setLayout(top_layout)

        bottom = QWidget()
        bottom_layout = QHBoxLayout()
        # 그룹박스 제목이 내용 위로 겹치지 않게 약간 아래로 내림
        bottom_layout.setContentsMargins(8, 10, 8, 6)
        bottom_layout.setSpacing(8)
        bottom.setLayout(bottom_layout)
        bottom.setMinimumHeight(240)

        traj_box = QGroupBox("궤적/표시 선택")
        traj_grid = QGridLayout()
        # 제목 영역 확보를 위해 상단 마진을 넉넉히 주고, 박스 간격을 조금 띄움
        traj_grid.setContentsMargins(10, 16, 10, 8)
        traj_grid.setHorizontalSpacing(14)
        traj_grid.setVerticalSpacing(8)
        traj_grid.setAlignment(Qt.AlignmentFlag.AlignTop)

        def cap(text: str) -> QLabel:
            lbl = QLabel(text)
            lbl.setProperty("role", "tv_caption")
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            # 캡션 박스 높이를 옆 입력란과 최대한 맞춘다.
            try:
                h = int(
                    max(
                        self.db_input.sizeHint().height(),
                        self.session_combo.sizeHint().height(),
                        self.slot_combo.sizeHint().height(),
                        self.video_input.sizeHint().height(),
                        self.lines_input.sizeHint().height(),
                        self.pick_db_btn.sizeHint().height(),
                        self.pick_video_btn.sizeHint().height(),
                        self.pick_lines_btn.sizeHint().height(),
                        self.width_spin.sizeHint().height(),
                    )
                )
                if h > 0:
                    lbl.setFixedHeight(h)
            except Exception:
                pass
            return lbl

        def cap_list(text: str) -> QLabel:
            # 라인 목록 캡션은 리스트 위에서만 쓰이므로, 높이를 조금 줄여 더 얇게 보이게 한다.
            lbl = cap(text)
            try:
                base_h = int(lbl.sizeHint().height())
                target_h = max(20, int(base_h * 0.70))
                lbl.setFixedHeight(target_h)
            except Exception:
                pass
            return lbl

        # row 0: DB / session
        db_row = QHBoxLayout()
        db_row.addWidget(self.db_input, stretch=1)
        db_row.addWidget(self.pick_db_btn)

        sess_row = QHBoxLayout()
        sess_row.addWidget(self.session_combo, stretch=1)
        sess_row.addWidget(self.refresh_sessions_btn)

        traj_grid.addWidget(cap("DB 경로"), 0, 0)
        traj_grid.addWidget(self._wrap(db_row), 0, 1)
        traj_grid.addWidget(cap("session_id"), 0, 2)
        traj_grid.addWidget(self._wrap(sess_row), 0, 3)

        # row 1: slot / background video
        slot_row = QHBoxLayout()
        slot_row.addWidget(self.slot_combo, stretch=1)
        slot_row.addWidget(self.refresh_slots_btn)

        video_row = QHBoxLayout()
        video_row.addWidget(self.video_input, stretch=1)
        video_row.addWidget(self.pick_video_btn)

        traj_grid.addWidget(cap("슬롯(15분)"), 1, 0)
        traj_grid.addWidget(self._wrap(slot_row), 1, 1)
        traj_grid.addWidget(cap("배경 영상"), 1, 2)
        traj_grid.addWidget(self._wrap(video_row), 1, 3)

        # row 2: thickness / frame_step / max_tracks (single line)
        perf_row = QHBoxLayout()
        perf_row.addWidget(cap("궤적 두께"))
        perf_row.addWidget(self.width_spin)
        perf_row.addSpacing(14)
        perf_row.addWidget(cap("프레임 간격"))
        perf_row.addWidget(self.frame_step_spin)
        perf_row.addSpacing(14)
        perf_row.addWidget(cap("최대 트랙"))
        perf_row.addWidget(self.max_tracks_spin)
        perf_row.addStretch()
        traj_grid.addWidget(self._wrap(perf_row), 2, 0, 1, 4)

        # row 3: (spacer) - 남는 높이를 여기서 먹고, 아래 버튼줄(row 4)이 박스 하단에 고정되게 함
        traj_grid.addItem(QSpacerItem(0, 0, QSizePolicy.Minimum, QSizePolicy.Expanding), 3, 0, 1, 4)
        traj_grid.setRowStretch(3, 1)

        # row 4: zoom/move + refresh (single line) -> 박스 맨 아래에 고정
        zoom_row = QHBoxLayout()
        zoom_row.setSpacing(10)
        try:
            zoom_row.setAlignment(Qt.AlignmentFlag.AlignBottom)
        except Exception:
            pass
        zoom_row.addWidget(self.fit_btn)
        zoom_row.addWidget(self.reset_zoom_btn)
        zoom_row.addWidget(self.bg_toggle_btn)
        zoom_row.addSpacing(10)
        zoom_row.addWidget(self.reload_btn)
        zoom_row.addWidget(self.zoom_label)
        zoom_row.addWidget(self.quit_btn)
        zoom_wrap = QWidget()
        zoom_wrap.setLayout(zoom_row)
        # 하단 여백을 조금 줘서 버튼들이 바닥에 붙어 보이지 않게 함
        try:
            zoom_row.setContentsMargins(0, 6, 0, 6)
        except Exception:
            pass
        traj_grid.addWidget(zoom_wrap, 4, 0, 1, 4)

        # 세션 선택란을 더 넓게(창을 넓혔을 때 오른쪽 컬럼이 더 늘어나도록)
        traj_grid.setColumnStretch(1, 4)
        traj_grid.setColumnStretch(3, 6)

        traj_box.setLayout(traj_grid)

        line_box = QGroupBox("분석라인 설정")
        line_grid = QGridLayout()
        line_grid.setContentsMargins(10, 16, 10, 8)
        line_grid.setHorizontalSpacing(14)
        line_grid.setVerticalSpacing(6)
        line_grid.setAlignment(Qt.AlignmentFlag.AlignTop)

        lines_row = QHBoxLayout()
        lines_row.addWidget(self.lines_input, stretch=1)
        lines_row.addWidget(self.pick_lines_btn)
        line_grid.addWidget(cap("Lines JSON"), 0, 0)
        line_grid.addWidget(self._wrap(lines_row), 0, 1, 1, 3)

        # row 1: line_id + bound in one line
        line_grid.addWidget(cap("라인 ID"), 1, 0)
        line_grid.addWidget(self.line_id_input, 1, 1)
        line_grid.addWidget(cap("bound"), 1, 2)
        line_grid.addWidget(self.bound_input, 1, 3)

        # row 2: list on left, actions on right (2-row grid)
        self.lines_list.setMinimumHeight(280)
        self.lines_list.setMaximumHeight(480)

        left_v = QVBoxLayout()
        left_v.setSpacing(4)
        left_v.addWidget(cap_list("라인 목록"))
        left_v.addWidget(self.lines_list, stretch=1)

        right_grid = QGridLayout()
        right_grid.setHorizontalSpacing(8)
        right_grid.setVerticalSpacing(18)
        try:
            right_grid.setContentsMargins(0, 10, 0, 0)
        except Exception:
            pass
        right_grid.addWidget(self.line_add_btn, 0, 0)
        right_grid.addWidget(self.line_delete_btn, 0, 1)
        right_grid.addWidget(self.line_clear_pts_btn, 1, 0)
        right_grid.addWidget(self.line_reset_btn, 1, 1)
        right_grid.addWidget(self.line_load_btn, 2, 0)
        right_grid.addWidget(self.line_save_btn, 2, 1)
        right_grid.setRowStretch(3, 1)

        list_row = QHBoxLayout()
        list_row.addLayout(left_v, stretch=4)
        list_row.addSpacing(10)
        list_row.addLayout(right_grid, stretch=6)
        line_grid.addWidget(self._wrap(list_row), 2, 0, 1, 4)

        line_box.setLayout(line_grid)

        bottom_layout.addWidget(traj_box, stretch=1)
        bottom_layout.addWidget(line_box, stretch=1)

        self.splitter.addWidget(top)
        self.splitter.addWidget(bottom)
        self.splitter.setSizes([760, 240])

        container = QWidget()
        layout = QVBoxLayout()
        layout.setContentsMargins(6, 6, 6, 6)
        layout.addWidget(self.splitter, stretch=1)
        layout.addWidget(self.status_label)
        container.setLayout(layout)
        self.setCentralWidget(container)

    def _apply_style(self) -> None:
        # 궤적보기 전용: 캡션 박스 높이/패딩을 줄여 입력란과 높이를 맞추고, 제목 겹침을 완화
        self.setStyleSheet(
            """
            QLabel[role="tv_caption"] {
                background: #2b3342;
                color: #e8f0ff;
                border: 1px solid #3d4656;
                border-radius: 6px;
                padding: 2px 8px;
                font-weight: 800;
                min-height: 24px;
            }
            """
        )

    def _restore_window_state(self) -> None:
        if not self.config_path or not self.config_path.exists():
            return
        try:
            cfg = load_app_config(self.config_path)
            geo = cfg.get(self._config_geometry_key)
            if isinstance(geo, str) and geo:
                self._apply_geometry_str(geo)
            sizes = cfg.get(self._config_splitter_key)
            if isinstance(sizes, list) and sizes and all(isinstance(x, int) for x in sizes):
                try:
                    self.splitter.setSizes([int(x) for x in sizes])
                except Exception:
                    pass
        except Exception:
            return

    def closeEvent(self, event) -> None:
        self._cancel_loader()
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
            return

        self._restoring_options = True
        try:
            db_path = opts.get("db_path")
            if isinstance(db_path, str) and db_path:
                self.db_input.setText(db_path)

            lines_path = opts.get("lines_path")
            if isinstance(lines_path, str) and lines_path:
                self.lines_input.setText(lines_path)
                self._load_lines_from_path()

            video_path = opts.get("video_path")
            if isinstance(video_path, str):
                self.video_input.setText(video_path)

            try:
                w = float(opts.get("track_width", self.track_width))
                if 0.1 < w <= 50:
                    self.track_width = w
                    self.width_spin.setValue(w)
            except Exception:
                pass

            try:
                fs = int(opts.get("frame_step", int(self.frame_step_spin.value())))
                if fs > 0:
                    self.frame_step_spin.setValue(fs)
            except Exception:
                pass

            try:
                mt = int(opts.get("max_tracks", int(self.max_tracks_spin.value())))
                if mt >= 0:
                    self.max_tracks_spin.setValue(mt)
            except Exception:
                pass

            try:
                bg = bool(opts.get("bg_enabled", False))
                self._bg_enabled = bg
                self.bg_toggle_btn.setText("이미지 끄기" if bg else "이미지 켜기")
            except Exception:
                pass

            # session/slot은 목록 로딩 후 적용
            sid = opts.get("session_id")
            self._restore_session_id = str(sid) if isinstance(sid, str) and sid else None
            sidx = opts.get("slot_index")
            self._restore_slot_index = int(sidx) if isinstance(sidx, int) else None
        finally:
            self._restoring_options = False

    def _persist_options(self) -> None:
        if self._restoring_options:
            return
        if not self.config_path:
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

            sess_raw = self.session_combo.currentText().strip()
            opts["session_id"] = "" if (not sess_raw or sess_raw == "(전체)") else sess_raw
            try:
                sdata = self.slot_combo.currentData()
                opts["slot_index"] = int(sdata) if sdata is not None else None
            except Exception:
                opts["slot_index"] = None

            cfg[self._config_opts_key] = opts
            save_app_config(self.config_path, cfg)
        except Exception:
            return

    def _persist_window_state(self) -> None:
        if not self.config_path:
            return
        try:
            cfg = load_app_config(self.config_path) if self.config_path.exists() else {}
            cfg[self._config_geometry_key] = self._geometry_to_str()
            try:
                cfg[self._config_splitter_key] = [int(x) for x in self.splitter.sizes()]
            except Exception:
                pass
            save_app_config(self.config_path, cfg)
        except Exception:
            return

    def _geometry_to_str(self) -> str:
        g = self.geometry()
        return f"{g.width()}x{g.height()}+{g.x()}+{g.y()}"

    def _apply_geometry_str(self, s: str) -> None:
        m = re.match(r"^(\\d+)x(\\d+)\\+(-?\\d+)\\+(-?\\d+)$", s.strip())
        if not m:
            return
        w, h, x, y = (int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)))
        if w > 100 and h > 100:
            self.resize(w, h)
        self.move(x, y)

    def _wrap(self, layout: QHBoxLayout) -> QWidget:
        w = QWidget()
        try:
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(6)
        except Exception:
            pass
        w.setLayout(layout)
        return w

    def _wire_signals(self) -> None:
        self.refresh_sessions_btn.clicked.connect(lambda: self._refresh_sessions(select=self.session_combo.currentText() or None))
        self.refresh_slots_btn.clicked.connect(self._refresh_slots)
        self.reload_btn.clicked.connect(self._reload_all)
        # 전체 앱 종료가 아니라, 궤적보기 창만 닫기
        self.quit_btn.clicked.connect(self.close)
        self.width_spin.valueChanged.connect(self._on_width_changed)
        self.fit_btn.clicked.connect(self.view.fit_to_rect)
        self.reset_zoom_btn.clicked.connect(self.view.reset_zoom)
        self.bg_toggle_btn.clicked.connect(self._toggle_background)
        self.frame_step_spin.valueChanged.connect(self._on_perf_changed)
        self.max_tracks_spin.valueChanged.connect(self._on_perf_changed)

        self.pick_db_btn.clicked.connect(self._pick_db)
        self.pick_video_btn.clicked.connect(self._pick_video)
        self.pick_lines_btn.clicked.connect(self._pick_lines)

        self.line_add_btn.clicked.connect(self._add_line_from_pending)
        self.line_delete_btn.clicked.connect(self._delete_selected_line)
        self.line_clear_pts_btn.clicked.connect(self._clear_pending_points)
        self.line_reset_btn.clicked.connect(self._reset_lines)
        self.line_save_btn.clicked.connect(self._save_lines)
        self.line_load_btn.clicked.connect(self._load_lines_from_path)

        self.session_combo.currentTextChanged.connect(self._on_session_changed)
        self.slot_combo.currentTextChanged.connect(self._on_slot_changed)

        try:
            self.db_input.editingFinished.connect(self._persist_options)
            self.lines_input.editingFinished.connect(self._persist_options)
            self.video_input.editingFinished.connect(self._persist_options)
        except Exception:
            pass

    def _on_session_changed(self, _txt: str) -> None:
        if self._restoring_options:
            return
        self._persist_options()
        self._reload_all()

    def _on_slot_changed(self, _txt: str) -> None:
        if self._restoring_options:
            return
        self._persist_options()
        self._reload_all()

    def _on_perf_changed(self, _val: int) -> None:
        if self._restoring_options:
            return
        self._persist_options()
        self._render_scene()

    def _pick_db(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "DB 선택", str(Path(self.db_input.text() or "output").resolve()), "SQLite (*.sqlite *.db);;All Files (*)")
        if path:
            self.db_input.setText(path)
            self._refresh_sessions(select=self.session_combo.currentText() or None)
            self._reload_all()
            self._persist_options()

    def _pick_video(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "영상 선택", str(Path(self.video_input.text() or ".").resolve()), "Video Files (*.mp4 *.avi *.mov *.mkv);;All Files (*)")
        if path:
            self.video_input.setText(path)
            try:
                self._ensure_base_matches_video(Path(path))
            except Exception:
                pass
            self._reload_all()
            self._persist_options()

    def _pick_lines(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Lines JSON 선택", str(Path(self.lines_input.text() or "config").resolve()), "JSON Files (*.json);;All Files (*)")
        if path:
            self.lines_input.setText(path)
            self._load_lines_from_path()
            self._render_scene()
            self._persist_options()

    def _on_width_changed(self, val: float) -> None:
        self.track_width = float(val)
        # 두께 변경은 DB 재로딩이 필요 없음: 기존 아이템 pen만 갱신
        for it in list(self._track_items):
            try:
                pen = it.pen()
                pen.setWidthF(self.track_width)
                it.setPen(pen)
            except Exception:
                continue
        self._persist_options()

    def _refresh_sessions(self, select: Optional[str] = None) -> None:
        db_path = Path(self.db_input.text().strip() or "output/tracks.sqlite")
        sessions = _load_sessions(db_path)
        self.session_combo.blockSignals(True)
        self.session_combo.clear()
        self.session_combo.addItem("(전체)")
        for s in sessions:
            self.session_combo.addItem(s)
        if select and select in sessions:
            self.session_combo.setCurrentText(select)
        elif sessions:
            # 빈값(전체)은 DB가 크면 매우 느림 → 기본은 최신 세션 선택
            self.session_combo.setCurrentText(sessions[-1])
        self.session_combo.blockSignals(False)
        # 복원 옵션 적용(최초 1회)
        if self._restore_session_id:
            idx = self.session_combo.findText(str(self._restore_session_id))
            if idx >= 0:
                self.session_combo.setCurrentIndex(idx)
            self._restore_session_id = None
        self._refresh_slots()

    def _reload_all(self) -> None:
        db_path = Path(self.db_input.text().strip() or "output/tracks.sqlite")
        if not db_path.exists():
            self.status_label.setText(f"DB 없음: {db_path}")
            self.scene.clear()
            return
        self._load_lines_from_path()
        self._render_scene()

    def _refresh_slots(self) -> None:
        db_path = Path(self.db_input.text().strip() or "output/tracks.sqlite")
        session_id = None if self.session_combo.currentIndex() <= 0 else self.session_combo.currentText().strip()

        slots: List[int] = []
        if db_path.exists():
            try:
                with sqlite3.connect(db_path) as conn:
                    try:
                        has_track_trajs = (
                            conn.execute(
                                "select 1 from sqlite_master where type='table' and name='track_trajs' limit 1"
                            ).fetchone()
                            is not None
                        )
                    except Exception:
                        has_track_trajs = False

                    if has_track_trajs:
                        sql = "select distinct (start_ts_ms / 900000) as slot from track_trajs where start_ts_ms is not null"
                        params: List[object] = []
                        if session_id:
                            sql += " and session_id = ?"
                            params.append(session_id)
                        sql += " order by slot"
                        rows = conn.execute(sql, tuple(params)).fetchall()
                        slots = [int(r[0]) for r in rows if r and r[0] is not None]
                    else:
                        sql = "select distinct (timestamp_ms / 900000) as slot from tracks where timestamp_ms is not null"
                        params2: List[object] = []
                        if session_id:
                            sql += " and session_id = ?"
                            params2.append(session_id)
                        sql += " order by slot"
                        rows = conn.execute(sql, tuple(params2)).fetchall()
                        slots = [int(r[0]) for r in rows if r and r[0] is not None]
            except Exception:
                slots = []

        cur_data = self.slot_combo.currentData()
        self.slot_combo.blockSignals(True)
        self.slot_combo.clear()
        self.slot_combo.addItem("(전체)", None)
        for s in slots[:5000]:
            start_min = int(s * 15)
            end_min = start_min + 15
            self.slot_combo.addItem(f"{s} ({start_min}~{end_min}분)", int(s))
        if cur_data is not None:
            idx = self.slot_combo.findData(cur_data)
            if idx >= 0:
                self.slot_combo.setCurrentIndex(idx)
        self.slot_combo.blockSignals(False)
        if self._restore_slot_index is not None:
            idx2 = self.slot_combo.findData(int(self._restore_slot_index))
            if idx2 >= 0:
                self.slot_combo.setCurrentIndex(idx2)
            self._restore_slot_index = None

    def _load_lines_from_path(self) -> None:
        lines_path = Path(self.lines_input.text().strip() or "config/lines.json")
        self._lines, self._lines_base_size = _read_lines(lines_path)
        self._clear_pending_points()
        self._refresh_lines_list()

    def _ensure_base_matches_video(self, video_path: Optional[Path]) -> None:
        """배경 영상 원본 해상도와 lines base가 다르면 자동 보정(라인 좌표도 함께 스케일)."""
        probed = _probe_video_size(video_path)
        if not probed:
            return
        new_w, new_h = int(probed[0]), int(probed[1])
        if new_w <= 0 or new_h <= 0:
            return

        base = self._lines_base_size
        # base가 없으면 채움
        if not (base and base[0] > 0 and base[1] > 0):
            self._lines_base_size = (new_w, new_h)
            return

        old_w, old_h = int(base[0]), int(base[1])
        if old_w == new_w and old_h == new_h:
            return

        # base가 잘못 저장된 경우(예: 2560x1440로 저장됐는데 실제 영상은 1920x1080),
        # 트랙이 한쪽만 보이거나(잘림) 라인이 어긋난다. → 라인 좌표를 새 base로 스케일.
        if self._lines:
            for ln in self._lines:
                ln.points = _rescale_points(ln.points, (old_w, old_h), (new_w, new_h))

        if self._pending_line_points:
            try:
                tmp = [[float(x), float(y)] for x, y in self._pending_line_points]
                tmp2 = _rescale_points(tmp, (old_w, old_h), (new_w, new_h))
                self._pending_line_points = [(float(p[0]), float(p[1])) for p in tmp2 if len(p) >= 2]
            except Exception:
                pass

        self._lines_base_size = (new_w, new_h)
        self._refresh_lines_list()

    def _save_lines(self) -> None:
        try:
            lines_path = Path(self.lines_input.text().strip() or "config/lines.json")
            video_path_str = self.video_input.text().strip()
            video_path = Path(video_path_str) if video_path_str else None
            self._ensure_base_matches_video(video_path)
            image_size = self._get_current_image_size_int()
            _write_lines(lines_path, self._lines, image_size=image_size)
            # viewer에서 저장한 lines 경로를 app_config에도 반영(카운팅/다음 실행에서 동일 경로 사용)
            if self.config_path:
                try:
                    cfg = load_app_config(self.config_path) if self.config_path.exists() else {}
                    cfg["count_lines_path"] = str(lines_path)
                    # 탐지/추적 단계에서 참조하는 line_settings_path도 동일 파일로 맞춤(원하면 나중에 분리 가능)
                    cfg["line_settings_path"] = str(lines_path)
                    save_app_config(self.config_path, cfg)
                except Exception:
                    pass
            QMessageBox.information(self, "저장 완료", f"저장: {lines_path}")
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "저장 실패", str(exc))

    def _reset_lines(self) -> None:
        self._lines = []
        self._clear_pending_points()
        self._refresh_lines_list()
        self._render_scene()

    def _clear_pending_points(self) -> None:
        self._pending_line_points = []
        for it in list(self._pending_items):
            try:
                self.scene.removeItem(it)
            except Exception:
                pass
        self._pending_items = []

    def _on_view_click(self, x: float, y: float) -> None:
        # 현재 화면 좌표계(배경/트랙 좌표계)에서 2포인트를 수집
        if len(self._pending_line_points) >= 2:
            return
        try:
            bx = float(x) / float(self._scale_sx or 1.0)
            by = float(y) / float(self._scale_sy or 1.0)
        except Exception:
            bx, by = float(x), float(y)
        self._pending_line_points.append((bx, by))
        sx = float(bx) * float(self._scale_sx or 1.0)
        sy = float(by) * float(self._scale_sy or 1.0)
        dot_pen = QPen(QColor("#ffd166"))
        dot_pen.setWidthF(2.0)
        dot = QGraphicsEllipseItem(sx - 4, sy - 4, 8, 8)
        dot.setPen(dot_pen)
        self.scene.addItem(dot)
        self._pending_items.append(dot)

        if len(self._pending_line_points) == 2:
            (x1, y1), (x2, y2) = self._pending_line_points
            x1s, y1s = float(x1) * float(self._scale_sx or 1.0), float(y1) * float(self._scale_sy or 1.0)
            x2s, y2s = float(x2) * float(self._scale_sx or 1.0), float(y2) * float(self._scale_sy or 1.0)
            dash_pen = QPen(QColor("#ffd166"))
            dash_pen.setWidthF(2.5)
            dash_pen.setStyle(Qt.PenStyle.DashLine)
            tmp_line = QGraphicsLineItem(x1s, y1s, x2s, y2s)
            tmp_line.setPen(dash_pen)
            self.scene.addItem(tmp_line)
            self._pending_items.append(tmp_line)
            self.status_label.setText("포인트 2개 선택됨 → '라인 추가'를 누르세요.")
        else:
            self.status_label.setText(f"포인트 1개 선택됨 ({x:.1f}, {y:.1f}) → 한 번 더 클릭하세요.")

    def _add_line_from_pending(self) -> None:
        if len(self._pending_line_points) < 2:
            QMessageBox.information(self, "포인트 부족", "화면에서 2포인트를 클릭해 라인을 지정하세요.")
            return
        line_id = self.line_id_input.text().strip() or f"line_{len(self._lines) + 1}"
        bound = self.bound_input.currentText().strip()
        (x1, y1), (x2, y2) = self._pending_line_points[:2]
        self._lines.append(LineDef(line_id=line_id, points=[[x1, y1], [x2, y2]], bound=bound))
        self._clear_pending_points()
        self._refresh_lines_list()
        self._render_scene()

    def _refresh_lines_list(self) -> None:
        self.lines_list.clear()
        for ln in self._lines:
            text = f"{ln.line_id}"
            if ln.bound:
                text += f" ({ln.bound})"
            item = QListWidgetItem(text)
            self.lines_list.addItem(item)

    def _delete_selected_line(self) -> None:
        row = self.lines_list.currentRow()
        if row < 0 or row >= len(self._lines):
            return
        self._lines.pop(row)
        self._refresh_lines_list()
        self._render_scene()

    def _get_current_image_size_int(self) -> Optional[Tuple[int, int]]:
        # 저장용 base는 "원본 좌표계" 기준이어야 한다.
        if self._lines_base_size and self._lines_base_size[0] > 0 and self._lines_base_size[1] > 0:
            return (int(self._lines_base_size[0]), int(self._lines_base_size[1]))
        # fallback: 배경 영상 원본 크기 probe
        try:
            video_path_str = self.video_input.text().strip()
            video_path = Path(video_path_str) if video_path_str else None
            probed = _probe_video_size(video_path)
            if probed and probed[0] > 0 and probed[1] > 0:
                return (int(probed[0]), int(probed[1]))
        except Exception:
            pass
        return None

    def _cancel_loader(self) -> None:
        th = self._load_thread
        if th is None:
            return
        try:
            if th.isRunning():
                th.requestInterruption()
                th.quit()
                if not th.wait(5000):
                    try:
                        th.terminate()
                    except Exception:
                        pass
                    th.wait(1500)
        except Exception:
            pass
        try:
            if th.isRunning():
                return
        except Exception:
            return
        self._load_thread = None
        self._load_worker = None

    def _set_status_if_token(self, msg: str, token: int) -> None:
        if token != self._loading_token:
            return
        self.status_label.setText(str(msg))

    def _sender_token(self) -> Optional[int]:
        try:
            snd = self.sender()
            if snd is None:
                return None
            return int(getattr(snd, "token", 0))
        except Exception:
            return None

    @Slot(object)
    def _on_tracks_batch_queued(self, batch: object) -> None:
        token = self._sender_token()
        if token is None:
            return
        self._on_tracks_batch(batch, token)

    @Slot(str)
    def _on_worker_progress(self, msg: str) -> None:
        token = self._sender_token()
        if token is None:
            return
        self._set_status_if_token(str(msg), token)

    @Slot(str)
    def _on_worker_error(self, msg: str) -> None:
        token = self._sender_token()
        if token is None:
            return
        self._set_status_if_token(f"[ERROR] {msg}", token)

    @Slot(object)
    def _on_tracks_finished_queued(self, meta: object) -> None:
        token = self._sender_token()
        if token is None:
            return
        self._on_tracks_finished(meta, token)

    def _make_track_pens(self) -> List[QPen]:
        colors = [
            "#3e7dd1",
            "#d35f5f",
            "#4cbfa6",
            "#c4a000",
            "#ad7fa8",
            "#729fcf",
            "#e67e22",
            "#27ae60",
        ]
        pens = [QPen(QColor(c)) for c in colors]
        for pen in pens:
            pen.setWidthF(self.track_width)
        return pens

    def _render_lines_overlay_simple(self, scene_w: float, scene_h: float) -> None:
        line_pen = QPen(QColor("#ffb347"))
        line_pen.setWidthF(3.0)

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
            x1, y1 = pts[0]
            x2, y2 = pts[1]
            p1 = QPointF(float(x1) * sx, float(y1) * sy)
            p2 = QPointF(float(x2) * sx, float(y2) * sy)
            item = QGraphicsLineItem(p1.x(), p1.y(), p2.x(), p2.y())
            item.setPen(line_pen)
            item.setZValue(2)
            self.scene.addItem(item)
            self._line_items.append(item)
            label_text = str(ln.line_id or "")
            if label_text:
                mx = (p1.x() + p2.x()) / 2.0
                my = (p1.y() + p2.y()) / 2.0
                text_item = QGraphicsTextItem(label_text)
                text_item.setDefaultTextColor(QColor("#ffe066"))
                text_item.setZValue(3)
                rect = text_item.boundingRect()
                text_item.setPos(mx - rect.width() / 2.0, my - rect.height() - 4.0)
                self.scene.addItem(text_item)
                self._line_label_items.append(text_item)

    def _on_tracks_batch(self, batch: object, token: int) -> None:
        if token != self._loading_token:
            return
        if not isinstance(batch, list):
            return
        pens = self._make_track_pens()
        for item in batch:
            try:
                _tid, pts = item
            except Exception:
                continue
            if not isinstance(pts, list) or len(pts) < 2:
                continue
            pen = pens[self._color_idx % len(pens)]
            self._color_idx += 1
            path = QPainterPath()
            x0, y0 = pts[0]
            sx = float(self._scale_sx or 1.0)
            sy = float(self._scale_sy or 1.0)
            path.moveTo(float(x0) * sx, float(y0) * sy)
            for x, y in pts[1:]:
                path.lineTo(float(x) * sx, float(y) * sy)
            gitem = QGraphicsPathItem(path)
            gitem.setPen(pen)
            gitem.setZValue(1)
            gitem.setCacheMode(QGraphicsPathItem.CacheMode.DeviceCoordinateCache)
            self.scene.addItem(gitem)
            self._track_items.append(gitem)

    def _on_tracks_finished(self, meta: object, token: int) -> None:
        if token != self._loading_token:
            return
        track_cnt = 0
        if isinstance(meta, dict):
            try:
                track_cnt = int(meta.get("track_count") or 0)
            except Exception:
                track_cnt = 0
        db_path = Path(self.db_input.text().strip() or "output/tracks.sqlite")
        session_raw = self.session_combo.currentText().strip()
        session_id = None if (not session_raw or session_raw == "(전체)") else session_raw
        session_id = session_id or "(전체)"
        self.status_label.setText(
            f"DB={db_path.name} session_id={session_id or '(전체)'}  tracks={track_cnt}  lines={len(self._lines)}  (frame_step={self.frame_step_spin.value()})"
        )

    def _render_scene_async(self) -> None:
        db_path = Path(self.db_input.text().strip() or "output/tracks.sqlite")
        session_raw = self.session_combo.currentText().strip()
        session_id = None if (not session_raw or session_raw == "(전체)") else session_raw
        video_path_str = self.video_input.text().strip()
        video_path = Path(video_path_str) if video_path_str else None
        try:
            self._ensure_base_matches_video(video_path)
        except Exception:
            pass

        if not db_path.exists():
            self.status_label.setText(f"DB 없음: {db_path}")
            self.scene.clear()
            return

        self._cancel_loader()

        bg_item, bg_w, bg_h = (None, 0.0, 0.0)
        if self._bg_enabled:
            bg_item, bg_w, bg_h = _load_background(video_path, self.resize_target)
        self._bg_item = bg_item
        self._bg_size = (bg_w, bg_h)

        self.scene.clear()
        self._line_items = []
        self._line_label_items = []
        self._track_items = []
        if bg_item is not None:
            bg_item.setZValue(0)
            self.scene.addItem(bg_item)

        base = self._lines_base_size
        # lines.json에 image_width/image_height가 없으면, 트랙(원본 좌표계)을 scene(표시 리사이즈)로
        # 스케일할 기준이 없어 좌표가 "작게/잘리게" 보일 수 있음.
        # 이 경우 선택된 배경 영상의 원본 해상도를 base로 사용한다.
        if not (base and base[0] > 0 and base[1] > 0):
            probed = _probe_video_size(video_path)
            if probed:
                base = probed
                self._lines_base_size = probed
        # 화면(scene)은 표시용 리사이즈 크기 기준(기본 1312x736)으로 고정
        if self.resize_target and self.resize_target[0] > 0 and self.resize_target[1] > 0:
            scene_w, scene_h = float(self.resize_target[0]), float(self.resize_target[1])
        elif bg_w > 0 and bg_h > 0:
            scene_w, scene_h = bg_w, bg_h
        elif base and base[0] > 0 and base[1] > 0:
            scene_w, scene_h = float(base[0]), float(base[1])
        else:
            scene_w, scene_h = 900.0, 650.0
        self.scene.setSceneRect(0, 0, scene_w, scene_h)

        if base and base[0] > 0 and base[1] > 0:
            self._scale_sx = float(scene_w) / float(base[0])
            self._scale_sy = float(scene_h) / float(base[1])
        else:
            self._scale_sx = 1.0
            self._scale_sy = 1.0

        self._render_lines_overlay_simple(scene_w, scene_h)
        self.view.set_fit_rect(self.scene.sceneRect())

        self._loading_token += 1
        token = self._loading_token
        self._color_idx = 0

        session_id2 = None if self.session_combo.currentIndex() <= 0 else self.session_combo.currentText().strip()
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
        self._load_thread = thread
        self._load_worker = worker

        self.status_label.setText("로딩 중... (데이터가 많으면 시간이 걸릴 수 있어요)")
        thread.start()

    def _render_scene(self) -> None:
        # NOTE: DB가 크면 동기 로딩 시 UI가 멈추므로, 비동기 로딩으로 전환.
        self._render_scene_async()
        return
        db_path = Path(self.db_input.text().strip() or "output/tracks.sqlite")
        session_id = self.session_combo.currentText().strip() or None
        video_path_str = self.video_input.text().strip()
        video_path = Path(video_path_str) if video_path_str else None
        lines_path = Path(self.lines_input.text().strip() or "config/lines.json")

        self.status_label.setText("로딩 중... (데이터가 많으면 시간이 걸릴 수 있어요)")
        QTimer.singleShot(0, lambda: None)
        tracks, bbox = _load_trajectories(
            db_path,
            session_id=session_id,
            frame_step=int(self.frame_step_spin.value()),
            max_tracks=int(self.max_tracks_spin.value()),
        )
        bg_item, bg_w, bg_h = (None, 0.0, 0.0)
        if self._bg_enabled:
            bg_item, bg_w, bg_h = _load_background(video_path, self.resize_target)
        self._bg_item = bg_item
        self._bg_size = (bg_w, bg_h)

        self.scene.clear()
        self._line_items = []
        self._line_label_items = []
        if bg_item is not None:
            self.scene.addItem(bg_item)

        use_raw_coords = bg_item is not None and bg_w > 0 and bg_h > 0
        if use_raw_coords:
            scene_w, scene_h = bg_w, bg_h
            scale_x = scale_y = 1.0
            shift_x = shift_y = 0.0
        else:
            minx, maxx, miny, maxy = bbox
            width = maxx - minx if maxx > minx else 1.0
            height = maxy - miny if maxy > miny else 1.0
            target_w, target_h = 900.0, 650.0
            scale = min(target_w / width, target_h / height)
            scene_w, scene_h = width * scale, height * scale
            scale_x = scale_y = scale
            shift_x, shift_y = minx, miny

        self.scene.setSceneRect(0, 0, scene_w, scene_h)

        colors = [
            "#3e7dd1",
            "#d35f5f",
            "#4cbfa6",
            "#c4a000",
            "#ad7fa8",
            "#729fcf",
            "#e67e22",
            "#27ae60",
        ]
        pens = [QPen(QColor(c)) for c in colors]
        for pen in pens:
            pen.setWidthF(self.track_width)
        color_idx = 0

        # trajectories (use QPainterPath per track to avoid creating huge amounts of QGraphicsLineItem)
        for tid, pts in tracks.items():
            if len(pts) < 2:
                continue
            pen = pens[color_idx % len(pens)]
            color_idx += 1
            path = QPainterPath()
            x0, y0 = pts[0]
            if use_raw_coords:
                path.moveTo(x0, y0)
                for x, y in pts[1:]:
                    path.lineTo(x, y)
            else:
                path.moveTo((x0 - shift_x) * scale_x, (y0 - shift_y) * scale_y)
                for x, y in pts[1:]:
                    path.lineTo((x - shift_x) * scale_x, (y - shift_y) * scale_y)
            item = QGraphicsPathItem(path)
            item.setPen(pen)
            item.setCacheMode(QGraphicsPathItem.CacheMode.DeviceCoordinateCache)
            self.scene.addItem(item)

        # lines overlay
        line_pen = QPen(QColor("#ffb347"))
        line_pen.setWidthF(3.0)

        # If file base size differs from current bg size, scale.
        base = self._lines_base_size
        if use_raw_coords and base and bg_w > 0 and bg_h > 0 and base[0] > 0 and base[1] > 0:
            sx = bg_w / base[0]
            sy = bg_h / base[1]
        else:
            sx = sy = 1.0

        for ln in self._lines:
            pts = ln.points
            if len(pts) < 2:
                continue
            x1, y1 = pts[0]
            x2, y2 = pts[1]
            if use_raw_coords:
                p1 = QPointF(x1 * sx, y1 * sy)
                p2 = QPointF(x2 * sx, y2 * sy)
            else:
                p1 = QPointF((x1 - shift_x) * scale_x, (y1 - shift_y) * scale_y)
                p2 = QPointF((x2 - shift_x) * scale_x, (y2 - shift_y) * scale_y)
            item = QGraphicsLineItem(p1.x(), p1.y(), p2.x(), p2.y())
            item.setPen(line_pen)
            self.scene.addItem(item)
            self._line_items.append(item)
            label_text = str(ln.line_id or "")
            if label_text:
                mx = (p1.x() + p2.x()) / 2.0
                my = (p1.y() + p2.y()) / 2.0
                text_item = QGraphicsTextItem(label_text)
                text_item.setDefaultTextColor(QColor("#ffe066"))
                rect = text_item.boundingRect()
                text_item.setPos(mx - rect.width() / 2.0, my - rect.height() - 4.0)
                self.scene.addItem(text_item)
                self._line_label_items.append(text_item)

        self.view.set_fit_rect(self.scene.sceneRect())

        track_cnt = len(tracks)
        self.status_label.setText(
            f"DB={db_path.name} session_id={session_id or '(전체)'}  tracks={track_cnt}  lines={len(self._lines)}  (frame_step={self.frame_step_spin.value()})"
        )

    def _toggle_background(self) -> None:
        self._bg_enabled = not self._bg_enabled
        self.bg_toggle_btn.setText("이미지 켜기" if not self._bg_enabled else "이미지 끄기")
        self._render_scene()
        self._persist_options()
