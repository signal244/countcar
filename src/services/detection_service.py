from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional

from src.config.device import resolve_device
from src.config.loader import load_app_config, load_json, load_line_settings
from src.config.model_resolver import can_auto_download_model, ensure_model_source, resolve_model_source
from src.config.validation import format_warnings, validate_app_config, validate_line_settings
from src.db.schema import init_db
from src.db.writer import TrackTrajDBWriter
from src.pipeline.detect_track import DetectionTracker


ProgressCallback = Callable[[str], None]
StopCallback = Callable[[], bool]


@dataclass(frozen=True)
class TrackerBuildResult:
    tracker: DetectionTracker
    device: str
    info: Dict[str, object]


@dataclass(frozen=True)
class DetectionRequest:
    cfg_path: Path
    video_path: Path
    line_path: Optional[Path] = None
    overrides: Mapping[str, object] = field(default_factory=dict)
    session_id: Optional[str] = None
    overwrite_session: bool = False


@dataclass(frozen=True)
class DetectionResult:
    session_id: str
    db_path: Path
    device: str
    tracker_info: Dict[str, object]
    stopped: bool


def _empty_line_config() -> Dict[str, object]:
    return {"line_set_id": None, "lines": [], "roi": [], "image_width": None, "image_height": None}


def _project_root(cfg_path: Path) -> Path:
    cwd = Path.cwd().resolve()
    if (cwd / "src").is_dir():
        return cwd
    current = cfg_path.resolve().parent
    for candidate in (current, *current.parents):
        if (candidate / "src").is_dir():
            return candidate
    return cwd


def resolve_project_path(value: str | Path, cfg_path: Path) -> Path:
    path = Path(str(value)).expanduser()
    if path.is_absolute():
        return path
    root = _project_root(cfg_path)
    candidates = (root / path, cfg_path.resolve().parent / path)
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def _max_xy(points: List[List[float]]) -> tuple[float, float]:
    mx = my = 0.0
    for point in points or []:
        if not isinstance(point, list) or len(point) < 2:
            continue
        try:
            mx = max(mx, float(point[0]))
            my = max(my, float(point[1]))
        except (TypeError, ValueError):
            continue
    return mx, my


def _max_line_xy(lines: List[Dict]) -> tuple[float, float]:
    mx = my = 0.0
    for line in lines or []:
        for point in line.get("points") or []:
            if not isinstance(point, list) or len(point) < 2:
                continue
            try:
                mx = max(mx, float(point[0]))
                my = max(my, float(point[1]))
            except (TypeError, ValueError):
                continue
    return mx, my


def build_tracker(
    cfg: Dict,
    line_cfg: Dict,
    class_mapping: Dict[str, str],
    *,
    cfg_path: Path = Path("config/app_config.json"),
) -> TrackerBuildResult:
    roi: List[List[float]] = cfg.get("roi") or line_cfg.get("roi", [])
    line_set_id: Optional[str] = line_cfg.get("line_set_id")
    resize = None
    if cfg.get("resize_width") and cfg.get("resize_height"):
        resize = (int(cfg["resize_width"]), int(cfg["resize_height"]))
    yolo_imgsz = int(cfg.get("yolo_imgsz") or (resize[0] if resize else 640))
    yolo_rect = bool(cfg.get("yolo_rect", True))

    if roi:
        iw = float(line_cfg.get("image_width") or 0.0)
        ih = float(line_cfg.get("image_height") or 0.0)
        if iw > 0 and ih > 0:
            roi_max_x, roi_max_y = _max_xy(roi)
            line_max_x, line_max_y = _max_line_xy(line_cfg.get("lines") or [])
            if resize and resize[0] > 0 and resize[1] > 0:
                base_w, base_h = float(resize[0]), float(resize[1])
            else:
                base_w = float(max(1, yolo_imgsz))
                base_h = float(max(1, int(round(base_w * (ih / iw)))))
            if (line_max_x > base_w + 2 or line_max_y > base_h + 2) and (
                roi_max_x <= base_w + 2 and roi_max_y <= base_h + 2
            ):
                sx = iw / base_w
                sy = ih / base_h
                roi = [[float(point[0]) * sx, float(point[1]) * sy] for point in roi]

    device = resolve_device(cfg.get("device"))
    raw_model = str(cfg.get("model_path") or "").strip()
    model_value: str | Path = resolve_project_path(raw_model, cfg_path) if raw_model else raw_model
    model_source, _ = resolve_model_source(model_value)
    if not model_source:
        raise FileNotFoundError("Model path is empty")
    model_source = ensure_model_source(model_value)

    tracker_value = cfg.get("tracker_config")
    tracker_path = resolve_project_path(str(tracker_value), cfg_path) if tracker_value else None
    allowed_classes = cfg.get("allowed_classes")
    apply_class_mapping = not can_auto_download_model(raw_model)
    tracker = DetectionTracker(
        model_path=model_source,
        class_mapping=class_mapping,
        apply_class_mapping=apply_class_mapping,
        camera_id=cfg.get("camera_id", "cam01"),
        roi=roi,
        line_set_id=line_set_id,
        confidence=float(cfg.get("confidence_threshold", 0.25)),
        device=device,
        tracker_config=tracker_path,
        allowed_classes=[int(value) for value in allowed_classes] if allowed_classes else None,
        resize=resize,
        yolo_imgsz=yolo_imgsz,
        yolo_rect=yolo_rect,
        target_fps=float(cfg.get("target_fps")) if cfg.get("target_fps") else None,
        max_idle_frames=int(cfg.get("max_idle_frames", 30)),
        flush_interval_minutes=(
            int(cfg.get("flush_interval_minutes", 15)) if cfg.get("flush_interval_minutes") else None
        ),
    )
    return TrackerBuildResult(
        tracker=tracker,
        device=device,
        info={
            "config_model_path": raw_model,
            "resolved_model_path": str(model_source),
            "yolo_imgsz": yolo_imgsz,
            "yolo_rect": yolo_rect,
            "apply_class_mapping": apply_class_mapping,
        },
    )


def _session_exists(conn: sqlite3.Connection, session_id: str) -> bool:
    for table in ("track_trajs", "tracks"):
        try:
            row = conn.execute(f"SELECT 1 FROM {table} WHERE session_id=? LIMIT 1", (session_id,)).fetchone()
        except sqlite3.OperationalError:
            continue
        if row:
            return True
    return False


def _make_unique_session_id(conn: sqlite3.Connection, base: str) -> str:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    candidate = f"{base}_{stamp}"
    if not _session_exists(conn, candidate):
        return candidate
    for index in range(2, 1000):
        numbered = f"{candidate}_{index}"
        if not _session_exists(conn, numbered):
            return numbered
    raise RuntimeError("Failed to allocate a unique session_id.")


def _delete_session(conn: sqlite3.Connection, session_id: str) -> None:
    tables = (
        "track_line_summary",
        "track_line_events",
        "track_virtual_events",
        "merged_tracks",
        "track_merge_exclude_manual",
        "track_merge_map_manual",
        "track_merge_map_auto",
        "track_merge_map",
        "track_exclusion_map",
        "track_merge_runs",
        "track_exclusion_runs",
        "track_trajs",
        "tracks",
    )
    for table in tables:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1", (table,)
        ).fetchone()
        if exists:
            conn.execute(f"DELETE FROM {table} WHERE session_id = ?", (session_id,))


class DetectionService:
    """Headless detection workflow shared by Windows GUI, CLI, and Colab."""

    def run(
        self,
        request: DetectionRequest,
        *,
        progress_cb: Optional[ProgressCallback] = None,
        should_stop_cb: Optional[StopCallback] = None,
    ) -> DetectionResult:
        def emit(message: str) -> None:
            if progress_cb is not None:
                progress_cb(message)

        cfg_path = Path(request.cfg_path)
        video_path = Path(request.video_path)
        if not cfg_path.exists():
            raise FileNotFoundError(f"Config file not found: {cfg_path}")
        if not video_path.exists():
            raise FileNotFoundError(f"Video file not found: {video_path}")

        cfg = load_app_config(cfg_path)
        cfg.update(dict(request.overrides))
        app_report = validate_app_config(cfg)
        app_report.raise_for_errors()
        for message in format_warnings(app_report.warnings):
            emit(message)

        line_path = request.line_path
        if line_path is None and cfg.get("line_settings_path"):
            line_path = resolve_project_path(str(cfg["line_settings_path"]), cfg_path)
        elif line_path is not None:
            line_path = resolve_project_path(line_path, cfg_path)

        line_cfg: Dict = _empty_line_config()
        if line_path is not None:
            if not line_path.exists():
                emit(f"[config:warn] 라인 설정 파일을 찾지 못해 전체 화면을 사용합니다: {line_path}")
            else:
                line_cfg = load_line_settings(line_path)
                line_report = validate_line_settings(line_cfg)
                line_report.raise_for_errors("라인 설정 오류")
                for message in format_warnings(line_report.warnings):
                    emit(message)

        mapping_path = cfg.get("class_mapping_path")
        class_mapping: Dict[str, str] = {}
        if mapping_path:
            resolved_mapping = resolve_project_path(str(mapping_path), cfg_path)
            if not resolved_mapping.exists():
                raise FileNotFoundError(f"Class mapping file not found: {resolved_mapping}")
            class_mapping = load_json(resolved_mapping)

        build = build_tracker(cfg, line_cfg, class_mapping, cfg_path=cfg_path)
        db_path = resolve_project_path(str(cfg["db_path"]), cfg_path)
        init_db(db_path)

        session_base = str(
            request.session_id or cfg.get("session_id") or video_path.stem
        ).strip() or video_path.stem
        overwrite = bool(request.overwrite_session or cfg.get("overwrite_session", False))
        session_id = session_base
        with closing(sqlite3.connect(db_path)) as conn, conn:
            if overwrite:
                _delete_session(conn, session_id)
                conn.commit()
            elif _session_exists(conn, session_id):
                session_id = _make_unique_session_id(conn, session_base)

        emit(f"[info] Using device: {build.device}")
        emit(f"[info] session_id={session_id}")
        emit(
            f"[info] model(config)={build.info['config_model_path']} "
            f"model(resolved)={build.info['resolved_model_path']}"
        )
        emit(
            f"[info] yolo_imgsz={build.info['yolo_imgsz']} "
            f"rect={build.info['yolo_rect']} "
            f"class_mapping={build.info['apply_class_mapping']}"
        )
        with TrackTrajDBWriter(db_path) as writer:
            build.tracker.run(
                video_path=video_path,
                db_writer=writer,
                session_id=session_id,
                progress_cb=progress_cb,
                should_stop_cb=should_stop_cb,
            )
        stopped = bool(should_stop_cb and should_stop_cb())
        return DetectionResult(
            session_id=session_id,
            db_path=db_path,
            device=build.device,
            tracker_info=build.info,
            stopped=stopped,
        )
