"""탐지 단일 진입 원칙 회귀 테스트.

CLAUDE.md 규칙: GUI·CLI·Colab 은 서로 다른 탐지 코드를 쓰지 않는다. 모두
공통 src/services/detection_service.py 를 호출한다. 탐지 로직을 고칠 때
GUI 쪽에만 반영하는 실수를 막기 위해, 저수준 탐지기(DetectionTracker /
src.pipeline.detect_track)를 서비스 밖에서 직접 끌어다 쓰면 실패시킨다.

AST 로 import 문만 본다(문자열·주석은 무시). 실제 경로 수렴은 다음과 같다.

  GUI  : src/ui/workers.py            -> DetectionService
  CLI  : src/pipeline/detect_to_db.py -> DetectionService
  Colab: notebook 이 subprocess 로 detect_to_db(CLI) 실행 -> DetectionService
"""

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"

# 저수준 탐지기를 직접 import 해도 되는 유일한 모듈(공통 서비스).
ALLOWED = {SRC / "services" / "detection_service.py"}

# 탐지기가 정의된 모듈. 자기 자신은 검사 대상에서 제외한다.
DETECT_TRACK_MODULE = SRC / "pipeline" / "detect_track.py"

# 서비스 밖에서 나타나면 안 되는 import 신호.
FORBIDDEN_NAMES = {"DetectionTracker"}
FORBIDDEN_MODULE_SUFFIX = "pipeline.detect_track"


def _imports_low_level_detector(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module.endswith(FORBIDDEN_MODULE_SUFFIX):
                return True
            if module.endswith("pipeline") and any(
                alias.name == "detect_track" for alias in node.names
            ):
                return True
            if any(alias.name in FORBIDDEN_NAMES for alias in node.names):
                return True
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.endswith(FORBIDDEN_MODULE_SUFFIX):
                    return True
    return False


class DetectionSingleEntryTests(unittest.TestCase):
    def test_only_service_imports_low_level_tracker(self) -> None:
        offenders = []
        for path in SRC.rglob("*.py"):
            if path in ALLOWED or path == DETECT_TRACK_MODULE:
                continue
            if "__pycache__" in path.parts:
                continue
            if _imports_low_level_detector(path):
                offenders.append(str(path.relative_to(ROOT)))
        self.assertFalse(
            offenders,
            "탐지기(DetectionTracker/detect_track)를 공통 서비스 밖에서 직접 "
            "import 하는 모듈이 있습니다. GUI·CLI·Colab 은 반드시 "
            "src/services/detection_service.py 를 경유해야 합니다:\n"
            + "\n".join(offenders),
        )

    def test_gui_and_cli_route_through_service(self) -> None:
        """GUI 워커와 CLI 진입점이 실제로 DetectionService 를 import 하는지."""
        for rel in ("src/ui/workers.py", "src/pipeline/detect_to_db.py"):
            path = ROOT / rel
            self.assertTrue(path.exists(), f"경로가 사라졌습니다: {rel}")
            tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
            found = any(
                isinstance(node, ast.ImportFrom)
                and (node.module or "").endswith("services.detection_service")
                and any(alias.name == "DetectionService" for alias in node.names)
                for node in ast.walk(tree)
            )
            self.assertTrue(
                found,
                f"{rel} 가 DetectionService 를 import 하지 않습니다 "
                "(공통 탐지 경로를 우회했을 수 있음).",
            )


if __name__ == "__main__":
    unittest.main()
