import logging
import re
from pathlib import Path
from typing import Tuple


# Ultralytics 가 내려받을 수 있는 공식 가중치 이름(yolov8m.pt, yolo11l.pt, yolo26n.pt, yolo26n-cls.pt 등).
# yolo26n_v1.pt 처럼 직접 학습한 모델에 붙인 이름은 공식 모델로 보지 않는다(차종 이름 변환이 필요하다).
_OFFICIAL_WEIGHTS = re.compile(r"^yolo(v\d+|\d{2})[nslmx]u?(-(cls|seg|pose|obb|reid))?\.pt$")


def can_auto_download_model(model_value: str | Path) -> bool:
    name = Path(str(model_value or "")).name.strip().lower()
    return bool(_OFFICIAL_WEIGHTS.match(name))



logger = logging.getLogger(__name__)

def resolve_model_source(model_value: str | Path) -> Tuple[str, bool]:
    raw = str(model_value or "").strip()
    if not raw:
        return "", False
    model_path = Path(raw)
    if model_path.exists():
        return str(model_path), False
    if can_auto_download_model(raw):
        return model_path.name, True
    return str(model_path), False


def ensure_model_source(model_value: str | Path) -> str:
    raw = str(model_value or "").strip()
    if not raw:
        return ""
    model_path = Path(raw)
    if model_path.exists():
        return str(model_path)
    if not can_auto_download_model(raw):
        return str(model_path)
    try:
        from ultralytics.utils.downloads import attempt_download_asset
    except Exception:
        logger.debug("Suppressed error", exc_info=True)
        return str(model_path.name)
    try:
        model_path.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        logger.debug("Suppressed error", exc_info=True)
    try:
        downloaded = attempt_download_asset(str(model_path), release="latest")
        return str(downloaded)
    except Exception:
        logger.debug("Suppressed error", exc_info=True)
        return str(model_path.name)
