from pathlib import Path
from typing import Tuple


def can_auto_download_model(model_value: str | Path) -> bool:
    name = Path(str(model_value or "")).name.strip().lower()
    return bool(name) and name.startswith("yolo") and name.endswith(".pt")


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
        return str(model_path.name)
    try:
        model_path.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    try:
        downloaded = attempt_download_asset(str(model_path), release="latest")
        return str(downloaded)
    except Exception:
        return str(model_path.name)
