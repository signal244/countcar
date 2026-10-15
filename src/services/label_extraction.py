"""여러 영상에서 학습용 이미지를 뽑고 YOLO 라벨(txt)을 만든다.

GUI(src/ui/extract_window.py)가 설정과 영상 목록을 모아 run_extraction 을 부른다.
- 영상마다 정한 간격(N초마다 1장)으로 프레임을 저장하고, 같은 프레임을 바로 모델에 넣어 라벨을 만든다.
- 파일 이름은 <교차로명>_<세션명>_NNNNNN.jpg. 같은 폴더에 다시 뽑으면 다음 번호부터 이어 붙인다.
- 실행 장치는 탐지와 같은 규칙(plan_runtime)으로 정한다(GPU 가 있으면 GPU).
"""
from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

from src.config.model_resolver import ensure_model_source
from src.config.runtime import plan_runtime, training_imgsz
from src.pipeline.image_extractor import label_class_id, next_start_index

logger = logging.getLogger(__name__)

ID_SCHEMES = ("dataset", "model")


@dataclass
class ExtractJob:
    video: Path
    junction: str
    session: str

    @property
    def prefix(self) -> str:
        parts = [re.sub(r"\s+", "_", _safe(p)) for p in (self.junction, self.session) if p.strip()]
        return "_".join(parts) or "video"


@dataclass
class ExtractSettings:
    out_dir: Path
    model_path: str = ""
    every_sec: float = 2.0            # N초마다 1장
    start_sec: float = 0.0
    end_sec: float = 0.0              # 0 이면 영상 끝까지
    max_images: int = 0               # 영상당 최대 장수(0 = 제한 없음)
    imgsz: str = "auto"               # "auto" 또는 숫자
    conf: float = 0.25
    class_ids: Optional[List[int]] = None   # 라벨링할 모델 클래스 번호(None = 사람 빼고 전부)
    id_scheme: str = "dataset"        # dataset: 학습 데이터 8클래스 번호 / model: 모델 번호 그대로
    images_only: bool = False
    runtime_mode: str = "auto"
    device_cfg: str = "auto"
    class_mapping: Dict[str, str] = field(default_factory=dict)


@dataclass
class ExtractResult:
    job: ExtractJob
    status: str                       # done | stopped | failed
    images: int = 0
    labeled: int = 0
    empty: int = 0
    dropped_boxes: int = 0
    class_counts: Counter = field(default_factory=Counter)
    message: str = ""


class StopFlags:
    """GUI 가 바꾸는 중지 요청. now: 지금 영상도 멈춤, after_current: 지금 영상까지만."""

    def __init__(self) -> None:
        self.now = False
        self.after_current = False


def _safe(name: str) -> str:
    return "".join("_" if c in '<>:"/\\|?*' else c for c in (name or "").strip()).strip().strip(".")


def frame_step(fps: float, every_sec: float) -> int:
    """N초마다 1장일 때 몇 프레임마다 저장할지(최소 1)."""
    return max(1, int(round(float(fps or 30.0) * max(0.0, float(every_sec)))))


def sample_frame_ids(total_frames: int, fps: float, every_sec: float,
                     start_sec: float = 0.0, end_sec: float = 0.0, max_images: int = 0) -> range:
    """저장할 프레임 번호들(구간·간격·최대 장수 반영)."""
    fps = float(fps or 30.0)
    first = max(0, int(round(float(start_sec) * fps)))
    last = total_frames if not end_sec or end_sec <= 0 else min(total_frames, int(round(float(end_sec) * fps)))
    ids = range(first, max(first, last), frame_step(fps, every_sec))
    if max_images and max_images > 0:
        ids = ids[:max_images]
    return ids


def load_label_model(settings: ExtractSettings, log: Callable[[str], None]):
    """라벨링 모델과 predict 인자(장치·크기)를 준비한다."""
    from ultralytics import YOLO

    plan = plan_runtime(settings.model_path, settings.runtime_mode, settings.device_cfg)
    log(f"[extract] 실행 장치: {plan.label}")
    if plan.warning:
        log(f"[extract:warn] {plan.warning}")
    source = plan.model_path if plan.openvino else ensure_model_source(plan.model_path)
    model = YOLO(str(source), task="detect")
    if plan.fixed_imgsz:
        imgsz = int(plan.fixed_imgsz)
    elif str(settings.imgsz).lower() == "auto":
        imgsz = training_imgsz(plan.model_path) or 640
    else:
        imgsz = int(settings.imgsz)
    kwargs = {"conf": float(settings.conf), "imgsz": imgsz, "device": plan.device, "verbose": False}
    if settings.class_ids:
        kwargs["classes"] = [int(c) for c in settings.class_ids]
    log(f"[extract] 라벨 모델 {Path(str(source)).name}, 이미지 크기 {imgsz}, 신뢰도 {settings.conf:.2f}, "
        f"번호 체계 {'학습 데이터 8클래스' if settings.id_scheme == 'dataset' else '모델 번호 그대로'}")
    return model, kwargs


def label_frame(model, frame, kwargs: Dict, class_mapping: Dict[str, str], scheme: str):
    """프레임 하나를 라벨 줄로. (줄 목록, 뺀 박스 수, 클래스 이름별 수)."""
    result = model(frame, **kwargs)[0]
    lines: List[str] = []
    dropped = 0
    counts: Counter = Counter()
    boxes = result.boxes
    if boxes is None:
        return lines, dropped, counts
    names = model.names
    for cls_raw, xywhn in zip(boxes.cls.tolist(), boxes.xywhn.tolist()):
        cls = label_class_id(int(cls_raw), names, class_mapping, scheme)
        if cls is None:
            dropped += 1
            continue
        x_c, y_c, w, h = xywhn
        lines.append(f"{cls} {x_c:.6f} {y_c:.6f} {w:.6f} {h:.6f}\n")
        counts[str(names.get(int(cls_raw), cls_raw))] += 1
    return lines, dropped, counts


def run_extraction(
    jobs: List[ExtractJob],
    settings: ExtractSettings,
    *,
    flags: Optional[StopFlags] = None,
    log_cb: Optional[Callable[[int, str], None]] = None,
    status_cb: Optional[Callable[[int, str, str], None]] = None,
    progress_cb: Optional[Callable[[int, int, int], None]] = None,
    model_loader: Callable = load_label_model,
    video_opener: Optional[Callable] = None,
    imwrite: Optional[Callable] = None,
) -> List[ExtractResult]:
    """영상들을 차례로 처리한다. 콜백의 첫 인자는 영상 순번(0부터, -1 은 전체)."""
    flags = flags or StopFlags()

    def log(i: int, msg: str) -> None:
        if log_cb is not None:
            log_cb(i, msg)

    def status(i: int, state: str, detail: str = "") -> None:
        if status_cb is not None:
            status_cb(i, state, detail)

    if video_opener is None or imwrite is None:
        import cv2
        video_opener = video_opener or cv2.VideoCapture
        imwrite = imwrite or cv2.imwrite

    images_dir = Path(settings.out_dir) / "images"
    labels_dir = Path(settings.out_dir) / "labels"
    images_dir.mkdir(parents=True, exist_ok=True)
    if not settings.images_only:
        labels_dir.mkdir(parents=True, exist_ok=True)

    model, kwargs = (None, {})
    if not settings.images_only:
        model, kwargs = model_loader(settings, lambda m: log(-1, m))

    results: List[ExtractResult] = []
    for i, job in enumerate(jobs):
        if flags.now or flags.after_current:
            break
        res = ExtractResult(job, "done")
        status(i, "running")
        cap = video_opener(str(job.video))
        try:
            if not cap.isOpened():
                raise RuntimeError(f"영상을 열 수 없습니다: {job.video}")
            import cv2
            fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0) or 30.0
            total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
            ids = sample_frame_ids(total, fps, settings.every_sec, settings.start_sec, settings.end_sec,
                                   settings.max_images)
            wanted = set(ids)
            planned = len(ids)
            idx = next_start_index(images_dir, job.prefix)
            log(i, f"[extract] {job.video.name}: {planned:,}장 예정 ({settings.every_sec:g}초마다, "
                   f"시작 번호 {idx:06d}, 이름 {job.prefix}_NNNNNN.jpg)")
            if planned and ids[0] > 0:
                cap.set(cv2.CAP_PROP_POS_FRAMES, float(ids[0]))
            frame_no = ids[0] if planned else total
            last_id = ids[-1] if planned else -1
            while frame_no <= last_id:
                if flags.now:
                    res.status = "stopped"
                    break
                # 저장하지 않을 프레임은 디코딩만 건너뛴다(grab 이 read 보다 빠르다).
                if frame_no not in wanted:
                    if not cap.grab():
                        break
                    frame_no += 1
                    continue
                ok, frame = cap.read()
                if not ok or frame is None:
                    break
                name = f"{job.prefix}_{idx:06d}"
                if not imwrite(str(images_dir / f"{name}.jpg"), frame):
                    log(i, f"[extract:warn] 이미지 저장 실패: {name}.jpg")
                else:
                    res.images += 1
                    idx += 1
                    if model is not None:
                        lines, dropped, counts = label_frame(model, frame, kwargs, settings.class_mapping,
                                                             settings.id_scheme)
                        res.dropped_boxes += dropped
                        res.class_counts.update(counts)
                        if lines:
                            (labels_dir / f"{name}.txt").write_text("".join(lines), encoding="utf-8")
                            res.labeled += 1
                        else:
                            res.empty += 1
                    if progress_cb is not None:
                        progress_cb(i, res.images, planned)
                frame_no += 1
        except Exception as exc:  # noqa: BLE001
            logger.debug("추출 실패: %s", job.video, exc_info=True)
            res.status = "failed"
            res.message = str(exc)
            log(i, f"[extract:error] {exc}")
        finally:
            cap.release()
        summary = f"이미지 {res.images:,}장, 라벨 {res.labeled:,}장, 빈 이미지 {res.empty:,}장"
        if res.dropped_boxes:
            summary += f", 번호를 바꿀 수 없어 뺀 박스 {res.dropped_boxes:,}개"
        if res.class_counts:
            summary += " | " + ", ".join(f"{k} {v:,}" for k, v in res.class_counts.most_common())
        log(i, f"[extract] {job.video.name}: {summary}")
        status(i, res.status, f"{res.images:,}장")
        results.append(res)
    return results
