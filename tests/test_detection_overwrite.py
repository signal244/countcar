import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.db.schema import init_db
from src.services.detection_service import DetectionRequest, DetectionService


class DetectionOverwriteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.db = root / "tracks.sqlite"
        init_db(self.db)
        self.cfg = root / "config.json"
        self.cfg.write_text(json.dumps({"model_path": "mock.pt", "db_path": str(self.db)}), encoding="utf-8")
        self.video = root / "mock.mp4"
        self.video.touch()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.executemany("insert into track_trajs(session_id,camera_id,track_id) values(?,?,?)",
                             [("target", "cam", "original"), ("keep", "cam", "other")])
            conn.execute("insert into track_merge_map_manual(session_id,source_track_id,merged_track_id) values('target','old','original')")

    def run_service(self, action, stop=None, overwrite=True):
        tracker = Mock()
        tracker.run.side_effect = action
        build = SimpleNamespace(tracker=tracker, device="cpu", info={
            "config_model_path": "mock.pt", "resolved_model_path": "mock.pt",
            "yolo_imgsz": 640, "yolo_rect": True, "apply_class_mapping": False})
        with patch("src.services.detection_service.build_tracker", return_value=build):
            return DetectionService().run(DetectionRequest(self.cfg, self.video,
                session_id="target", overwrite_session=overwrite), should_stop_cb=stop)

    def write_partial(self, **kwargs):
        kwargs["db_writer"].add_track({"session_id": kwargs["session_id"],
            "camera_id": "cam", "track_id": "new"}, [[0, 0, 1, 1]])

    def tracks(self):
        with closing(sqlite3.connect(self.db)) as conn:
            return conn.execute("select session_id,track_id from track_trajs order by session_id,track_id").fetchall()

    def assert_original_preserved(self):
        self.assertIn(("target", "original"), self.tracks())
        self.assertIn(("keep", "other"), self.tracks())
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("select count(*) from track_merge_map_manual where session_id='target'").fetchone()[0], 1)

    def test_first_frame_failure_preserves_original_and_derived_rows(self):
        with self.assertLogs("src.services.detection_service", level="ERROR"):
            with self.assertRaisesRegex(RuntimeError, "first frame"):
                self.run_service(Mock(side_effect=RuntimeError("first frame")))
        self.assert_original_preserved()

    def test_midstream_failure_retains_partial_and_original(self):
        def action(**kwargs):
            self.write_partial(**kwargs)
            raise RuntimeError("midstream")
        with self.assertLogs("src.services.detection_service", level="ERROR"):
            with self.assertRaisesRegex(RuntimeError, "midstream"):
                self.run_service(action)
        self.assert_original_preserved()
        self.assertTrue(any(session.startswith("target_partial_") and tid == "new" for session, tid in self.tracks()))

    def test_cancel_is_latched_and_returns_partial_session(self):
        stop = Mock(side_effect=[True, False])
        def action(**kwargs):
            self.write_partial(**kwargs)
            self.assertTrue(kwargs["should_stop_cb"]())
        result = self.run_service(action, stop=stop)
        self.assertTrue(result.stopped)
        self.assertTrue(result.session_id.startswith("target_partial_"))
        self.assert_original_preserved()
        stop.assert_called_once()

    def test_success_atomically_replaces_only_target(self):
        def action(**kwargs):
            self.assert_original_preserved()
            self.write_partial(**kwargs)
        result = self.run_service(action)
        self.assertFalse(result.stopped)
        self.assertEqual(result.session_id, "target")
        self.assertEqual(self.tracks(), [("keep", "other"), ("target", "new")])
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("select count(*) from track_merge_map_manual").fetchone()[0], 0)

    def test_promotion_failure_rolls_back_original_deletion(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("create trigger reject_promotion before update of session_id on track_trajs begin select raise(ABORT, 'promotion failed'); end")
        with self.assertLogs("src.services.detection_service", level="ERROR"):
            with self.assertRaisesRegex(sqlite3.IntegrityError, "promotion failed"):
                self.run_service(self.write_partial)
        self.assert_original_preserved()
        self.assertTrue(any(session.startswith("target_partial_") for session, _tid in self.tracks()))

    def test_non_overwrite_keeps_unique_session_behavior(self):
        result = self.run_service(self.write_partial, overwrite=False)
        self.assertNotEqual(result.session_id, "target")
        self.assert_original_preserved()
