"""현재 GUI 설정을 Colab에서 그대로 실행할 수 있는 형태로 내보낸다.

- build_colab_config: 윈도우 경로를 /content/drive/MyDrive/... 로 바꾼 설정 dict
- build_colab_cell: 그 설정 파일을 읽어 파이프라인을 실행하는 짧은 셀 코드

설정 본문만 내보내고 실행 코드는 짧게 유지한다. 노트북마다 파이프라인 코드를
복제하면 로컬과 Colab 로직이 갈라지기 때문이다.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

COLAB_DRIVE_ROOT = "/content/drive/MyDrive"

# 구글드라이브가 로컬에 마운트될 때 쓰이는 접두사들
_DRIVE_PREFIXES = (
    "내 드라이브/",
    "MyDrive/",
    "My Drive/",
)

# 값이 경로인 설정 키. 이 키들만 Colab 경로로 변환한다.
PATH_CONFIG_KEYS = (
    "model_path",
    "db_path",
    "tracker_config",
    "line_settings_path",
    "class_mapping_path",
    "count_db_path",
    "count_db_dir",
    "count_lines_path",
    "count_output_dir",
)


def to_colab_path(value: str | Path, project_root: str | Path) -> str:
    """윈도우 경로나 프로젝트 상대경로를 Colab 드라이브 경로로 바꾼다.

    loader._normalize_colab_drive_path 와 같은 규칙이지만, 그쪽은 윈도우에서
    실행될 때 변환하지 않는다(로컬 실행을 깨뜨리지 않기 위해). 내보내기는
    윈도우에서 실행되면서도 변환해야 하므로 여기에 따로 둔다.
    """
    raw = str(value or "").strip()
    if not raw:
        return ""
    if raw.startswith("/content/"):
        return raw

    normalized = raw.replace("\\", "/")

    # 절대경로가 아니면 프로젝트 루트 기준으로 붙인다.
    is_absolute = (len(normalized) > 1 and normalized[1] == ":") or normalized.startswith("/")
    if not is_absolute:
        normalized = f"{str(project_root).replace(chr(92), '/').rstrip('/')}/{normalized.lstrip('/')}"

    # 드라이브 문자를 떼고 "내 드라이브/" 이후만 남긴다.
    if len(normalized) > 1 and normalized[1] == ":":
        normalized = normalized[2:]
    normalized = normalized.lstrip("/")

    for prefix in _DRIVE_PREFIXES:
        lowered = normalized.lower()
        if lowered.startswith(prefix.lower()):
            return f"{COLAB_DRIVE_ROOT}/{normalized[len(prefix):]}"

    # 드라이브 밖의 경로는 Colab에서 접근할 수 없다. 원본을 그대로 돌려주어
    # 변환되지 않았다는 사실이 눈에 띄게 한다(is_colab_path 로 검사할 수 있다).
    return raw


def is_colab_path(value: str) -> bool:
    """Colab에서 실제로 접근 가능한 경로인지 확인한다."""
    text = str(value or "").strip()
    return text.startswith("/content/")


def unconvertible_paths(cfg: Dict[str, object]) -> Dict[str, str]:
    """변환에 실패해 Colab에서 못 읽는 경로들을 돌려준다."""
    bad: Dict[str, str] = {}
    for key in PATH_CONFIG_KEYS:
        value = str(cfg.get(key) or "").strip()
        if value and not is_colab_path(value):
            bad[key] = value
    return bad


def build_colab_config(
    cfg: Dict[str, object],
    project_root: str | Path,
    *,
    allowed_classes: Optional[List[int]] = None,
    count_class_mapping: Optional[Dict[str, str]] = None,
    count_class_columns: Optional[List[str]] = None,
) -> Dict[str, object]:
    """GUI 설정을 Colab용 설정 dict로 변환한다."""
    exported: Dict[str, object] = dict(cfg)
    exported.pop("root_dir", None)

    for key in PATH_CONFIG_KEYS:
        if key in exported and exported[key]:
            exported[key] = to_colab_path(str(exported[key]), project_root)

    if allowed_classes is not None:
        exported["allowed_classes"] = sorted(int(c) for c in allowed_classes)
    if count_class_mapping is not None:
        exported["count_class_mapping"] = dict(count_class_mapping)
    if count_class_columns is not None:
        exported["count_class_columns"] = list(count_class_columns)

    return exported


def build_colab_cell(
    project_root: str | Path,
    config_path: str | Path,
    video_path: str | Path,
    line_settings_path: str | Path,
    session_id: str = "",
    *,
    interval_minutes: int = 15,
) -> str:
    """설정 파일을 읽어 탐지를 실행하는 Colab 셀 코드를 만든다."""
    project = to_colab_path(project_root, project_root)
    config = to_colab_path(config_path, project_root)
    video = to_colab_path(video_path, project_root) if video_path else ""
    lines = to_colab_path(line_settings_path, project_root) if line_settings_path else ""

    return f'''# ── Count Car : Colab 실행 셀 (GUI에서 자동 생성) ──────────────────────
# 설정은 아래 CONFIG 파일에 들어 있습니다. 값을 바꾸려면 GUI에서 다시 생성하세요.
import os, subprocess, sys
from google.colab import drive

drive.mount("/content/drive", force_remount=True)

PROJECT = r"{project}"
CONFIG  = r"{config}"
VIDEO   = r"{video}"
LINES   = r"{lines}"
SESSION = r"{session_id}"

os.chdir(PROJECT)
subprocess.run(
    [sys.executable, "-m", "pip", "install", "-q", "-r", "colab/requirements_colab.txt"],
    check=True,
)

cmd = [sys.executable, "-m", "src.pipeline.detect_to_db",
       "--video", VIDEO, "--config", CONFIG]
if LINES:
    cmd += ["--line-settings", LINES]
if SESSION:
    cmd += ["--session-id", SESSION]

proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
for line in proc.stdout:
    print(line, end="")
if proc.wait() != 0:
    raise RuntimeError("탐지 실패")
print("탐지 완료")


# ── 이어서 카운팅까지 하려면 아래 주석을 해제하세요 ──────────────────
# import json
# from pathlib import Path
# from src.pipeline.count_tracks import run_count
#
# cfg = json.loads(Path(CONFIG).read_text(encoding="utf-8"))
# out, counts = run_count(
#     db_path=Path(cfg["count_db_path"]),
#     lines_path=Path(cfg["count_lines_path"]),
#     interval_min={interval_minutes},
#     out_xlsx=Path(cfg["count_output_dir"]) / "counts.xlsx",
#     session_id=SESSION or None,
#     mode="turn",
#     log_cb=print,
#     class_mapping=cfg.get("count_class_mapping"),
#     class_columns=cfg.get("count_class_columns"),
# )
# print("카운팅 완료:", out)
'''
