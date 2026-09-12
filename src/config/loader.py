import json
import os
from pathlib import Path
from typing import Any, Dict


def load_json(path: Path) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _normalize_colab_drive_path(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _normalize_colab_drive_path(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalize_colab_drive_path(v) for v in value]
    if not isinstance(value, str):
        return value

    raw = value.strip()
    if not raw:
        return value

    if os.name == "nt":
        return value
    if "/content/drive/" in raw:
        return value

    normalized = raw.replace("\\", "/")
    prefixes = (
        "G:/내 드라이브/",
        "g:/내 드라이브/",
        "G:/MyDrive/",
        "g:/MyDrive/",
        "G:/My Drive/",
        "g:/My Drive/",
    )
    for prefix in prefixes:
        if normalized.startswith(prefix):
            suffix = normalized[len(prefix) :]
            return f"/content/drive/MyDrive/{suffix}"
    return value


def load_app_config(path: Path) -> Dict[str, Any]:
    cfg = _normalize_colab_drive_path(load_json(path))
    cfg["root_dir"] = str(path.parent.resolve())
    return cfg


def load_app_config_with_state(path: Path, state_path: Path) -> Dict[str, Any]:
    """Load portable defaults and overlay local GUI/runtime preferences."""
    cfg = load_app_config(path)
    if state_path.exists():
        state = _normalize_colab_drive_path(load_json(state_path))
        if isinstance(state, dict):
            cfg.update(state)
    return cfg


def load_line_settings(path: Path) -> Dict[str, Any]:
    return load_json(path)


def save_app_config(path: Path, cfg: Dict[str, Any]) -> None:
    """Save config dict to disk without persisting the derived root_dir key."""
    data = dict(cfg)
    data.pop("root_dir", None)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def save_user_state(path: Path, state: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    save_app_config(path, state)
