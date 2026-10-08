"""PC 사양에 맞춰 탐지 모델을 '어떻게 돌릴지' 정한다.

사용자는 학습한 .pt 모델만 고른다. GPU(CUDA)가 있으면 .pt 를 그대로 GPU 로,
없으면 같은 폴더의 OpenVINO 변환 모델(<이름>_int8_openvino_model)을 CPU 로 돌린다.
변환 모델은 scripts/export_openvino.py 가 이 이름 규칙으로 만든다.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import Optional

from src.config.device import resolve_device

logger = logging.getLogger(__name__)

OPENVINO_SUFFIXES = ("_int8_openvino_model", "_openvino_model")
RUNTIME_MODES = ("auto", "gpu", "cpu")


@dataclass(frozen=True)
class RuntimePlan:
    mode: str                 # "gpu" | "cpu"
    device: str               # ultralytics device 문자열
    model_path: str           # 실제로 불러올 모델(.pt 또는 OpenVINO 폴더)
    openvino: bool            # OpenVINO 변환 모델을 쓰는지
    fixed_imgsz: Optional[int]  # 변환 모델은 입력 크기가 고정 — 긴 변
    warning: str = ""

    @property
    def label(self) -> str:
        if self.mode == "gpu":
            return f"GPU ({self.device}) · {Path(self.model_path).name}"
        if self.openvino:
            kind = "INT8" if self.model_path.rstrip("/\\").endswith("_int8_openvino_model") else "FP32"
            return f"CPU (OpenVINO {kind}) · {Path(self.model_path).name}"
        return f"CPU (PyTorch, 느림) · {Path(self.model_path).name}"


def is_openvino_model(model_path: str | Path) -> bool:
    return Path(str(model_path)).name.rstrip("/\\").endswith("_openvino_model")


def _has_openvino_files(folder: Path) -> bool:
    # 빈 폴더나 변환이 중간에 끊긴 폴더를 변환 모델로 오인하지 않는다.
    return folder.is_dir() and any(folder.glob("*.xml"))


def find_openvino_model(model_path: str | Path) -> Optional[Path]:
    """선택한 모델과 짝이 되는 OpenVINO 변환 모델 폴더. INT8 을 FP32 보다 먼저 찾는다."""
    path = Path(str(model_path))
    if is_openvino_model(path):
        return path if _has_openvino_files(path) else None
    for suffix in OPENVINO_SUFFIXES:
        candidate = path.with_name(path.stem + suffix)
        if _has_openvino_files(candidate):
            return candidate
    return None


def base_model_path(model_path: str | Path) -> str:
    """OpenVINO 폴더가 선택돼 있으면 원본 .pt 경로로 되돌린다(Colab 은 항상 .pt 를 쓴다)."""
    path = Path(str(model_path))
    if not is_openvino_model(path):
        return str(model_path)
    name = path.name.rstrip("/\\")
    for suffix in OPENVINO_SUFFIXES:
        if name.endswith(suffix):
            return str(path.with_name(name[: -len(suffix)] + ".pt"))
    return str(model_path)


def exported_imgsz(model_dir: str | Path) -> Optional[int]:
    """변환 모델 metadata.yaml 의 입력 크기(긴 변)."""
    meta = Path(str(model_dir)) / "metadata.yaml"
    try:
        import yaml

        value = (yaml.safe_load(meta.read_text(encoding="utf-8")) or {}).get("imgsz")
    except Exception:
        logger.debug("metadata.yaml 읽기 실패: %s", meta, exc_info=True)
        return None
    if isinstance(value, (list, tuple)) and value:
        return int(max(value))
    return int(value) if value else None


def training_imgsz(model_path: str | Path) -> Optional[int]:
    """.pt 안에 저장된 학습 imgsz. 읽지 못하면 None."""
    try:
        from ultralytics import YOLO

        value = (getattr(YOLO(str(model_path)), "ckpt", None) or {}).get("train_args", {}).get("imgsz")
    except Exception:
        logger.debug("학습 imgsz 읽기 실패: %s", model_path, exc_info=True)
        return None
    if isinstance(value, (list, tuple)) and value:
        return int(max(value))
    return int(value) if value else None


def needs_native_features(tracker_config: str | Path) -> bool:
    """탐지 모델 내부 특징으로 ReID 를 하는 설정인지(model: auto). 변환 모델에서는 쓸 수 없다."""
    try:
        import yaml

        data = yaml.safe_load(Path(str(tracker_config)).read_text(encoding="utf-8")) or {}
    except Exception:
        logger.debug("추적 설정 읽기 실패: %s", tracker_config, exc_info=True)
        return False
    return bool(data.get("with_reid")) and str(data.get("model", "auto")).strip().lower() == "auto"


def effective_tracker(plan: RuntimePlan, tracker_config: str, cpu_tracker_config: str) -> str:
    """변환 모델로 돌 때 탐지 모델 특징 ReID 설정이면 CPU 용(전용 ReID 모델) 설정으로 바꾼다."""
    if plan.openvino and cpu_tracker_config and needs_native_features(tracker_config):
        return cpu_tracker_config
    return tracker_config


@lru_cache(maxsize=None)
def _auto_device(device_cfg: str) -> str:
    return resolve_device(device_cfg)


# 자동 모드에서 이보다 메모리가 작은 GPU 는 CPU(OpenVINO)보다 느리거나 메모리가 모자란다(예: GTX 750 Ti 1GB).
MIN_AUTO_GPU_MEMORY_GB = 4.0


@lru_cache(maxsize=None)
def _gpu_info() -> tuple[str, float]:
    try:
        import torch

        props = torch.cuda.get_device_properties(0)
        return props.name, props.total_memory / 1024**3
    except Exception:
        logger.debug("GPU 정보 읽기 실패", exc_info=True)
        return "", 0.0


def plan_runtime(model_path: str | Path, runtime_mode: str = "auto", device_cfg: str = "auto") -> RuntimePlan:
    mode = str(runtime_mode or "auto").strip().lower()
    if mode not in RUNTIME_MODES:
        mode = "auto"
    device_cfg = str(device_cfg or "auto").strip().lower()
    model_text = str(model_path)

    if mode == "auto":
        device = _auto_device(device_cfg)
        mode = "cpu" if device == "cpu" else "gpu"
        if device.startswith(("cuda", "0")):
            name, memory_gb = _gpu_info()
            if name and memory_gb < MIN_AUTO_GPU_MEMORY_GB:
                plan = plan_runtime(model_path, "cpu", device_cfg)
                note = (f"GPU({name}, {memory_gb:.0f}GB)는 메모리가 작아 CPU 로 실행합니다. "
                        "GPU 를 쓰려면 실행 모드를 GPU 로 고르세요.")
                return replace(plan, warning=f"{note} {plan.warning}".strip())

    if mode == "gpu":
        device = device_cfg if device_cfg not in ("auto", "cpu") else _auto_device("auto")
        if device == "cpu":
            plan = plan_runtime(model_path, "cpu", device_cfg)
            return replace(plan, warning=f"GPU 를 찾지 못해 CPU 로 실행합니다. {plan.warning}".strip())
        if is_openvino_model(model_text) and Path(base_model_path(model_text)).exists():
            model_text = base_model_path(model_text)
        return RuntimePlan("gpu", device, model_text, False, None)

    openvino_dir = find_openvino_model(model_text)
    if openvino_dir is not None:
        return RuntimePlan("cpu", "cpu", str(openvino_dir), True, exported_imgsz(openvino_dir))
    return RuntimePlan(
        "cpu", "cpu", model_text, False, None,
        f"OpenVINO 변환 모델이 없어 PyTorch 로 CPU 실행합니다(느림). "
        f"scripts/export_openvino.py --model {model_text} 로 변환하세요.",
    )
