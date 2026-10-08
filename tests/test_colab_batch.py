import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "colab"))

import batch_snippet  # noqa: E402
import colab_io  # noqa: E402
from src.services.batch_detection import BatchJob, jobs_from_dicts, jobs_from_folder, jobs_to_dicts  # noqa: E402
from tests.test_batch_detection import FakeService  # noqa: E402


class ColabBatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.root = base / "drive_project"          # Drive 의 프로젝트 폴더 역할
        (self.root / "colab").mkdir(parents=True)
        (self.root / "colab" / "app_config_colab.json").write_text("{}", encoding="utf-8")
        self.videos = base / "drive_videos"
        self.videos.mkdir()
        for name in ("경원교차로_오전첨두.mp4", "경원교차로_오후첨두.mp4", "봉황교사거리_오전첨두.mp4", "이름규칙아님.mp4"):
            (self.videos / name).write_bytes(b"video")
        self.work = base / "content"
        self.service = FakeService()

    def run_batch(self, jobs):
        return batch_snippet.run_colab_batch(jobs, root=self.root, work_dir=self.work, service=self.service)

    def drive_sessions(self, junction):
        db = self.root / "output" / "db_snapshots" / f"tracks_{junction}.sqlite"
        with closing(sqlite3.connect(db)) as conn:
            return dict(conn.execute("select session_id, status from detection_runs"))

    def test_folder_plan_skips_badly_named_videos(self):
        jobs = batch_snippet.plan_jobs(video_dir=self.videos)
        self.assertEqual([j.session_id for j in jobs],
                         ["경원교차로 오전첨두", "경원교차로 오후첨두", "봉황교사거리 오전첨두"])

    def test_jobs_file_round_trip_keeps_per_video_model(self):
        jobs, _ = jobs_from_folder(self.videos)
        jobs[1].model_path, jobs[1].imgsz = "models/yolo26n_v1.pt", "1280"
        path = Path(self.tmp.name) / "jobs.json"
        path.write_text(json.dumps({"jobs": jobs_to_dicts(jobs)}), encoding="utf-8")
        loaded = batch_snippet.plan_jobs(jobs_file=path)
        self.assertEqual([(j.session_id, j.model_path, j.imgsz) for j in loaded],
                         [(j.session_id, j.model_path, j.imgsz) for j in jobs])
        self.assertEqual(jobs_from_dicts(jobs_to_dicts(jobs))[1].model_path, "models/yolo26n_v1.pt")

    def test_runs_per_junction_saves_to_drive_and_resumes(self):
        jobs = batch_snippet.plan_jobs(video_dir=self.videos)
        results = self.run_batch(jobs)
        self.assertEqual([r.status for r in results], ["done"] * 3)
        self.assertEqual(self.drive_sessions("경원교차로"), {"경원교차로 오전첨두": "done", "경원교차로 오후첨두": "done"})
        self.assertEqual(self.drive_sessions("봉황교사거리"), {"봉황교사거리 오전첨두": "done"})
        self.assertEqual(list((self.work / "video").glob("*")), [])           # 로컬 영상은 지운다
        self.assertEqual(list((self.root / "output" / "db_snapshots").glob(".lock_*")), [])  # 잠금 해제
        self.service.requests.clear()
        again = self.run_batch(jobs)                                           # 세션이 끊긴 뒤 다시 실행한 경우
        self.assertEqual([r.status for r in again], ["skipped"] * 3)
        self.assertEqual(self.service.requests, [])

    def test_lock_waits_then_clears_stale_lock(self):
        lock = Path(self.tmp.name) / ".lock_x"
        colab_io.acquire_lock(lock)
        with self.assertRaises(TimeoutError):
            colab_io.acquire_lock(lock, timeout_sec=0.2, stale_sec=3600, poll_sec=0.05)
        colab_io.acquire_lock(lock, timeout_sec=1, stale_sec=0, poll_sec=0.05)  # 오래된 잠금으로 보고 가져온다
        colab_io.release_lock(lock)
        self.assertFalse(lock.exists())


if __name__ == "__main__":
    unittest.main()
