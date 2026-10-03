import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from src.db.schema import init_db
from src.db.writer import TrackTrajDBWriter
from src.pipeline.count_tracks import run_count
from src.pipeline.track_merge import merge_params_from_config

SESSION = "s"


class AutoMergeCountTests(unittest.TestCase):
    """차량이 가려져 궤적이 둘로 끊긴 상황: 1번은 A선만, 2번은 B선만 통과한다."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "tracks.sqlite"
        self.lines = Path(self.tmp.name) / "lines.json"
        self.lines.write_text(json.dumps({"image_width": 1000, "image_height": 200, "lines": [
            {"id": "A", "points": [[100, 0], [100, 200]], "bound": "west_bound"},
            {"id": "B", "points": [[900, 0], [900, 200]], "bound": "east_bound"},
        ]}), encoding="utf-8")
        init_db(self.db)
        cfg = json.loads(Path("config/app_config.json").read_text(encoding="utf-8-sig"))
        self.params = merge_params_from_config(cfg)

    def add_track(self, tid, start_ms, start_x, end_x, step_ms=500, speed=100.0):
        pts, t, x = [], start_ms, float(start_x)
        while x <= end_x:
            pts.append([t // 100, t, x, 100.0])
            t += step_ms
            x += speed * step_ms / 1000.0
        with TrackTrajDBWriter(self.db) as writer:
            writer.add_track({"session_id": SESSION, "camera_id": "cam", "track_id": tid, "class_name": "car",
                              "start_ts_ms": pts[0][1], "end_ts_ms": pts[-1][1]}, pts)

    def count(self, auto_merge):
        _path, counts = run_count(self.db, self.lines, session_id=SESSION, reconnect_passes=0,
                                  extrap_horizon=0, use_virtual_events=False, use_track_merge=True,
                                  auto_merge=auto_merge, out_xlsx=Path(self.tmp.name) / "counts.xlsx")
        return int(counts["count"].sum())

    def test_config_enables_merge_with_backward_guard(self):
        self.assertIsNotNone(self.params)
        self.assertGreater(self.params["max_backward_px"], 0)
        self.assertFalse(self.params["require_roi_boundary_cross"])

    def test_occluded_vehicle_is_merged_into_one_turn(self):
        self.add_track("1", 0, 0, 400)        # 0~4초, A선 통과 후 가려짐
        self.add_track("2", 5000, 500, 1000)  # 1초 뒤 진행방향 앞쪽에서 다시 나타나 B선 통과
        self.assertEqual(self.count(None), 0)
        self.assertEqual(self.count(self.params), 1)

    def test_track_starting_behind_parent_end_is_not_merged(self):
        self.add_track("1", 0, 0, 400)
        self.add_track("2", 5000, 200, 1000)  # 부모 끝점보다 200px 뒤에서 시작 = 뒤따르던 다른 차
        self.assertEqual(self.count(dict(self.params, max_backward_px=0)), 1)
        self.assertEqual(self.count(self.params), 0)

    def test_manual_exclusion_overrides_auto_merge(self):
        self.add_track("1", 0, 0, 400)
        self.add_track("2", 5000, 500, 1000)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("insert into track_merge_exclude_manual(session_id, source_track_id, merged_track_id) "
                         "values(?,?,?)", (SESSION, "2", "1"))
        self.assertEqual(self.count(self.params), 0)

    def test_broken_only_keeps_complete_track_intact(self):
        from src.pipeline.track_merge import run_track_merge
        self.add_track("1", 0, 0, 950)        # A, B 모두 통과 = 이미 집계되는 완결 궤적
        self.add_track("2", 10000, 1000, 1300)  # 바로 앞에 나타난 조각
        loose = dict(self.params, broken_only=False)
        self.assertEqual(run_track_merge(self.db, SESSION, lines_path=self.lines, **loose)["merged_count"], 1)
        self.assertEqual(run_track_merge(self.db, SESSION, lines_path=self.lines, **self.params)["merged_count"], 0)

    def test_disabled_in_config_returns_none(self):
        self.assertIsNone(merge_params_from_config({"count_merge_enabled": False, "count_merge_max_gap_sec": 5}))


if __name__ == "__main__":
    unittest.main()
