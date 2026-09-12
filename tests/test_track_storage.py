import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from src.db.schema import DB_USER_VERSION, init_db
from src.db.writer import CHECKPOINT_EXTRA, TrackTrajDBWriter, decode_traj
from src.pipeline.detect_track import _add_class_evidence, _apply_stable_class
from src.pipeline.track_merge import load_effective_track_merge_map
from src.services.detection_service import _delete_session


class TrackStorageTests(unittest.TestCase):
    def test_weighted_class_evidence_beats_single_peak(self) -> None:
        meta = {}
        _add_class_evidence(meta, 1, "car", "승용차", 0.95)
        for _ in range(4):
            _add_class_evidence(meta, 2, "truck", "화물", 0.70)
        _apply_stable_class(meta)
        self.assertEqual(meta["vehicle_type"], "화물")

    def test_checkpoint_is_replaced_by_final_track(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "tracks.sqlite"
            record = {
                "session_id": "session",
                "camera_id": "cam",
                "track_id": "1",
                "class_id": 1,
                "class_name": "car",
                "vehicle_type": "승용차",
                "start_frame": 0,
                "end_frame": 1,
                "start_ts_ms": 0,
                "end_ts_ms": 100,
                "track_len": 2,
            }
            points = [[0, 0, 1.0, 1.0], [1, 100, 2.0, 2.0]]
            with TrackTrajDBWriter(db_path, buffer_size=10) as writer:
                writer.checkpoint_track(record, points[:1])
                writer.add_track(record, points)

            with closing(sqlite3.connect(db_path)) as conn:
                rows = conn.execute("select traj, extra from track_trajs").fetchall()
                user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            self.assertEqual(user_version, DB_USER_VERSION)
            self.assertEqual(len(rows), 1)
            self.assertNotEqual(rows[0][1], CHECKPOINT_EXTRA)
            self.assertEqual(decode_traj(rows[0][0]), points)

    def test_merge_map_is_flattened_and_cycles_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "tracks.sqlite"
            init_db(db_path)
            with closing(sqlite3.connect(db_path)) as conn, conn:
                conn.executemany(
                    "insert into track_merge_map_auto(session_id, source_track_id, merged_track_id) values(?,?,?)",
                    [
                        ("session", "A", "B"),
                        ("session", "B", "C"),
                        ("session", "D", "E"),
                        ("session", "E", "D"),
                    ],
                )
            mapping = load_effective_track_merge_map(db_path, "session")
            self.assertEqual(mapping["A"], "C")
            self.assertEqual(mapping["B"], "C")
            self.assertNotIn("D", mapping)
            self.assertNotIn("E", mapping)

    def test_overwrite_cleanup_is_scoped_to_one_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "tracks.sqlite"
            init_db(db_path)
            with closing(sqlite3.connect(db_path)) as conn, conn:
                conn.executemany(
                    "insert into track_trajs(session_id, camera_id, track_id) values(?,?,?)",
                    [("target", "cam", "1"), ("keep", "cam", "2")],
                )
                conn.executemany(
                    "insert into track_merge_runs(run_id, session_id) values(?,?)",
                    [("merge-target", "target"), ("merge-keep", "keep")],
                )
                conn.executemany(
                    "insert into track_exclusion_runs(run_id, session_id) values(?,?)",
                    [("exclude-target", "target"), ("exclude-keep", "keep")],
                )
                _delete_session(conn, "target")
                self.assertEqual(conn.execute("select count(*) from track_trajs").fetchone()[0], 1)
                self.assertEqual(conn.execute("select count(*) from track_merge_runs").fetchone()[0], 1)
                self.assertEqual(conn.execute("select count(*) from track_exclusion_runs").fetchone()[0], 1)
                self.assertEqual(
                    conn.execute("select session_id from track_trajs").fetchone()[0],
                    "keep",
                )


if __name__ == "__main__":
    unittest.main()
