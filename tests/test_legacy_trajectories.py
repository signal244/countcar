import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from src.db.schema import init_db
from src.db.writer import TrackTrajDBWriter
from src.pipeline.count_tracks import load_traj_table, run_count


class LegacyTrajectoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "tracks.sqlite"
        self.lines = Path(self.tmp.name) / "lines.json"
        self.lines.write_text(json.dumps({"image_width": 100, "image_height": 100, "lines": [
            {"id": "A", "points": [[10, 0], [10, 100]], "bound": "west_bound"},
            {"id": "B", "points": [[20, 0], [20, 100]], "bound": "east_bound"},
        ]}), encoding="utf-8")
        init_db(self.db)

    def legacy(self, session="old", tid="1"):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.executemany(
                "insert into tracks(session_id,camera_id,track_id,frame_id,timestamp_ms,center_x,center_y,class_name) values(?,?,?,?,?,?,?,?)",
                [(session, "cam", tid, frame, frame * 1000, x, 50, "car")
                 for frame, x in enumerate([0, 15, 30])],
            )

    def modern(self, session="new", tid="1"):
        with TrackTrajDBWriter(self.db) as writer:
            writer.add_track({"session_id": session, "camera_id": "cam", "track_id": tid,
                              "class_name": "car", "end_ts_ms": 2000},
                             [[0, 0, 0, 50], [1, 1000, 15, 50], [2, 2000, 30, 50]])

    def count(self, session=None):
        path, counts = run_count(self.db, self.lines, session_id=session,
                                reconnect_passes=0, extrap_horizon=0,
                                use_virtual_events=False,
                                out_xlsx=Path(self.tmp.name) / "counts.xlsx")
        self.assertTrue(path.is_file())
        return int(counts["count"].sum())

    def test_initialization_does_not_hide_legacy_data(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("drop table track_trajs")
        self.legacy()
        before = load_traj_table(self.db, "old")
        self.assertEqual(len(before), 3)
        init_db(self.db)
        self.assertTrue(before.equals(load_traj_table(self.db, "old")))
        self.assertEqual(self.count("old"), 1)

    def test_mixed_sessions_are_counted_separately_then_summed(self):
        self.legacy()
        self.modern()
        self.assertEqual(self.count("old"), 1)
        self.assertEqual(self.count("new"), 1)
        self.assertEqual(self.count(), 2)

    def test_modern_copy_wins_without_hiding_other_legacy_tracks(self):
        self.legacy("mixed", "1")
        self.legacy("mixed", "2")
        self.modern("mixed", "1")
        self.assertEqual(len(load_traj_table(self.db, "mixed")), 6)
        self.assertEqual(self.count("mixed"), 2)

    def test_unnamed_session_does_not_repeat_named_sessions(self):
        self.legacy(None)
        self.modern()
        self.assertEqual(self.count(), 2)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("select count(*) from track_line_summary where session_id='new'").fetchone()[0], 1)

    def test_empty_database_exports_zero_counts(self):
        self.assertEqual(self.count(), 0)

    def test_ui_class_counts_and_slots_include_legacy_without_duplicates(self):
        from src.db.queries import distinct_vehicle_types
        from src.db.trajectories import trajectory_slots
        self.legacy("mixed", "1")
        self.legacy("mixed", "2")
        self.modern("mixed", "1")
        self.assertEqual(distinct_vehicle_types(self.db, "mixed"), {"car": 2})
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(trajectory_slots(conn, "mixed"), [0])

    def test_gui_loader_reads_legacy_and_filters_slots(self):
        from PySide6.QtWidgets import QApplication
        from src.ui.trajectory_viewer2 import _TrajectoryLoadWorker
        app = QApplication.instance() or QApplication([])
        self.legacy()
        for slot, expected in ((0, 1), (1, 0)):
            worker = _TrajectoryLoadWorker(self.db, "old", slot, 1, 0, 1)
            batches, errors, finished = [], [], []
            worker.batch_ready.connect(batches.extend)
            worker.error.connect(errors.append)
            worker.finished.connect(finished.append)
            worker.run()
            self.assertEqual(errors, [])
            self.assertEqual(len(batches), expected)
            self.assertEqual(finished[0]["track_count"], expected)

    def test_unnamed_virtual_events_do_not_load_other_sessions(self):
        from src.pipeline.virtual_events import load_virtual_events
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.executemany("insert into track_virtual_events(session_id,track_id,ts_ms,line_id,method) values(?,?,?,?,?)",
                             [("new", "1", 1, "A", "test"), ("", "2", 2, "B", "test")])
        self.assertEqual(set(load_virtual_events(self.db, "")), {"2"})
