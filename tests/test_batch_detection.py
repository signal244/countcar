import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from src.db.schema import init_db
from src.db.writer import TrackTrajDBWriter
from src.services.batch_detection import (
    BatchJob,
    cleanup_partial_sessions,
    db_path_for,
    parse_video_name,
    run_batch,
    session_state,
)
from src.services.detection_service import DetectionResult


def _write_session(db: Path, session_id: str) -> None:
    init_db(db)
    with TrackTrajDBWriter(db) as writer:
        writer.add_track({"session_id": session_id, "camera_id": "cam", "track_id": "1", "class_name": "car",
                          "end_ts_ms": 1000}, [[0, 0, 0, 0], [1, 1000, 10, 0]])


class FakeService:
    """영상 이름에 fail/stop 이 있으면 실패/중지를 흉내 내고, 아니면 세션을 저장한다."""

    def __init__(self):
        self.requests = []

    def run(self, request, progress_cb=None, should_stop_cb=None):
        self.requests.append(request)
        db = Path(request.overrides["db_path"])
        name = Path(request.video_path).name
        if "fail" in name:
            raise RuntimeError("디코딩 실패")
        if "stop" in name:
            _write_session(db, f"{request.session_id}_partial_abc123")
            return DetectionResult(f"{request.session_id}_partial_abc123", db, "cpu", {}, True)
        _write_session(db, request.session_id)
        if progress_cb:
            progress_cb("[done] total sampled frames: 50 (vid_stride=2)")
        return DetectionResult(request.session_id, db, "cpu", {}, False)


class BatchDetectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.service = FakeService()

    def job(self, name, model="", imgsz=""):
        junction, session = parse_video_name(name)
        return BatchJob(self.dir / name, junction, session, model, imgsz)

    def run_jobs(self, jobs, **kw):
        statuses = []
        results = run_batch(jobs, cfg_path=Path("config/app_config.json"), db_dir=self.dir, service=self.service,
                            base_overrides={"model_path": "models/main.pt", "allowed_classes": [1, 2]},
                            status_cb=lambda i, s, d: statuses.append((i, s)), **kw)
        return results, statuses

    def runs(self, junction):
        with closing(sqlite3.connect(db_path_for(junction, self.dir))) as conn:
            return dict(conn.execute("select session_id, status from detection_runs"))

    def test_parse_video_name_uses_last_underscore(self):
        self.assertEqual(parse_video_name("경원교차로_오전첨두.mp4"), ("경원교차로", "오전첨두"))
        self.assertEqual(parse_video_name(r"D:\v\칠산교차로_1_오후첨두.mp4"), ("칠산교차로_1", "오후첨두"))
        self.assertEqual(parse_video_name("이름만.mp4"), ("이름만", ""))

    def test_each_junction_gets_its_own_db_and_sessions(self):
        jobs = [self.job(n) for n in ("경원교차로_오전첨두.mp4", "경원교차로_오후첨두.mp4", "봉황교사거리_오전첨두.mp4")]
        results, _ = self.run_jobs(jobs)
        self.assertEqual([r.status for r in results], ["done"] * 3)
        self.assertEqual(self.runs("경원교차로"), {"경원교차로 오전첨두": "done", "경원교차로 오후첨두": "done"})
        self.assertEqual(self.runs("봉황교사거리"), {"봉황교사거리 오전첨두": "done"})
        req = self.service.requests[0]
        self.assertTrue(req.overwrite_session)
        self.assertEqual(req.overrides["line_settings_path"], "")
        self.assertEqual(req.overrides["db_path"], str(db_path_for("경원교차로", self.dir)))
        self.assertEqual(req.overrides["model_path"], "models/main.pt")  # 기본: 현재 설정

    def test_completed_videos_are_skipped_on_rerun(self):
        jobs = [self.job("경원교차로_오전첨두.mp4"), self.job("경원교차로_점심첨두.mp4")]
        self.run_jobs(jobs)
        self.service.requests.clear()
        results, _ = self.run_jobs(jobs)
        self.assertEqual([r.status for r in results], ["skipped", "skipped"])
        self.assertEqual(self.service.requests, [])

    def test_failure_does_not_stop_the_rest(self):
        results, _ = self.run_jobs([self.job("경원교차로_fail.mp4"), self.job("경원교차로_오후첨두.mp4")])
        self.assertEqual([r.status for r in results], ["failed", "done"])
        self.assertEqual(self.runs("경원교차로")["경원교차로 fail"], "failed")

    def test_existing_session_without_record_follows_policy(self):
        _write_session(db_path_for("경원교차로", self.dir), "경원교차로 오전첨두")
        job = self.job("경원교차로_오전첨두.mp4")
        self.assertEqual(session_state(db_path_for("경원교차로", self.dir), job.session_id), "existing")
        results, _ = self.run_jobs([job])
        self.assertEqual(results[0].status, "skipped")
        results, _ = self.run_jobs([job], existing="overwrite")
        self.assertEqual(results[0].status, "done")

    def test_stop_now_discards_partial_and_halts(self):
        jobs = [self.job("경원교차로_stop.mp4"), self.job("경원교차로_오후첨두.mp4")]
        results, _ = self.run_jobs(jobs)
        self.assertEqual([r.status for r in results], ["stopped"])
        db = db_path_for("경원교차로", self.dir)
        with closing(sqlite3.connect(db)) as conn:
            sessions = {r[0] for r in conn.execute("select distinct session_id from track_trajs")}
        self.assertEqual(sessions, set())
        self.assertEqual(session_state(db, "경원교차로 stop"), "new")  # 다시 실행하면 처음부터 처리

    def test_leftover_partial_sessions_are_cleaned(self):
        db = db_path_for("경원교차로", self.dir)
        _write_session(db, "경원교차로 오전첨두_partial_deadbeef")
        _write_session(db, "경원교차로 오전첨두X")
        self.assertEqual(cleanup_partial_sessions(db, "경원교차로 오전첨두"), ["경원교차로 오전첨두_partial_deadbeef"])
        with closing(sqlite3.connect(db)) as conn:
            self.assertEqual({r[0] for r in conn.execute("select distinct session_id from track_trajs")},
                             {"경원교차로 오전첨두X"})

    def test_per_video_model_and_imgsz(self):
        self.run_jobs([self.job("경원교차로_오전첨두.mp4", "models/other.pt", "1280")])
        overrides = self.service.requests[0].overrides
        self.assertEqual(overrides["model_path"], "models/other.pt")
        self.assertEqual(overrides["allowed_classes"], "auto")  # 메인 모델 기준 번호를 쓰지 않는다
        self.assertEqual(overrides["yolo_imgsz"], 1280)


if __name__ == "__main__":
    unittest.main()
