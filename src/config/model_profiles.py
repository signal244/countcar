"""모델별 클래스 목록과 차종 설정(프로파일)을 관리한다.

- 모델 파일에서 클래스 목록을 읽어오고, 파일 지문(크기/수정시각) 기준으로 캐시한다.
- 모델마다 다른 "탐지 대상 차종"과 "엑셀 집계 열 매핑"을 프로파일로 저장한다.

기존에 count_tracks._write_excel 안에 하드코딩되어 있던 매핑을 여기로 옮겨,
알려진 모델 계열은 기본값으로 자동 제안하고 사용자가 바꾸면 프로파일에 남긴다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

MODEL_SUFFIXES = (".pt", ".onnx", ".engine")
PROFILE_SCHEMA_VERSION = 1

# 커스텀 학습 모델(best.pt 계열): 8클래스 -> 엑셀 6열
CUSTOM_EXCEL_MAPPING: Dict[str, str] = {
    "passenger_car": "승용차",
    "small_bus": "소형버스",
    "large_bus": "대형버스",
    "small_truck": "소형화물",
    "medium_truck": "중형화물",
    "etc": "중형화물",
    "large_truck": "대형화물",
}
CUSTOM_EXCEL_COLUMNS: List[str] = ["승용차", "소형버스", "대형버스", "소형화물", "중형화물", "대형화물"]

# 공식 YOLO(COCO) 계열: car / bus / truck 3분류를 그대로 반영한다.
COCO_EXCEL_MAPPING: Dict[str, str] = {
    "car": "승용차",
    "bus": "버스",
    "truck": "화물",
}
COCO_EXCEL_COLUMNS: List[str] = ["승용차", "버스", "화물"]

# 집계 기본 열에는 없지만 탐지는 해 두는 차종.
# 탐지에서 빼면 DB에 남지 않아 되돌릴 수 없으므로, 저장은 해 두고
# 집계에 넣을지 여부만 사용자가 정하게 한다.
OPTIONAL_EXCEL_MAPPING: Dict[str, str] = {
    "motorcycle": "이륜차",
    "bicycle": "자전거",
}
OPTIONAL_EXCEL_COLUMNS: List[str] = ["이륜차", "자전거"]

# 이미 한글로 저장된 DB(과거 결과)도 그대로 통과시키기 위한 항등 매핑
KOREAN_PASSTHROUGH: Dict[str, str] = {
    name: name for name in (*CUSTOM_EXCEL_COLUMNS, *COCO_EXCEL_COLUMNS, *OPTIONAL_EXCEL_COLUMNS)
}

# 차종으로 취급하지 않는 클래스 (탐지 기본 선택에서 제외)
NON_VEHICLE_CLASS_NAMES = {
    "person",
    "airplane",
    "train",
    "boat",
    "traffic light",
    "fire hydrant",
    "stop sign",
    "parking meter",
    "bench",
    "ignore",
}


@dataclass
class ModelProfile:
    """한 모델 파일에 대한 클래스 목록과 사용자 차종 설정."""

    key: str
    path: str = ""
    fingerprint: str = ""
    classes: Dict[int, str] = field(default_factory=dict)
    detect_class_ids: List[int] = field(default_factory=list)
    excel_mapping: Dict[str, str] = field(default_factory=dict)
    excel_columns: List[str] = field(default_factory=list)

    @property
    def has_classes(self) -> bool:
        return bool(self.classes)

    def class_names(self) -> List[str]:
        return [self.classes[cid] for cid in sorted(self.classes)]

    def detect_class_names(self) -> List[str]:
        return [self.classes[cid] for cid in sorted(self.detect_class_ids) if cid in self.classes]

    def to_json(self) -> Dict[str, object]:
        return {
            "path": self.path,
            "fingerprint": self.fingerprint,
            "classes": {str(k): v for k, v in sorted(self.classes.items())},
            "detect_class_ids": sorted(int(c) for c in self.detect_class_ids),
            "excel_mapping": dict(self.excel_mapping),
            "excel_columns": list(self.excel_columns),
        }

    @classmethod
    def from_json(cls, key: str, data: Dict[str, object]) -> "ModelProfile":
        raw_classes = data.get("classes") or {}
        classes: Dict[int, str] = {}
        if isinstance(raw_classes, dict):
            for k, v in raw_classes.items():
                try:
                    classes[int(k)] = str(v)
                except (TypeError, ValueError):
                    continue
        raw_ids = data.get("detect_class_ids") or []
        detect_ids: List[int] = []
        if isinstance(raw_ids, list):
            for v in raw_ids:
                try:
                    detect_ids.append(int(v))
                except (TypeError, ValueError):
                    continue
        raw_mapping = data.get("excel_mapping") or {}
        mapping = {str(k): str(v) for k, v in raw_mapping.items()} if isinstance(raw_mapping, dict) else {}
        raw_columns = data.get("excel_columns") or []
        columns = [str(c) for c in raw_columns] if isinstance(raw_columns, list) else []
        return cls(
            key=key,
            path=str(data.get("path") or ""),
            fingerprint=str(data.get("fingerprint") or ""),
            classes=classes,
            detect_class_ids=detect_ids,
            excel_mapping=mapping,
            excel_columns=columns,
        )


def profile_key(model_path: str | Path) -> str:
    """프로파일 식별자. 같은 파일명이면 폴더가 달라도 같은 설정을 쓴다."""
    return Path(str(model_path or "")).name.strip().lower()


def file_fingerprint(model_path: str | Path) -> str:
    """모델 파일이 교체되었는지 판단하기 위한 지문(크기-수정시각)."""
    try:
        stat = Path(model_path).stat()
    except OSError:
        return ""
    return f"{stat.st_size}-{int(stat.st_mtime)}"


def list_model_files(models_dir: str | Path) -> List[Path]:
    """models 폴더에서 사용 가능한 모델 파일을 이름순으로 나열한다."""
    directory = Path(models_dir)
    if not directory.is_dir():
        return []
    found = [e for e in directory.iterdir() if e.is_file() and e.suffix.lower() in MODEL_SUFFIXES]
    return sorted(found, key=lambda p: p.name.lower())


def read_model_classes(model_path: str | Path) -> Dict[int, str]:
    """모델 파일을 열어 {클래스 번호: 클래스 이름}을 읽는다.

    실패하면 빈 dict를 돌려주고 예외를 올리지 않는다(UI가 멈추지 않도록).
    """
    path = Path(model_path)
    if not path.exists():
        return {}
    try:
        from ultralytics import YOLO  # 지역 import: GUI 시작 시간을 늘리지 않기 위함
    except Exception:
        return {}
    try:
        names = YOLO(str(path)).names
    except Exception:
        return {}
    if isinstance(names, dict):
        result: Dict[int, str] = {}
        for k, v in names.items():
            try:
                result[int(k)] = str(v).strip()
            except (TypeError, ValueError):
                continue
        return result
    if isinstance(names, (list, tuple)):
        return {i: str(v).strip() for i, v in enumerate(names)}
    return {}


def suggest_excel_mapping(class_names: List[str]) -> Tuple[Dict[str, str], List[str]]:
    """클래스 이름을 보고 알려진 계열의 기본 엑셀 매핑을 제안한다.

    커스텀(best.pt) 계열과 COCO 계열 중 더 많이 일치하는 쪽을 고르고,
    어느 쪽도 아니면 클래스 이름을 그대로 열로 쓴다.
    """
    normalized = [str(n).strip() for n in class_names if str(n).strip()]
    lowered = [n.lower() for n in normalized]

    custom_hits = sum(1 for n in lowered if n in CUSTOM_EXCEL_MAPPING)
    coco_hits = sum(1 for n in lowered if n in COCO_EXCEL_MAPPING)
    korean_hits = sum(1 for n in normalized if n in KOREAN_PASSTHROUGH)

    if korean_hits and korean_hits >= custom_hits and korean_hits >= coco_hits:
        mapping = {n: n for n in normalized if n in KOREAN_PASSTHROUGH}
        source_columns = (
            CUSTOM_EXCEL_COLUMNS if any(n in CUSTOM_EXCEL_COLUMNS for n in mapping.values()) else COCO_EXCEL_COLUMNS
        )
    elif custom_hits and custom_hits >= coco_hits:
        mapping = {n: CUSTOM_EXCEL_MAPPING[n.lower()] for n in normalized if n.lower() in CUSTOM_EXCEL_MAPPING}
        source_columns = CUSTOM_EXCEL_COLUMNS
    elif coco_hits:
        mapping = {n: COCO_EXCEL_MAPPING[n.lower()] for n in normalized if n.lower() in COCO_EXCEL_MAPPING}
        source_columns = COCO_EXCEL_COLUMNS
    else:
        mapping = {n: n for n in normalized if n.lower() not in NON_VEHICLE_CLASS_NAMES}
        source_columns = []

    used = {v for v in mapping.values() if v}
    columns = [c for c in source_columns if c in used]
    # 알려진 순서에 없는 열은 클래스 등장 순서를 유지해 뒤에 붙인다.
    for name in normalized:
        target = mapping.get(name, "")
        if target and target not in columns:
            columns.append(target)
    return mapping, columns


def suggest_detect_class_ids(
    classes: Dict[int, str],
    excel_mapping: Optional[Dict[str, str]] = None,
) -> List[int]:
    """기본 탐지 대상 클래스 번호.

    집계 열로 매핑된 차종에 이륜차·자전거를 더해 고른다. COCO 모델은
    80클래스 중 실제 차종은 5개뿐이라, 이 규칙이 없으면 새·의자까지
    탐지 대상이 된다. 반대로 집계 기본 열(3종)만 쓰면 이륜차가 DB에
    남지 않아 나중에 되돌릴 수 없다.
    """
    if excel_mapping:
        mapped = {str(k).strip().lower() for k, v in excel_mapping.items() if str(v).strip()}
        # 집계 기본 열에 없더라도 차종이면 탐지해 둔다(나중에 재집계할 수 있도록).
        mapped |= set(OPTIONAL_EXCEL_MAPPING)
        selected = [cid for cid, name in classes.items() if str(name).strip().lower() in mapped]
        if selected:
            return sorted(selected)
    selected = [cid for cid, name in classes.items() if str(name).strip().lower() not in NON_VEHICLE_CLASS_NAMES]
    return sorted(selected) if selected else sorted(classes)


def build_profile(model_path: str | Path, classes: Optional[Dict[int, str]] = None) -> ModelProfile:
    """모델에서 읽은 클래스로 기본값이 채워진 새 프로파일을 만든다."""
    path = Path(model_path)
    resolved = dict(classes) if classes is not None else read_model_classes(path)
    mapping, columns = suggest_excel_mapping([resolved[c] for c in sorted(resolved)])
    return ModelProfile(
        key=profile_key(path),
        path=str(path),
        fingerprint=file_fingerprint(path),
        classes=resolved,
        detect_class_ids=suggest_detect_class_ids(resolved, mapping),
        excel_mapping=mapping,
        excel_columns=columns,
    )


class ModelProfileStore:
    """config/model_profiles.json 에 프로파일을 읽고 쓰는 저장소."""

    def __init__(self, store_path: str | Path):
        self.store_path = Path(store_path)
        self._profiles: Dict[str, ModelProfile] = {}
        self._loaded = False

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self.store_path.exists():
            return
        try:
            with open(self.store_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        models = data.get("models") if isinstance(data, dict) else None
        if not isinstance(models, dict):
            return
        for key, raw in models.items():
            if isinstance(raw, dict):
                self._profiles[str(key)] = ModelProfile.from_json(str(key), raw)

    def save(self) -> None:
        self._ensure_loaded()
        payload = {
            "schema_version": PROFILE_SCHEMA_VERSION,
            "models": {key: prof.to_json() for key, prof in sorted(self._profiles.items())},
        }
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.store_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

    def get_cached(self, model_path: str | Path) -> Optional[ModelProfile]:
        """모델을 로드하지 않고 저장된 프로파일만 돌려준다."""
        self._ensure_loaded()
        return self._profiles.get(profile_key(model_path))

    def get(self, model_path: str | Path, *, refresh: bool = False) -> ModelProfile:
        """프로파일을 가져온다. 캐시가 없거나 모델 파일이 바뀌었으면 다시 읽는다."""
        self._ensure_loaded()
        key = profile_key(model_path)
        cached = self._profiles.get(key)
        fingerprint = file_fingerprint(model_path)

        stale = cached is None or not cached.has_classes
        if cached is not None and fingerprint and cached.fingerprint != fingerprint:
            stale = True
        if not (refresh or stale):
            return cached  # type: ignore[return-value]

        classes = read_model_classes(model_path)
        if not classes:
            # 모델을 읽지 못하면 기존 캐시라도 유지한다.
            if cached is not None:
                return cached
            empty = ModelProfile(key=key, path=str(model_path), fingerprint=fingerprint)
            self._profiles[key] = empty
            return empty

        fresh = build_profile(model_path, classes)
        if cached is not None and cached.has_classes:
            # 사용자가 고른 값은 살리되, 사라진 클래스는 정리한다.
            valid_names = set(fresh.class_names())
            kept_ids = [cid for cid in cached.detect_class_ids if cid in fresh.classes]
            if kept_ids:
                fresh.detect_class_ids = kept_ids
            kept_mapping = {k: v for k, v in cached.excel_mapping.items() if k in valid_names}
            if kept_mapping:
                fresh.excel_mapping = kept_mapping
                kept_columns = [c for c in cached.excel_columns if c in set(kept_mapping.values())]
                fresh.excel_columns = kept_columns or fresh.excel_columns
        self._profiles[key] = fresh
        self.save()
        return fresh

    def put(self, profile: ModelProfile) -> None:
        self._ensure_loaded()
        self._profiles[profile.key] = profile
        self.save()
