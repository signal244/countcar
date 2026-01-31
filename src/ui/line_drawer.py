import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import cv2
from PySide6.QtCore import Qt, QPointF
from PySide6.QtGui import QColor, QImage, QPen, QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QGraphicsLineItem,
    QGraphicsTextItem,
    QGraphicsScene,
    QGraphicsView,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)


def cvimg_to_pixmap(frame) -> Optional[QPixmap]:
    if frame is None:
        return None
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    h, w, _ = rgb.shape
    max_side = 960
    scale = min(1.0, max_side / max(h, w))
    if scale < 1.0:
        new_w = int(w * scale)
        new_h = int(h * scale)
        rgb = cv2.resize(rgb, (new_w, new_h), interpolation=cv2.INTER_AREA)
        h, w = new_h, new_w
    bytes_per_line = 3 * w
    qimg = QImage(rgb.data, w, h, bytes_per_line, QImage.Format_RGB888)
    return QPixmap.fromImage(qimg)


def load_first_frame(video_path: Path):
    cap = cv2.VideoCapture(str(video_path))
    ok, frame = cap.read()
    cap.release()
    return frame if ok else None


class LineDrawerWindow(QMainWindow):
    """Draw lines on the first frame and save to JSON."""

    def __init__(self, video_path: Path, line_path: Path):
        super().__init__()
        self.video_path = video_path
        self.line_path = line_path
        self.setWindowTitle("라인 설정 (첫 프레임)")
        self.resize(1100, 800)

        self.lines: List[Dict] = []
        self.current_points: List[Tuple[float, float]] = []
        self.frame_h: Optional[int] = None
        self.frame_w: Optional[int] = None
        self.orig_h: Optional[int] = None
        self.orig_w: Optional[int] = None
        self.scale: float = 1.0
        self._line_item_refs: List[QGraphicsLineItem] = []
        self._line_label_refs: List[QGraphicsTextItem] = []

        self._load_json()
        self._build_ui()
        self._load_frame()
        self._draw_existing()

    def _build_ui(self) -> None:
        central = QWidget()
        layout = QVBoxLayout()
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)
        central.setLayout(layout)
        self.setCentralWidget(central)

        # top: graphics view
        self.scene = QGraphicsScene()
        self.view = QGraphicsView(self.scene)
        self.view.setRenderHints(self.view.renderHints())
        self.view.setMouseTracking(True)
        self.view.setMinimumHeight(620)
        self.view.mousePressEvent = self._on_mouse_press  # type: ignore

        # bottom: controls
        ctrl_wrap = QWidget()
        ctrl = QGridLayout()
        ctrl.setVerticalSpacing(8)
        ctrl.setHorizontalSpacing(10)
        ctrl_wrap.setLayout(ctrl)

        info = QLabel(f"영상: {self.video_path}")
        info.setWordWrap(True)
        self.size_info = QLabel("")
        self.size_info.setWordWrap(True)

        self.path_input = QLineEdit(str(self.line_path))
        self.path_input.setPlaceholderText("config/lines.json")
        pick_btn = QPushButton("라인 JSON 선택")
        pick_btn.clicked.connect(self._open_json)
        save_as_btn = QPushButton("저장 경로...")
        save_as_btn.clicked.connect(self._choose_save_path)

        self.id_input = QLineEdit()
        self.id_input.setPlaceholderText("line_1")
        self.bound_input = QComboBox()
        self.bound_input.addItems(["north_bound", "south_bound", "east_bound", "west_bound"])
        self.bound_input.setEditable(True)

        self.line_count_label = QLabel("")
        self._update_count_label()

        add_btn = QPushButton("현재 포인트로 라인 추가 (2포인트 필요)")
        add_btn.clicked.connect(self._add_line_from_points)

        reset_pts_btn = QPushButton("포인트 초기화")
        reset_pts_btn.clicked.connect(self._reset_points)
        reset_lines_btn = QPushButton("라인 전체 초기화")
        reset_lines_btn.clicked.connect(self._reset_lines)

        save_btn = QPushButton("저장")
        save_btn.clicked.connect(self._save_json)

        load_btn = QPushButton("불러오기")
        load_btn.clicked.connect(self._reload_from_path_input)

        self.line_list_box = QVBoxLayout()
        self.line_list_box.setSpacing(4)
        list_container = QWidget()
        list_container.setLayout(self.line_list_box)
        list_scroll = QScrollArea()
        list_scroll.setWidgetResizable(True)
        list_scroll.setWidget(list_container)
        list_scroll.setMinimumHeight(140)

        r = 0
        ctrl.addWidget(info, r, 0, 1, 2)
        ctrl.addWidget(self.size_info, r, 2, 1, 2)
        r += 1
        ctrl.addWidget(QLabel("라인 JSON"), r, 0)
        ctrl.addWidget(self.path_input, r, 1)
        ctrl.addWidget(pick_btn, r, 2)
        ctrl.addWidget(save_as_btn, r, 3)
        r += 1
        ctrl.addWidget(QLabel("라인 ID"), r, 0)
        ctrl.addWidget(self.id_input, r, 1)
        ctrl.addWidget(QLabel("bound"), r, 2)
        ctrl.addWidget(self.bound_input, r, 3)
        r += 1
        ctrl.addWidget(add_btn, r, 0, 1, 2)
        ctrl.addWidget(reset_pts_btn, r, 2)
        ctrl.addWidget(reset_lines_btn, r, 3)
        r += 1
        ctrl.addWidget(self.line_count_label, r, 0, 1, 2)
        ctrl.addWidget(load_btn, r, 2)
        ctrl.addWidget(save_btn, r, 3)
        r += 1
        ctrl.addWidget(list_scroll, r, 0, 1, 4)

        layout.addWidget(self.view, stretch=1)
        layout.addWidget(ctrl_wrap)

    def _load_frame(self) -> None:
        frame = load_first_frame(self.video_path)
        if frame is None:
            QMessageBox.critical(self, "에러", "영상의 첫 프레임을 불러올 수 없습니다.")
            return
        self.orig_h, self.orig_w = frame.shape[:2]
        pix = cvimg_to_pixmap(frame)
        if pix:
            self.scene.clear()
            self._line_item_refs.clear()
            self._line_label_refs.clear()
            self.scene.addPixmap(pix)
            # 픽스된 표시 크기에 맞춰 bound 계산 기준을 설정
            self.frame_w = pix.width()
            self.frame_h = pix.height()
            if self.orig_w and self.orig_w > 0:
                self.scale = self.frame_w / float(self.orig_w)
            else:
                self.scale = 1.0
            try:
                self.size_info.setText(
                    f"원본: {self.orig_w}x{self.orig_h} | 표시: {self.frame_w}x{self.frame_h} | scale={self.scale:.4f}\n"
                    "라인 좌표는 '원본 크기' 기준으로 저장됩니다."
                )
            except Exception:
                pass

    def _load_json(self) -> None:
        try:
            with open(self.line_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.lines = data.get("lines", [])
            self.loaded_data = data
        except Exception:
            self.lines = []
            self.loaded_data = {"line_set_id": "default", "lines": [], "roi": []}

    def _draw_existing(self) -> None:
        for item in self._line_item_refs:
            self.scene.removeItem(item)
        self._line_item_refs.clear()
        for item in self._line_label_refs:
            self.scene.removeItem(item)
        self._line_label_refs.clear()
        pen = QPen(Qt.green, 2)
        for ln in self.lines:
            pts = ln.get("points") or []
            if len(pts) >= 2:
                # 저장된 좌표는 원본 해상도 기준 → 화면 스케일로 변환
                p1 = QPointF(pts[0][0] * self.scale, pts[0][1] * self.scale)
                p2 = QPointF(pts[1][0] * self.scale, pts[1][1] * self.scale)
                item = QGraphicsLineItem(p1.x(), p1.y(), p2.x(), p2.y())
                item.setPen(pen)
                self.scene.addItem(item)
                self._line_item_refs.append(item)
                label_text = str(ln.get("name") or ln.get("id") or "line")
                if label_text:
                    mx = (p1.x() + p2.x()) / 2.0
                    my = (p1.y() + p2.y()) / 2.0
                    text_item = QGraphicsTextItem(label_text)
                    text_item.setDefaultTextColor(QColor("#ffe066"))
                    text_item.setZValue(3)
                    rect = text_item.boundingRect()
                    text_item.setPos(mx - rect.width() / 2.0, my - rect.height() - 4.0)
                    self.scene.addItem(text_item)
                    self._line_label_refs.append(text_item)
        self._refresh_line_list()

    def _on_mouse_press(self, event) -> None:
        pos = self.view.mapToScene(event.pos())
        if self.frame_w and self.frame_h:
            if pos.x() < 0 or pos.y() < 0 or pos.x() > self.frame_w or pos.y() > self.frame_h:
                return
        self.current_points.append((pos.x(), pos.y()))
        pen = QPen(Qt.yellow, 3, Qt.DashLine)
        dot = QGraphicsLineItem(pos.x(), pos.y(), pos.x() + 1, pos.y() + 1)
        dot.setPen(pen)
        self.scene.addItem(dot)
        if len(self.current_points) == 2:
            p1, p2 = self.current_points
            line = QGraphicsLineItem(p1[0], p1[1], p2[0], p2[1])
            line.setPen(QPen(Qt.cyan, 2))
            self.scene.addItem(line)
            # 자동 bound 계산 후 입력란 반영
            pts_orig = [
                (p1[0] / self.scale if self.scale else p1[0], p1[1] / self.scale if self.scale else p1[1]),
                (p2[0] / self.scale if self.scale else p2[0], p2[1] / self.scale if self.scale else p2[1]),
            ]
            bound = self._auto_bound(pts_orig)
            self.bound_input.setCurrentText(bound)
        self._update_count_label()

    def _add_line_from_points(self) -> None:
        if len(self.current_points) < 2:
            QMessageBox.information(self, "포인트 부족", "두 포인트를 클릭해 라인을 지정하세요.")
            return
        line_id = self.id_input.text().strip() or f"line_{len(self.lines)+1}"
        bound = str(self.bound_input.currentText()).strip()
        pts_disp = self.current_points[:2]
        pts = [(x / self.scale if self.scale else x, y / self.scale if self.scale else y) for (x, y) in pts_disp]
        bound = bound or self._auto_bound(pts)
        self.bound_input.setCurrentText(bound)
        new_line = {"id": line_id, "points": [[pts[0][0], pts[0][1]], [pts[1][0], pts[1][1]]], "bound": bound}
        self.lines.append(new_line)
        self.loaded_data["lines"] = self.lines
        self._reset_points()
        self._load_frame()
        self._draw_existing()
        self._update_count_label()
        QMessageBox.information(self, "라인 추가", f"추가됨: {line_id} ({bound})")

    def _reset_points(self) -> None:
        self.current_points = []

    def _choose_save_path(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self,
            "라인 설정 JSON 저장",
            str(self.line_path),
            "JSON Files (*.json);;All Files (*)",
        )
        if path:
            self.line_path = Path(path)
            if hasattr(self, "path_input"):
                self.path_input.setText(str(self.line_path))

    def _reload_from_path_input(self) -> None:
        path_str = self.path_input.text().strip() if hasattr(self, "path_input") else ""
        if not path_str:
            return
        self.line_path = Path(path_str)
        self._load_json()
        self._load_frame()
        self._draw_existing()
        self._update_count_label()

    def _save_json(self) -> None:
        try:
            path_str = self.path_input.text().strip() if hasattr(self, "path_input") else ""
            if path_str:
                self.line_path = Path(path_str)
            else:
                self._choose_save_path()
            try:
                for ln in self.lines:
                    if isinstance(ln, dict):
                        ln.pop("direction", None)
            except Exception:
                pass
            if self.orig_w:
                self.loaded_data["image_width"] = self.orig_w
            if self.orig_h:
                self.loaded_data["image_height"] = self.orig_h
            with open(self.line_path, "w", encoding="utf-8") as f:
                json.dump(self.loaded_data, f, indent=2, ensure_ascii=False)
            if hasattr(self, "path_input"):
                self.path_input.setText(str(self.line_path))
            QMessageBox.information(self, "저장 완료", f"저장: {self.line_path}")
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "저장 실패", str(exc))

    def _open_json(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "라인 설정 JSON 열기",
            str(self.line_path.parent),
            "JSON Files (*.json);;All Files (*)",
        )
        if not path:
            return
        self.line_path = Path(path)
        if hasattr(self, "path_input"):
            self.path_input.setText(str(self.line_path))
        self._load_json()
        self._load_frame()
        self._draw_existing()
        self._update_count_label()

    def _update_count_label(self) -> None:
        self.line_count_label.setText(f"라인 개수: {len(self.lines)} | 현재 포인트: {len(self.current_points)}")

    def _auto_bound(self, pts: List[Tuple[float, float]]) -> str:
        """
        자동 bound 판단:
        - 라인 기울기 기준으로 수직/수평을 우선 결정.
        - 수직 위주라면 X 위치로 west/east, 수평 위주라면 Y 위치로 north/south.
        - 중간 기울기는 화면 중심 대비 좌/우 또는 상/하로 결정.
        """
        base_w = self.orig_w or self.frame_w
        base_h = self.orig_h or self.frame_h
        if base_h is None or base_w is None:
            return "north_bound"
        (x1, y1), (x2, y2) = pts
        midx = (x1 + x2) / 2.0
        midy = (y1 + y2) / 2.0
        dx = abs(x2 - x1)
        dy = abs(y2 - y1)
        # 기울기 임계: 수직 성향이 강하면 X 기준, 수평 성향이 강하면 Y 기준
        if dy > dx * 0.6:
            return "west_bound" if midx < base_w / 2 else "east_bound"
        if dx > dy * 0.6:
            return "north_bound" if midy < base_h / 2 else "south_bound"
        # 애매하면 화면 중심 대비 가까운 축으로 결정
        dist_left = midx
        dist_right = base_w - midx
        dist_top = midy
        dist_bottom = base_h - midy
        if max(dist_left, dist_right) >= max(dist_top, dist_bottom):
            return "west_bound" if midx < base_w / 2 else "east_bound"
        return "north_bound" if midy < base_h / 2 else "south_bound"

    def _reset_lines(self) -> None:
        self.lines = []
        self.loaded_data["lines"] = self.lines
        self._reset_points()
        self._load_frame()
        self._draw_existing()
        self._update_count_label()

    def _refresh_line_list(self) -> None:
        while self.line_list_box.count():
            item = self.line_list_box.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()
        for idx, ln in enumerate(self.lines):
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            label = QLabel(f"{ln.get('id','line')}: {ln.get('bound','')}")
            btn = QPushButton("삭제")
            btn.setFixedWidth(60)

            def make_remover(i: int):
                return lambda: self._remove_line(i)

            btn.clicked.connect(make_remover(idx))
            row.addWidget(label)
            row.addStretch()
            row.addWidget(btn)
            wrap = QWidget()
            wrap.setLayout(row)
            self.line_list_box.addWidget(wrap)

    def _remove_line(self, idx: int) -> None:
        if 0 <= idx < len(self.lines):
            removed = self.lines.pop(idx)
            self.loaded_data["lines"] = self.lines
            self._load_frame()
            self._draw_existing()
            self._update_count_label()
            QMessageBox.information(self, "라인 삭제", f"삭제됨: {removed.get('id')}")
