"""여러 영상 이미지·라벨 추출(label_extraction) 테스트. 영상·모델은 가짜로 대신한다."""

import tempfile
import unittest
from pathlib import Path

import cv2

from src.pipeline.image_extractor import label_class_id
from src.services.label_extraction import (
    ExtractJob,
    ExtractSettings,
    StopFlags,
    frame_step,
    run_extraction,
    sample_frame_ids,
)


class _FakeCap:
    def __init__(self, frames: int, fps: float = 20.0):
        self.frames, self.fps, self.pos = frames, fps, 0

    def isOpened(self):
        return True

    def get(self, prop):
        return {cv2.CAP_PROP_FPS: self.fps, cv2.CAP_PROP_FRAME_COUNT: self.frames}.get(prop, 0)

    def set(self, prop, value):
        if prop == cv2.CAP_PROP_POS_FRAMES:
            self.pos = int(value)

    def grab(self):
        if self.pos >= self.frames:
            return False
        self.pos += 1
        return True

    def read(self):
        if self.pos >= self.frames:
            return False, None
        frame = self.pos
        self.pos += 1
        return True, frame

    def release(self):
        pass


class _Tensor:
    def __init__(self, values):
        self.values = values

    def tolist(self):
        return list(self.values)


class _FakeModel:
    """프레임마다 승용차 1대 + 이륜차 1대를 찾는 모델."""
    names = {0: "passenger_car", 1: "motorcycle"}

    def __init__(self):
        self.seen = []

    def __call__(self, frame, **kwargs):
        self.seen.append(frame)
        boxes = type("B", (), {"cls": _Tensor([0, 1]), "xywhn": _Tensor([[0.5, 0.5, 0.1, 0.1], [0.2, 0.2, 0.05, 0.05]])})
        return [type("R", (), {"boxes": boxes})]


class SamplingTests(unittest.TestCase):
    def test_every_two_seconds_at_20fps_is_every_40th_frame(self):
        self.assertEqual(frame_step(20.0, 2.0), 40)
        self.assertEqual(frame_step(20.0, 0), 1)   # 0 = 모든 프레임

    def test_range_and_limit(self):
        ids = sample_frame_ids(10_000, 20.0, 2.0, start_sec=10, end_sec=20)
        self.assertEqual(list(ids), [200, 240, 280, 320, 360])
        self.assertEqual(list(sample_frame_ids(10_000, 20.0, 2.0, max_images=3)), [0, 40, 80])

    def test_prefix_uses_junction_and_session(self):
        self.assertEqual(ExtractJob(Path("a.mp4"), "장유 센터", "오전첨두").prefix, "장유_센터_오전첨두")


class LabelIdTests(unittest.TestCase):
    def test_unknown_class_is_dropped_in_dataset_scheme(self):
        names = {3: "motorcycle", 2: "car"}
        self.assertIsNone(label_class_id(3, names, {}, "dataset"))   # 예전에는 3(중형화물)으로 잘못 들어갔다
        self.assertEqual(label_class_id(2, names, {}, "dataset"), 2)
        self.assertEqual(label_class_id(3, names, {}, "model"), 3)

    def test_person_is_always_dropped(self):
        self.assertIsNone(label_class_id(0, {0: "person"}, {}, "model"))


class RunExtractionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name)
        self.written = []

    def tearDown(self):
        self.tmp.cleanup()

    def _imwrite(self, path, frame):
        Path(path).write_text(str(frame))
        self.written.append(Path(path).name)
        return True

    def _run(self, jobs, settings, model=None, **kw):
        model = model or _FakeModel()
        return run_extraction(jobs, settings, model_loader=lambda s, log: (model, {}),
                              video_opener=lambda path: _FakeCap(400), imwrite=self._imwrite, **kw), model

    def test_videos_run_in_order_and_labels_follow_dataset_ids(self):
        jobs = [ExtractJob(Path("a.mp4"), "경원교", "오전"), ExtractJob(Path("b.mp4"), "경원교", "오후")]
        results, model = self._run(jobs, ExtractSettings(out_dir=self.out, every_sec=2.0))
        self.assertEqual([r.images for r in results], [10, 10])   # 400프레임 / 40
        self.assertEqual(self.written[0], "경원교_오전_000001.jpg")
        self.assertEqual(self.written[10], "경원교_오후_000001.jpg")
        self.assertEqual(model.seen[:3], [0, 40, 80])
        label = (self.out / "labels" / "경원교_오전_000001.txt").read_text().splitlines()
        self.assertEqual(label, ["2 0.500000 0.500000 0.100000 0.100000"])   # 이륜차는 뺐다
        self.assertEqual(results[0].dropped_boxes, 10)

    def test_numbering_continues_on_rerun(self):
        job = [ExtractJob(Path("a.mp4"), "경원교", "오전")]
        settings = ExtractSettings(out_dir=self.out, every_sec=2.0, max_images=2)
        self._run(job, settings)
        (self.out / "images" / "경원교_오전_000001.jpg").write_text("x")
        (self.out / "images" / "경원교_오전_000002.jpg").write_text("x")
        self.written.clear()
        self._run(job, settings)
        self.assertEqual(self.written, ["경원교_오전_000003.jpg", "경원교_오전_000004.jpg"])

    def test_images_only_skips_model(self):
        loaded = []
        results = run_extraction([ExtractJob(Path("a.mp4"), "j", "s")],
                                 ExtractSettings(out_dir=self.out, every_sec=10.0, images_only=True),
                                 model_loader=lambda s, log: loaded.append(1),
                                 video_opener=lambda path: _FakeCap(400), imwrite=self._imwrite)
        self.assertEqual(loaded, [])
        self.assertEqual(results[0].images, 2)
        self.assertFalse((self.out / "labels").exists())

    def test_stop_after_current_skips_next_video(self):
        flags = StopFlags()
        jobs = [ExtractJob(Path("a.mp4"), "j", "a"), ExtractJob(Path("b.mp4"), "j", "b")]

        def status(i, state, detail):
            if state == "running":
                flags.after_current = True
        results, _ = self._run(jobs, ExtractSettings(out_dir=self.out, every_sec=2.0), flags=flags, status_cb=status)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].status, "done")


if __name__ == "__main__":
    unittest.main()
