import json
from pathlib import Path
from typing import Any, Dict


def load_json(path: Path) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_app_config(path: Path) -> Dict[str, Any]:
    cfg = load_json(path)
    cfg["root_dir"] = str(path.parent.resolve())
    return cfg


def load_line_settings(path: Path) -> Dict[str, Any]:
    return load_json(path)


def save_app_config(path: Path, cfg: Dict[str, Any]) -> None:
    """Save config dict to disk (root_dir 키는 저장하지 않음)."""
    data = dict(cfg)
    data.pop("root_dir", None)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
