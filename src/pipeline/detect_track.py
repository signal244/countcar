import math
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple

import cv2
import numpy as np
from ultralytics import YOLO

from src.db.writer import TrackTrajDBWriter


@dataclass
class TrackState:
    start_frame: int
    entry: Tuple[float, float]
    last_seen: int
    last_center: Tuple[float, float]
    length: int = 1

    def update(self, center: Tuple[float, float], frame_id: int) -> None:
        self.last_center = center
        self.last_seen = frame_id
        self.length += 1


def point_in_roi(point: Tuple[float, float], roi: List[List[float]]) -> bool:
    """Return True if the point is inside ROI polygon or ROI is empty."""
    if not roi or len(roi) < 3:
        return True
    polygon = np.array(roi, dtype=np.int32)
    return cv2.pointPolygonTest(polygon, point, measureDist=False) >= 0


class DetectionTracker:
    """Run YOLO detection+tracking and log to SQLite."""

    def __init__(
        self,
        model_path: Path,
        class_mapping: Dict[str, str],
        camera_id: str,
        roi: List[List[float]],
        line_set_id: Optional[str],
        confidence: float = 0.25,
        device: str = "cpu",
        tracker_config: Optional[Path] = None,
        allowed_classes: Optional[List[int]] = None,
        resize: Optional[Tuple[int, int]] = None,
        yolo_imgsz: Optional[int] = None,
        yolo_rect: bool = True,
        target_fps: Optional[float] = None,
        max_idle_frames: int = 30,
        flush_interval_minutes: Optional[int] = 15,
    ):
        self.model = YOLO(str(model_path))
        self.class_mapping = class_mapping
        self.camera_id = camera_id
        self.roi = roi
        self.line_set_id = line_set_id
        self.confidence = confidence
        self.device = device
        self.tracker_config = str(tracker_config) if tracker_config else None
        self.allowed_classes = allowed_classes
        self.resize = resize  # legacy/compat (예전 설정값)
        if yolo_imgsz is None:
            if resize and resize[0] > 0 and resize[1] > 0:
                yolo_imgsz = int(max(resize[0], resize[1]))
            else:
                yolo_imgsz = 640
        self.yolo_imgsz = int(yolo_imgsz)
        self.yolo_rect = bool(yolo_rect)
        self.target_fps = target_fps
        self.max_idle_frames = max_idle_frames
        self.flush_interval_ms = (
            int(flush_interval_minutes * 60_000) if flush_interval_minutes and flush_interval_minutes > 0 else None
        )

    def run(
        self,
        video_path: Path,
        db_writer: TrackTrajDBWriter,
        session_id: Optional[str] = None,
        progress_cb: Optional[Callable[[str], None]] = None,
        should_stop_cb: Optional[Callable[[], bool]] = None,
    ) -> None:
        session = session_id or uuid.uuid4().hex
        fps = self._probe_fps(video_path)
        frame_duration_ms = 1000.0 / fps if fps else None
        # 설정된 주기로 주기적 flush (없으면 비활성화)
        flush_interval_ms = self.flush_interval_ms if frame_duration_ms and self.flush_interval_ms else None
        next_flush_ms = flush_interval_ms
        active: Dict[int, TrackState] = {}
        traj_buf: Dict[int, List[List[float]]] = {}
        meta_buf: Dict[int, Dict] = {}

        if progress_cb:
            progress_cb("[start] tracking...")

        # 원본 FPS -> target_fps로 샘플링(프레임 스킵)
        vid_stride = 1
        if self.target_fps and fps and fps > 0 and self.target_fps > 0:
            ratio = float(fps) / float(self.target_fps)
            if ratio > 1.0:
                vid_stride = max(1, int(round(ratio)))
        if progress_cb and self.target_fps:
            progress_cb(f"[info] src_fps={fps:.2f} target_fps={self.target_fps:.2f} vid_stride={vid_stride}")

        # A안: 원본 비디오를 그대로 입력하고, 결과 좌표를 "원본 프레임 좌표계"로 저장한다.
        # - imgsz는 "추론 시 내부 입력 크기"만 결정하며, Ultralytics는 결과를 원본 프레임 좌표로 복원한다.
        source_path = str(video_path)
        imgsz = int(self.yolo_imgsz)

        # Ultralytics 로그(프레임별 "video 1/1 ...")를 최대한 억제
        try:
            import logging

            from ultralytics.utils import LOGGER  # type: ignore

            LOGGER.setLevel(logging.ERROR)
        except Exception:
            pass

        try:
            stream = self.model.track(
                source=source_path,
                stream=True,
                tracker=self.tracker_config,
                conf=self.confidence,
                device=self.device,
                imgsz=imgsz,
                rect=self.yolo_rect,
                classes=self.allowed_classes,
                vid_stride=vid_stride,
                verbose=False,
            )
        except TypeError:
            stream = self.model.track(
                source=source_path,
                stream=True,
                tracker=self.tracker_config,
                conf=self.confidence,
                device=self.device,
                imgsz=imgsz,
                classes=self.allowed_classes,
                vid_stride=vid_stride,
                verbose=False,
            )

        sample_idx = 0
        debug_frames_left = 3
        for result in stream:
            if should_stop_cb is not None:
                try:
                    if should_stop_cb():
                        if progress_cb:
                            progress_cb("[stop] interruption requested")
                        break
                except Exception:
                    pass
            src_frame_id = sample_idx * vid_stride
            if progress_cb and sample_idx % 100 == 0:
                progress_cb(f"[progress] frame {src_frame_id}")
            timestamp_ms = int(src_frame_id * frame_duration_ms) if frame_duration_ms else None
            boxes = result.boxes
            if boxes is None or boxes.id is None:
                sample_idx += 1
                continue

            seen_in_frame = set()
            # 원본 프레임 크기(좌표계 기준)
            orig_h = orig_w = None
            try:
                if getattr(result, "orig_img", None) is not None:
                    orig_h, orig_w = result.orig_img.shape[:2]
                elif getattr(result, "orig_shape", None) is not None:
                    orig_h, orig_w = result.orig_shape[:2]
            except Exception:
                orig_h = orig_w = None

            xyxy = None
            if orig_w and orig_h:
                try:
                    # xyxyn: [0..1] 정규화 좌표 (원본 프레임 기준). => 픽셀 좌표로 복원해서 사용.
                    xyxyn = boxes.xyxyn.cpu().numpy()
                    xyxy = xyxyn.copy()
                    xyxy[:, [0, 2]] *= float(orig_w)
                    xyxy[:, [1, 3]] *= float(orig_h)
                except Exception:
                    xyxy = None
            if xyxy is None:
                xyxy = boxes.xyxy.cpu().numpy()

            track_ids = boxes.id.cpu().numpy().astype(int)
            classes = boxes.cls.cpu().numpy().astype(int) if boxes.cls is not None else np.full_like(track_ids, -1)
            confs = boxes.conf.cpu().numpy() if boxes.conf is not None else np.zeros_like(track_ids, dtype=float)

            if progress_cb and debug_frames_left > 0 and orig_w and orig_h:
                try:
                    xmax = float(np.max(xyxy[:, 2])) if len(xyxy) else 0.0
                    ymax = float(np.max(xyxy[:, 3])) if len(xyxy) else 0.0
                    progress_cb(
                        f"[debug] orig={orig_w}x{orig_h} boxes_max=({xmax:.1f},{ymax:.1f}) imgsz={imgsz} rect={self.yolo_rect} source={Path(source_path).name}"
                    )
                except Exception:
                    progress_cb(f"[debug] orig={orig_w}x{orig_h} imgsz={imgsz} rect={self.yolo_rect} source={Path(source_path).name}")
                debug_frames_left -= 1

            for idx, track_id in enumerate(track_ids):
                x1, y1, x2, y2 = xyxy[idx].tolist()
                center_x = (x1 + x2) / 2.0
                center_y = (y1 + y2) / 2.0

                if not point_in_roi((center_x, center_y), self.roi):
                    continue

                cls_id = int(classes[idx]) if idx < len(classes) else -1
                class_name = self.model.names.get(cls_id, str(cls_id))
                vehicle_type = self.class_mapping.get(class_name, class_name)
                conf_val = float(confs[idx]) if idx < len(confs) else 0.0

                state = active.get(track_id)
                status = "ongoing"
                if state is None:
                    state = TrackState(
                        start_frame=sample_idx,
                        entry=(center_x, center_y),
                        last_seen=sample_idx,
                        last_center=(center_x, center_y),
                        length=1,
                    )
                    active[track_id] = state
                    status = "start"
                else:
                    state.update((center_x, center_y), sample_idx)

                direction_hint = None
                if state.entry != (center_x, center_y):
                    dx = center_x - state.entry[0]
                    dy = center_y - state.entry[1]
                    direction_hint = math.degrees(math.atan2(dy, dx))

                # 트랙 단위 저장: 프레임별 포인트는 버퍼에 누적 후, end 시 1row로 저장
                traj_buf.setdefault(track_id, []).append([float(src_frame_id), float(timestamp_ms or 0), float(center_x), float(center_y)])
                if track_id not in meta_buf:
                    meta_buf[track_id] = {
                        "session_id": session,
                        "camera_id": self.camera_id,
                        "track_id": str(track_id),
                        "class_id": cls_id,
                        "class_name": class_name,
                        "vehicle_type": vehicle_type,
                        "start_frame": int(src_frame_id),
                        "start_ts_ms": int(timestamp_ms or 0),
                        "entry_x": float(state.entry[0]),
                        "entry_y": float(state.entry[1]),
                        "confidence": float(conf_val),
                    }
                else:
                    # 대표 confidence는 간단히 max로 갱신
                    try:
                        meta_buf[track_id]["confidence"] = float(max(meta_buf[track_id].get("confidence") or 0.0, conf_val))
                    except Exception:
                        pass
                # 항상 최신 종료 정보 갱신
                meta_buf[track_id]["end_frame"] = int(src_frame_id)
                meta_buf[track_id]["end_ts_ms"] = int(timestamp_ms or 0)
                meta_buf[track_id]["exit_x"] = float(center_x)
                meta_buf[track_id]["exit_y"] = float(center_y)
                meta_buf[track_id]["track_len"] = int(state.length)
                meta_buf[track_id]["direction_hint"] = direction_hint
                seen_in_frame.add(track_id)

            # 15분 경계마다 버퍼를 비워 DB에 기록
            if (
                flush_interval_ms is not None
                and timestamp_ms is not None
                and next_flush_ms is not None
                and timestamp_ms >= next_flush_ms
            ):
                db_writer.flush()
                if progress_cb:
                    progress_cb(f"[flush] frame={src_frame_id} ts_ms={timestamp_ms}")
                while timestamp_ms >= next_flush_ms:
                    next_flush_ms += flush_interval_ms

            self._finalize_inactive(active, seen_in_frame, sample_idx, db_writer, session, frame_duration_ms, vid_stride, traj_buf, meta_buf)
            sample_idx += 1

        # finalize remaining tracks as ended
        for track_id, state in list(active.items()):
            self._write_end_record(track_id, state, sample_idx, db_writer, session, frame_duration_ms, vid_stride, traj_buf, meta_buf)
            active.pop(track_id, None)
        if progress_cb:
            progress_cb(f"[done] total sampled frames: {sample_idx} (vid_stride={vid_stride})")

    def _probe_fps(self, video_path: Path) -> Optional[float]:
        cap = cv2.VideoCapture(str(video_path))
        fps = cap.get(cv2.CAP_PROP_FPS)
        cap.release()
        if fps and fps > 0:
            return fps
        return self.target_fps

    def _finalize_inactive(
        self,
        active: Dict[int, TrackState],
        seen_in_frame: Iterable[int],
        frame_id: int,
        db_writer: TrackTrajDBWriter,
        session_id: str,
        frame_duration_ms: Optional[float],
        vid_stride: int = 1,
        traj_buf: Optional[Dict[int, List[List[float]]]] = None,
        meta_buf: Optional[Dict[int, Dict]] = None,
    ) -> None:
        for track_id, state in list(active.items()):
            if track_id in seen_in_frame:
                continue
            if frame_id - state.last_seen >= self.max_idle_frames:
                self._write_end_record(track_id, state, frame_id, db_writer, session_id, frame_duration_ms, vid_stride, traj_buf, meta_buf)
                active.pop(track_id, None)

    def _write_end_record(
        self,
        track_id: int,
        state: TrackState,
        frame_id: int,
        db_writer: TrackTrajDBWriter,
        session_id: str,
        frame_duration_ms: Optional[float],
        vid_stride: int = 1,
        traj_buf: Optional[Dict[int, List[List[float]]]] = None,
        meta_buf: Optional[Dict[int, Dict]] = None,
    ) -> None:
        if traj_buf is None or meta_buf is None:
            return
        meta = meta_buf.get(track_id) or {
            "session_id": session_id,
            "camera_id": self.camera_id,
            "track_id": str(track_id),
        }
        # 보정: end 정보가 비어있으면 현재 state로 채움
        src_frame_id = frame_id * max(1, int(vid_stride))
        timestamp_ms = int(src_frame_id * frame_duration_ms) if frame_duration_ms else 0
        meta.setdefault("start_frame", int(src_frame_id))
        meta.setdefault("start_ts_ms", int(timestamp_ms))
        meta["end_frame"] = int(meta.get("end_frame") or src_frame_id)
        meta["end_ts_ms"] = int(meta.get("end_ts_ms") or timestamp_ms)
        meta["track_len"] = int(meta.get("track_len") or state.length)
        meta["entry_x"] = float(meta.get("entry_x") or state.entry[0])
        meta["entry_y"] = float(meta.get("entry_y") or state.entry[1])
        meta["exit_x"] = float(meta.get("exit_x") or state.last_center[0])
        meta["exit_y"] = float(meta.get("exit_y") or state.last_center[1])
        meta["direction_hint"] = meta.get("direction_hint")
        points = traj_buf.get(track_id, [])
        if points:
            # sort by frame_id
            try:
                points = sorted(points, key=lambda r: r[0])
            except Exception:
                pass
        db_writer.add_track(
            {
                "session_id": meta.get("session_id") or session_id,
                "camera_id": meta.get("camera_id") or self.camera_id,
                "track_id": meta.get("track_id") or str(track_id),
                "class_id": meta.get("class_id"),
                "class_name": meta.get("class_name"),
                "vehicle_type": meta.get("vehicle_type"),
                "start_frame": meta.get("start_frame"),
                "end_frame": meta.get("end_frame"),
                "start_ts_ms": meta.get("start_ts_ms"),
                "end_ts_ms": meta.get("end_ts_ms"),
                "track_len": meta.get("track_len"),
                "entry_x": meta.get("entry_x"),
                "entry_y": meta.get("entry_y"),
                "exit_x": meta.get("exit_x"),
                "exit_y": meta.get("exit_y"),
                "direction_hint": meta.get("direction_hint"),
                "extra": None,
            },
            points,
        )
        traj_buf.pop(track_id, None)
        meta_buf.pop(track_id, None)
