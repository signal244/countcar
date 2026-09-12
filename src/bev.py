import copy
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

import cv2
import numpy as np


def _normalize_points(points: Sequence[Sequence[float]]) -> List[List[float]]:
    out: List[List[float]] = []
    for row in points or []:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        out.append([float(row[0]), float(row[1])])
    return out


def default_dst_points(output_size: Tuple[int, int]) -> List[List[float]]:
    width = max(1, int(output_size[0]))
    height = max(1, int(output_size[1]))
    return [
        [0.0, 0.0],
        [float(width - 1), 0.0],
        [0.0, float(height - 1)],
        [float(width - 1), float(height - 1)],
    ]


@dataclass
class BevConfig:
    src_points: List[List[float]]
    dst_points: List[List[float]]
    output_size: Tuple[int, int]
    source_size: Optional[Tuple[int, int]] = None
    enabled: bool = True

    def is_valid(self) -> bool:
        return len(self.src_points) == 4 and len(self.dst_points) == 4 and int(self.output_size[0]) > 0 and int(self.output_size[1]) > 0

    def to_json(self) -> dict:
        payload = {
            "enabled": bool(self.enabled),
            "src_points": _normalize_points(self.src_points),
            "dst_points": _normalize_points(self.dst_points),
            "output_width": int(self.output_size[0]),
            "output_height": int(self.output_size[1]),
        }
        if self.source_size and self.source_size[0] > 0 and self.source_size[1] > 0:
            payload["source_width"] = int(self.source_size[0])
            payload["source_height"] = int(self.source_size[1])
        return payload


def load_bev_config(path: Path) -> Optional[BevConfig]:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    src_points = _normalize_points(data.get("src_points") or [])
    out_w = int(data.get("output_width") or 0)
    out_h = int(data.get("output_height") or 0)
    dst_points = _normalize_points(data.get("dst_points") or default_dst_points((out_w, out_h)))
    source_width = int(data.get("source_width") or 0)
    source_height = int(data.get("source_height") or 0)
    cfg = BevConfig(
        src_points=src_points,
        dst_points=dst_points,
        output_size=(out_w, out_h),
        source_size=(source_width, source_height) if source_width > 0 and source_height > 0 else None,
        enabled=bool(data.get("enabled", True)),
    )
    return cfg if cfg.is_valid() else None


def save_bev_config(path: Path, cfg: BevConfig) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg.to_json(), ensure_ascii=False, indent=2), encoding="utf-8")


def _matrix(cfg: BevConfig) -> np.ndarray:
    if not cfg.is_valid():
        raise ValueError("invalid BEV config")
    src = np.float32(cfg.src_points)
    dst = np.float32(cfg.dst_points)
    return cv2.getPerspectiveTransform(src, dst)


def inverse_matrix(cfg: BevConfig) -> np.ndarray:
    if not cfg.is_valid():
        raise ValueError("invalid BEV config")
    src = np.float32(cfg.src_points)
    dst = np.float32(cfg.dst_points)
    return cv2.getPerspectiveTransform(dst, src)


def warp_frame(frame, cfg: BevConfig):
    if frame is None or not cfg.is_valid():
        return frame
    width = max(1, int(cfg.output_size[0]))
    height = max(1, int(cfg.output_size[1]))
    return cv2.warpPerspective(frame, _matrix(cfg), (width, height))


def transform_points(points: Sequence[Sequence[float]], cfg: BevConfig) -> List[List[float]]:
    pts = _normalize_points(points)
    if not pts or not cfg.is_valid():
        return pts
    arr = np.array(pts, dtype=np.float32).reshape(-1, 1, 2)
    out = cv2.perspectiveTransform(arr, _matrix(cfg)).reshape(-1, 2)
    return [[float(row[0]), float(row[1])] for row in out]


def transform_line_defs(lines: Sequence[object], cfg: BevConfig) -> List[object]:
    out: List[object] = []
    for line in lines or []:
        copied = copy.deepcopy(line)
        copied.points = transform_points(getattr(line, "points", []) or [], cfg)
        in_point = getattr(line, "in_point", None)
        if isinstance(in_point, (list, tuple)) and len(in_point) >= 2:
            pts = transform_points([[float(in_point[0]), float(in_point[1])]], cfg)
            copied.in_point = pts[0] if pts else None
        out.append(copied)
    return out


def export_bev_video(
    video_path: Path,
    output_path: Path,
    cfg: BevConfig,
    progress_cb: Optional[Callable[[int, int], None]] = None,
) -> Tuple[int, float]:
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"video open failed: {video_path}")
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 30.0)
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    width = max(1, int(cfg.output_size[0]))
    height = max(1, int(cfg.output_size[1]))
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    writer = cv2.VideoWriter(str(output_path), fourcc, fps, (width, height))
    if not writer.isOpened():
        cap.release()
        raise RuntimeError(f"video writer open failed: {output_path}")
    count = 0
    try:
        while True:
            ok, frame = cap.read()
            if not ok or frame is None:
                break
            writer.write(warp_frame(frame, cfg))
            count += 1
            if progress_cb is not None:
                try:
                    progress_cb(count, total_frames)
                except Exception:
                    pass
    finally:
        writer.release()
        cap.release()
    return count, fps
