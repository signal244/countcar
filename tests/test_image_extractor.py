"""image_extractor 순수 로직 테스트.

app.py 의 거대한 on_extract_images 에서 분리한 로직. 예전엔 GUI 에 묶여
테스트할 수 없던 부분이라, 분리와 함께 동작을 고정한다.
"""

import tempfile
import unittest
from pathlib import Path

from src.pipeline.image_extractor import (
    CLASS_NAME_TO_ID,
    MAPPED_NAME_TO_ID,
    detection_to_yolo_lines,
    extract_frames,
    next_start_index,
)


class _FakeBox:
    """ultralytics Box 흉내: .cls[0], .xywhn[0].tolist()."""

    def __init__(self, cls_id, xywhn):
        self.cls = [cls_id]
        self.xywhn = [_ToList(xywhn)]


class _ToList:
    def __init__(self, values):
        self._values = list(values)

    def tolist(self):
        return list(self._values)


class DetectionToYoloLinesTests(unittest.TestCase):
    def test_passenger_car_maps_to_id_2(self) -> None:
        boxes = [_FakeBox(0, [0.5, 0.5, 0.2, 0.4])]
        names = {0: "passenger_car"}
        lines = detection_to_yolo_lines(boxes, names, {})
        self.assertEqual(lines, ["2 0.500000 0.500000 0.200000 0.400000\n"])

    def test_person_is_excluded(self) -> None:
        # person 은 id 0 이라 라벨에서 제외된다.
        boxes = [_FakeBox(0, [0.1, 0.1, 0.1, 0.1])]
        names = {0: "person"}
        self.assertEqual(detection_to_yolo_lines(boxes, names, {}), [])

    def test_ignore_label_is_skipped(self) -> None:
        boxes = [_FakeBox(0, [0.1, 0.1, 0.1, 0.1])]
        names = {0: "none"}  # IGNORE_LABELS 에 속함
        self.assertEqual(detection_to_yolo_lines(boxes, names, {}), [])

    def test_generic_truck_fallback(self) -> None:
        boxes = [_FakeBox(3, [0.5, 0.5, 0.3, 0.3])]
        names = {3: "truck"}  # 범용 모델 폴백 -> 4
        lines = detection_to_yolo_lines(boxes, names, {})
        self.assertTrue(lines[0].startswith("4 "))

    def test_korean_mapped_name(self) -> None:
        # 원본 이름은 매핑표를 거쳐 한글 이름 -> id 로 해석된다.
        boxes = [_FakeBox(9, [0.5, 0.5, 0.1, 0.1])]
        names = {9: "sedan"}
        class_mapping = {"sedan": "승용차"}
        lines = detection_to_yolo_lines(boxes, names, class_mapping)
        self.assertTrue(lines[0].startswith(f"{MAPPED_NAME_TO_ID['승용차']} "))

    def test_malformed_box_is_skipped(self) -> None:
        class Broken:
            cls = []  # cls[0] 접근 시 IndexError
            xywhn = []
        lines = detection_to_yolo_lines([Broken()], {0: "car"}, {})
        self.assertEqual(lines, [])


class _FakeCap:
    """cv2 VideoCapture 흉내: n 프레임 후 (False, None)."""

    def __init__(self, n):
        self.n = n
        self.i = 0
        self.released = False

    def read(self):
        if self.i >= self.n:
            return False, None
        self.i += 1
        return True, object()

    def release(self):
        self.released = True


class ExtractFramesTests(unittest.TestCase):
    def _recording_imwrite(self, saved):
        def _imwrite(path, _frame):
            saved.append(path)
            return True
        return _imwrite

    def test_step_sampling(self) -> None:
        cap = _FakeCap(10)
        saved = []
        out = extract_frames(
            cap, Path("/imgs"), "j", step=2, first_n_limit=0, start_idx=1,
            imwrite=self._recording_imwrite(saved),
        )
        # frame_id 0,2,4,6,8 => 5장
        self.assertEqual(len(out), 5)
        self.assertTrue(cap.released)

    def test_first_n_limit_stops_early(self) -> None:
        cap = _FakeCap(100)
        saved = []
        out = extract_frames(
            cap, Path("/imgs"), "j", step=1, first_n_limit=3, start_idx=1,
            imwrite=self._recording_imwrite(saved),
        )
        self.assertEqual(len(out), 3)

    def test_start_idx_in_filename(self) -> None:
        cap = _FakeCap(1)
        saved = []
        extract_frames(
            cap, Path("/imgs"), "cam", step=1, first_n_limit=0, start_idx=42,
            imwrite=self._recording_imwrite(saved),
        )
        self.assertTrue(saved[0].endswith("cam_000042.jpg"))

    def test_failed_write_not_counted(self) -> None:
        cap = _FakeCap(3)

        def _fail(_path, _frame):
            return False

        out = extract_frames(
            cap, Path("/imgs"), "j", step=1, first_n_limit=0, start_idx=1,
            imwrite=_fail,
        )
        self.assertEqual(out, [])


class NextStartIndexTests(unittest.TestCase):
    def test_empty_dir_starts_at_one(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(next_start_index(Path(d), "j"), 1)

    def test_resumes_after_max_existing(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            dp = Path(d)
            (dp / "j_000004.jpg").write_bytes(b"")
            (dp / "j_000002.jpg").write_bytes(b"")
            (dp / "other_000099.jpg").write_bytes(b"")  # 다른 prefix 는 무시
            self.assertEqual(next_start_index(dp, "j"), 5)


if __name__ == "__main__":
    unittest.main()
