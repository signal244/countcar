import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from src.config.device import resolve_device
from src.config.loader import load_app_config, load_json, load_line_settings
from src.db.schema import init_db
from src.db.writer import TrackTrajDBWriter
from src.pipeline.detect_track import DetectionTracker


def _max_xy(points: List[List[float]]) -> tuple[float, float]:
    mx = my = 0.0
    for p in points or []:
        if not isinstance(p, list) or len(p) < 2:
            continue
        try:
            mx = max(mx, float(p[0]))
            my = max(my, float(p[1]))
        except Exception:
            continue
    return mx, my


def _max_line_xy(lines: List[Dict]) -> tuple[float, float]:
    mx = my = 0.0
    for ln in lines or []:
        pts = ln.get("points") or []
        if len(pts) < 2:
            continue
        for p in pts:
            if not isinstance(p, list) or len(p) < 2:
                continue
            try:
                mx = max(mx, float(p[0]))
                my = max(my, float(p[1]))
            except Exception:
                continue
    return mx, my


def build_tracker(cfg: Dict, line_cfg: Dict, class_mapping: Dict[str, str]):
    roi: List[List[float]] = cfg.get("roi") or line_cfg.get("roi", [])
    line_set_id: Optional[str] = line_cfg.get("line_set_id")
    resize = None
    if cfg.get("resize_width") and cfg.get("resize_height"):
        resize = (int(cfg["resize_width"]), int(cfg["resize_height"]))
    yolo_imgsz = int(cfg.get("yolo_imgsz") or (resize[0] if resize else 640))
    yolo_rect = bool(cfg.get("yolo_rect", True))

    # ROI 좌표계 자동 보정(호환성):
    # - YOLO 결과 좌표는 orig=원본 프레임 좌표계로 복원되어 DB에 저장됨.
    # - 그런데 line_settings.json의 roi가 "리사이즈 좌표계"로 저장되어 있으면(예: max_x<=1312, max_y<=736),
    #   저장 단계(point_in_roi)에서 화면 일부만 통과되어 DB 좌표가 1200x650 근처로 잘리는 현상이 생김.
    # - 아래 조건을 만족하면 roi를 원본(image_width/image_height) 기준으로 스케일 업한다.
    try:
        if roi:
            iw = float(line_cfg.get("image_width") or 0.0)
            ih = float(line_cfg.get("image_height") or 0.0)
            if iw > 0 and ih > 0:
                roi_max_x, roi_max_y = _max_xy(roi)
                line_max_x, line_max_y = _max_line_xy(line_cfg.get("lines") or [])

                # roi가 "작은 좌표계"(예: 1280x720/1312x736 등)로 저장되었는데
                # line/YOLO 좌표는 원본(예: 2560x1440)으로 쓰는 경우, point_in_roi에서
                # 화면 일부만 통과하여 "좌상단만 탐지되는 것처럼" 보일 수 있다.
                # - resize_width/height가 설정되어 있으면 그 좌표계를 우선 사용
                # - 없으면 yolo_imgsz(예: 1280) + 원본 비율로 base_h를 추정하여 스케일 업
                base_w = None
                base_h = None
                if resize and resize[0] > 0 and resize[1] > 0:
                    base_w, base_h = float(resize[0]), float(resize[1])
                else:
                    base_w = float(max(1, int(yolo_imgsz)))
                    base_h = float(max(1, int(round(base_w * (ih / iw)))))

                if base_w and base_h and base_w > 0 and base_h > 0:
                    # 라인(또는 원본 정보)이 base보다 큰데 roi는 base 이내이면 mismatch로 간주
                    if (line_max_x > base_w + 2 or line_max_y > base_h + 2) and (
                        roi_max_x <= base_w + 2 and roi_max_y <= base_h + 2
                    ):
                        sx = iw / base_w
                        sy = ih / base_h
                        roi = [[float(p[0]) * sx, float(p[1]) * sy] for p in roi]
    except Exception:
        pass
    allowed_classes = cfg.get("allowed_classes")
    device = resolve_device(cfg.get("device"))

    model_path = Path(cfg["model_path"])
    if not model_path.exists():
        raise FileNotFoundError(f"Model file not found: {model_path}")

    tracker = DetectionTracker(
        model_path=model_path,
        class_mapping=class_mapping,
        camera_id=cfg.get("camera_id", "cam01"),
        roi=roi,
        line_set_id=line_set_id,
        confidence=float(cfg.get("confidence_threshold", 0.25)),
        device=device,
        tracker_config=Path(cfg["tracker_config"]) if cfg.get("tracker_config") else None,
        allowed_classes=[int(c) for c in allowed_classes] if allowed_classes else None,
        resize=resize,
        yolo_imgsz=yolo_imgsz,
        yolo_rect=yolo_rect,
        target_fps=float(cfg.get("target_fps")) if cfg.get("target_fps") else None,
        max_idle_frames=int(cfg.get("max_idle_frames", 30)),
        flush_interval_minutes=int(cfg.get("flush_interval_minutes", 15)) if cfg.get("flush_interval_minutes") else None,
    )
    return tracker, device


def _session_exists(conn: sqlite3.Connection, session_id: str) -> bool:
    try:
        row = conn.execute("SELECT 1 FROM track_trajs WHERE session_id=? LIMIT 1", (session_id,)).fetchone()
        if row:
            return True
    except sqlite3.OperationalError:
        pass
    try:
        row = conn.execute("SELECT 1 FROM tracks WHERE session_id=? LIMIT 1", (session_id,)).fetchone()
        if row:
            return True
    except sqlite3.OperationalError:
        pass
    return False


def _make_unique_session_id(conn: sqlite3.Connection, base: str) -> str:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    candidate = f"{base}_{stamp}"
    if not _session_exists(conn, candidate):
        return candidate
    for i in range(2, 1000):
        candidate_i = f"{candidate}_{i}"
        if not _session_exists(conn, candidate_i):
            return candidate_i
    raise RuntimeError("Failed to allocate unique session_id (too many collisions).")


def _probe_frame_count(video_path: Path) -> Optional[int]:
    try:
        import cv2  # local import (Colab/Windows env may vary)

        cap = cv2.VideoCapture(str(video_path))
        n = cap.get(cv2.CAP_PROP_FRAME_COUNT)
        cap.release()
        if n and n > 0:
            return int(n)
    except Exception:
        return None
    return None


def _make_progress_filter(
    total_frames: Optional[int],
    every_n_frames: int = 1000,
) -> Tuple[Callable[[str], None], Callable[[str], None]]:
    """
    Returns (emit, filtered_progress_cb).
    - emit(msg): always prints immediately (flush)
    - filtered_progress_cb(msg): passes through non-progress logs, but converts
      '[progress] frame <N>' into a friendly message every N frames.
    """

    def emit(msg: str) -> None:
        print(msg, flush=True)

    last_logged = {"frame": -10**18}

    def progress_cb(msg: str) -> None:
        if not isinstance(msg, str):
            emit(str(msg))
            return

        prefix = "[progress] frame "
        if not msg.startswith(prefix):
            emit(msg)
            return

        try:
            frame_id = int(msg[len(prefix) :].strip())
        except Exception:
            emit(msg)
            return

        if frame_id - last_logged["frame"] < every_n_frames:
            return
        last_logged["frame"] = frame_id

        if total_frames and total_frames > 0:
            pct = min(100.0, (float(frame_id + 1) / float(total_frames)) * 100.0)
            emit(f"[progress] {frame_id+1}/{total_frames} 프레임 처리중 ({pct:.1f}%)")
        else:
            emit(f"[progress] {frame_id} 프레임 처리중")

    return emit, progress_cb


def main() -> None:
    parser = argparse.ArgumentParser(description="Run detection+tracking and save to SQLite.")
    parser.add_argument("--config", default="config/app_config.json", help="App config path")
    parser.add_argument("--video", required=True, help="Video file path")
    parser.add_argument("--line-settings", default=None, help="(optional) Line/ROI config path")
    parser.add_argument("--session-id", default=None, help="Optional session id")
    parser.add_argument(
        "--overwrite-session",
        action="store_true",
        help="If set and the same session_id exists, delete existing rows and overwrite. "
        "Default behavior keeps existing data and auto-suffixes a timestamp.",
    )
    parser.add_argument(
        "--flush-minutes",
        type=int,
        choices=[5, 15, 30, 60],
        help="DB flush interval in minutes (overrides config; 5/15/30/60).",
    )
    args = parser.parse_args()

    cfg_path = Path(args.config)
    cfg = load_app_config(cfg_path)

    # 탐지/추적→DB 저장 단계에서는 ROI/라인설정이 필수가 아니다.
    # - line-settings를 주면 ROI/라인을 로드(ROI 필터링 등)
    # - 안 주면 ROI/라인 없이 전체 화면을 대상으로 추적 결과만 저장
    line_cfg: Dict = {"line_set_id": None, "lines": [], "roi": [], "image_width": None, "image_height": None}
    line_path = None
    if args.line_settings:
        line_path = Path(args.line_settings)
    else:
        try:
            if cfg.get("line_settings_path"):
                line_path = Path(cfg["line_settings_path"])
        except Exception:
            line_path = None

    if line_path and line_path.exists():
        try:
            line_cfg = load_line_settings(line_path)
        except Exception:
            line_cfg = {"line_set_id": None, "lines": [], "roi": [], "image_width": None, "image_height": None}
    class_mapping = load_json(Path(cfg["class_mapping_path"])) if cfg.get("class_mapping_path") else {}

    if args.flush_minutes:
        cfg["flush_interval_minutes"] = args.flush_minutes

    tracker, device = build_tracker(cfg, line_cfg, class_mapping)
    db_path = Path(cfg["db_path"])

    video_path = Path(args.video)
    if not video_path.exists():
        raise FileNotFoundError(f"Video file not found: {video_path}")
    session_base = args.session_id or (str(cfg.get("session_id") or "").strip() or video_path.stem)
    total_frames = _probe_frame_count(video_path)

    # 동일 session_id 데이터가 있으면 삭제 후 새로 기록
    # 동일 session_id 처리: overwrite(덮어쓰기) 또는 시간 접미사로 새 세션 생성
    overwrite_session = bool(cfg.get("overwrite_session", False) or args.overwrite_session)
    init_db(db_path)

    session_id = session_base
    with sqlite3.connect(db_path) as conn:
        if overwrite_session:
            try:
                conn.execute("DELETE FROM track_trajs WHERE session_id = ?", (session_id,))
                conn.execute("DELETE FROM tracks WHERE session_id = ?", (session_id,))
                conn.commit()
            except sqlite3.OperationalError:
                pass
        else:
            if _session_exists(conn, session_id):
                session_id = _make_unique_session_id(conn, session_base)

    with TrackTrajDBWriter(db_path) as writer:
        emit, progress_cb = _make_progress_filter(total_frames, every_n_frames=1000)
        emit(f"[info] Using device: {device}")
        emit(f"[info] session_id={session_id}")
        if total_frames and total_frames > 0:
            emit(f"[info] total_frames={total_frames}")
        tracker.run(
            video_path=video_path,
            db_writer=writer,
            session_id=session_id,
            progress_cb=progress_cb,
        )


if __name__ == "__main__":
    main()
