from __future__ import annotations

import math

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List


@dataclass
class ValidationReport:
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def extend(self, other: "ValidationReport") -> None:
        self.errors.extend(other.errors)
        self.warnings.extend(other.warnings)

    def raise_for_errors(self, prefix: str = "설정 오류") -> None:
        if self.errors:
            raise ValueError(f"{prefix}: " + "; ".join(self.errors))


def _number_in_range(
    cfg: Dict[str, Any],
    key: str,
    report: ValidationReport,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    integer: bool = False,
) -> None:
    if key not in cfg or cfg.get(key) in (None, ""):
        return
    try:
        value = float(cfg[key])
        if isinstance(cfg[key], bool) or not math.isfinite(value):
            raise ValueError
        if integer and (not value.is_integer() or int(cfg[key]) != value):
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        report.errors.append(f"{key}는 유한한 {'정수' if integer else '숫자'}여야 합니다")
        return
    if minimum is not None and value < minimum:
        report.errors.append(f"{key}는 {minimum} 이상이어야 합니다")
    if maximum is not None and value > maximum:
        report.errors.append(f"{key}는 {maximum} 이하여야 합니다")


def validate_app_config(cfg: Dict[str, Any]) -> ValidationReport:
    report = ValidationReport()
    for key in ("model_path", "db_path"):
        if not str(cfg.get(key) or "").strip():
            report.errors.append(f"필수 설정 {key}가 비어 있습니다")

    _number_in_range(cfg, "confidence_threshold", report, minimum=0.0, maximum=1.0)
    _number_in_range(cfg, "target_fps", report, minimum=0.01)
    _number_in_range(cfg, "yolo_imgsz", report, minimum=32.0, integer=True)
    _number_in_range(cfg, "max_idle_frames", report, minimum=1.0, integer=True)
    _number_in_range(cfg, "flush_interval_minutes", report, minimum=0.0, integer=True)

    for key in ("resize_width", "resize_height"):
        _number_in_range(cfg, key, report, minimum=1, integer=True)
    resize_values = [cfg.get(key) not in (None, "") for key in ("resize_width", "resize_height")]
    if any(resize_values) and not all(resize_values):
        report.errors.append("resize_width와 resize_height는 함께 지정해야 합니다")
    roi = cfg.get("roi", [])
    if roi and (not isinstance(roi, list) or len(roi) < 3 or not all(_valid_point(point) for point in roi)):
        report.errors.append("roi는 유한한 좌표 세 개 이상의 다각형이어야 합니다")

    allowed = cfg.get("allowed_classes")
    if allowed is not None:
        if not isinstance(allowed, list):
            report.errors.append("allowed_classes는 정수 목록이어야 합니다")
        else:
            for value in allowed:
                try:
                    number = float(value)
                    if (isinstance(value, bool) or not math.isfinite(number)
                            or number < 0 or not number.is_integer() or int(value) != number):
                        raise ValueError
                except (TypeError, ValueError, OverflowError):
                    report.errors.append("allowed_classes에는 0 이상의 유한한 정수만 사용할 수 있습니다")
                    break
    return report


def _valid_point(point: object) -> bool:
    if not isinstance(point, (list, tuple)) or len(point) < 2:
        return False
    try:
        if any(isinstance(value, bool) or not math.isfinite(float(value)) for value in point[:2]):
            return False
    except (TypeError, ValueError, OverflowError):
        return False
    return True


def validate_line_settings(line_cfg: Dict[str, Any]) -> ValidationReport:
    report = ValidationReport()
    lines = line_cfg.get("lines", [])
    if not isinstance(lines, list):
        report.errors.append("lines는 목록이어야 합니다")
        return report

    seen_ids: set[str] = set()
    for index, line in enumerate(lines):
        if not isinstance(line, dict):
            report.errors.append(f"lines[{index}]는 객체여야 합니다")
            continue
        line_id = str(line.get("id") or line.get("name") or "").strip()
        if not line_id:
            report.errors.append(f"lines[{index}]의 id가 비어 있습니다")
        elif line_id in seen_ids:
            report.errors.append(f"라인 id가 중복되었습니다: {line_id}")
        else:
            seen_ids.add(line_id)

        points = line.get("points")
        if not isinstance(points, list) or len(points) < 2 or not all(_valid_point(point) for point in points):
            report.errors.append(f"라인 {line_id or index}의 points는 두 개 이상의 좌표여야 합니다")
        if line.get("in_point") is not None and not _valid_point(line["in_point"]):
            report.errors.append(f"라인 {line_id or index}의 in_point는 유한한 좌표여야 합니다")
        if not str(line.get("bound") or "").strip():
            report.warnings.append(f"라인 {line_id or index}에 bound가 없어 접근로 집계가 제한될 수 있습니다")

    roi = line_cfg.get("roi", [])
    if roi and (not isinstance(roi, list) or len(roi) < 3 or not all(_valid_point(point) for point in roi)):
        report.errors.append("roi는 세 개 이상의 좌표로 구성된 다각형이어야 합니다")

    for key in ("image_width", "image_height"):
        if line_cfg.get(key) in (None, ""):
            report.warnings.append(f"{key}가 없어 좌표계 자동 검증이 제한됩니다")
            continue
        _number_in_range(line_cfg, key, report, minimum=1, integer=True)
    return report


def format_warnings(messages: Iterable[str]) -> List[str]:
    return [f"[config:warn] {message}" for message in messages]
