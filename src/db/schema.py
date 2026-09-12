import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

SCHEMA_VERSION = "v2"
DB_USER_VERSION = 2


CREATE_SCHEMA_MIGRATIONS_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
  version INTEGER PRIMARY KEY,
  applied_at TEXT NOT NULL
);
"""


CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS tracks (
  schema_version TEXT,
  session_id TEXT,
  camera_id TEXT,
  frame_id INTEGER,
  timestamp_ms INTEGER,
  track_id TEXT,
  status TEXT CHECK(status IN ('start','ongoing','end')),
  class_id INTEGER,
  class_name TEXT,
  vehicle_type TEXT,
  confidence REAL,
  bbox_x1 REAL, bbox_y1 REAL, bbox_x2 REAL, bbox_y2 REAL,
  center_x REAL, center_y REAL,
  roi_id TEXT,
  speed REAL, speed_unit TEXT,
  direction_hint REAL,
  track_len INTEGER,
  entry_x REAL, entry_y REAL, exit_x REAL, exit_y REAL,
  extra TEXT
);
"""

CREATE_TRACK_TRAJ_SQL = """
CREATE TABLE IF NOT EXISTS track_trajs (
  schema_version TEXT,
  session_id TEXT,
  camera_id TEXT,
  track_id TEXT,
  class_id INTEGER,
  class_name TEXT,
  vehicle_type TEXT,
  start_frame INTEGER,
  end_frame INTEGER,
  start_ts_ms INTEGER,
  end_ts_ms INTEGER,
  track_len INTEGER,
  entry_x REAL, entry_y REAL, exit_x REAL, exit_y REAL,
  direction_hint REAL,
  traj BLOB,
  extra TEXT
);
"""

CREATE_TRACK_MERGE_RUNS_SQL = """
CREATE TABLE IF NOT EXISTS track_merge_runs (
  run_id TEXT PRIMARY KEY,
  session_id TEXT,
  db_path TEXT,
  merged_count INTEGER,
  params_json TEXT,
  created_at TEXT
);
"""

CREATE_TRACK_MERGE_MAP_SQL = """
CREATE TABLE IF NOT EXISTS track_merge_map (
  run_id TEXT,
  session_id TEXT,
  source_track_id TEXT,
  merged_track_id TEXT,
  merge_score REAL,
  time_gap_sec REAL,
  end_start_dist REAL,
  direction_score REAL,
  class_match INTEGER,
  created_at TEXT,
  PRIMARY KEY (session_id, source_track_id)
);
"""

CREATE_TRACK_MERGE_MAP_AUTO_SQL = """
CREATE TABLE IF NOT EXISTS track_merge_map_auto (
  run_id TEXT,
  session_id TEXT,
  source_track_id TEXT,
  merged_track_id TEXT,
  merge_score REAL,
  time_gap_sec REAL,
  end_start_dist REAL,
  direction_score REAL,
  class_match INTEGER,
  created_at TEXT,
  PRIMARY KEY (session_id, source_track_id)
);
"""

CREATE_TRACK_MERGE_MAP_MANUAL_SQL = """
CREATE TABLE IF NOT EXISTS track_merge_map_manual (
  session_id TEXT,
  source_track_id TEXT,
  merged_track_id TEXT,
  reason TEXT,
  created_at TEXT,
  PRIMARY KEY (session_id, source_track_id)
);
"""

CREATE_TRACK_MERGE_EXCLUDE_MANUAL_SQL = """
CREATE TABLE IF NOT EXISTS track_merge_exclude_manual (
  session_id TEXT,
  source_track_id TEXT,
  merged_track_id TEXT,
  reason TEXT,
  created_at TEXT,
  PRIMARY KEY (session_id, source_track_id, merged_track_id)
);
"""


CREATE_TRACK_EXCLUSION_RUNS_SQL = """
CREATE TABLE IF NOT EXISTS track_exclusion_runs (
  run_id TEXT PRIMARY KEY,
  session_id TEXT,
  db_path TEXT,
  excluded_count INTEGER,
  reason TEXT,
  created_at TEXT
);
"""

CREATE_TRACK_EXCLUSION_MAP_SQL = """
CREATE TABLE IF NOT EXISTS track_exclusion_map (
  run_id TEXT,
  session_id TEXT,
  track_id TEXT,
  reason TEXT,
  source_line_points_json TEXT,
  created_at TEXT,
  PRIMARY KEY (session_id, track_id)
);
"""


CREATE_TRACK_VIRTUAL_EVENTS_SQL = """
CREATE TABLE IF NOT EXISTS track_virtual_events (
  session_id TEXT,
  camera_id TEXT,
  track_id TEXT,
  ts_ms INTEGER,
  line_id TEXT,
  bound TEXT,
  inout TEXT,
  method TEXT,
  horizon REAL,
  src_point_x REAL,
  src_point_y REAL,
  hit_x REAL,
  hit_y REAL,
  created_at TEXT,
  extra TEXT,
  PRIMARY KEY (session_id, track_id, line_id, ts_ms, method)
);
"""

CREATE_TRACK_LINE_EVENTS_SQL = """
CREATE TABLE IF NOT EXISTS track_line_events (
  schema_version TEXT,
  session_id TEXT,
  camera_id TEXT,
  track_id TEXT,
  line_id TEXT,
  bound TEXT,
  cross_order INTEGER,
  ts_ms INTEGER,
  cross_x REAL,
  cross_y REAL,
  inout TEXT,
  dir_x REAL,
  dir_y REAL,
  event_source TEXT,
  created_at TEXT,
  extra TEXT,
  PRIMARY KEY (session_id, camera_id, track_id, line_id, cross_order, event_source)
);
"""

CREATE_TRACK_LINE_SUMMARY_SQL = """
CREATE TABLE IF NOT EXISTS track_line_summary (
  schema_version TEXT,
  session_id TEXT,
  camera_id TEXT,
  track_id TEXT,
  first_line_id TEXT,
  first_bound TEXT,
  first_ts_ms INTEGER,
  last_line_id TEXT,
  last_bound TEXT,
  last_ts_ms INTEGER,
  cross_count INTEGER,
  created_at TEXT,
  extra TEXT,
  PRIMARY KEY (session_id, camera_id, track_id)
);
"""


INDEX_SQL: Iterable[str] = [
    "CREATE INDEX IF NOT EXISTS idx_tracks_session_cam_time ON tracks(session_id, camera_id, timestamp_ms)",
    "CREATE INDEX IF NOT EXISTS idx_tracks_track ON tracks(session_id, camera_id, track_id)",
    "CREATE INDEX IF NOT EXISTS idx_tracks_session_track_frame ON tracks(session_id, track_id, frame_id)",
    "CREATE INDEX IF NOT EXISTS idx_traj_session_cam_track ON track_trajs(session_id, camera_id, track_id)",
    "CREATE INDEX IF NOT EXISTS idx_traj_session_cam_time ON track_trajs(session_id, camera_id, start_ts_ms, end_ts_ms)",
    "CREATE INDEX IF NOT EXISTS idx_merge_runs_session_created ON track_merge_runs(session_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_merge_map_session_merged ON track_merge_map(session_id, merged_track_id)",
    "CREATE INDEX IF NOT EXISTS idx_merge_map_auto_session_merged ON track_merge_map_auto(session_id, merged_track_id)",
    "CREATE INDEX IF NOT EXISTS idx_merge_map_manual_session_merged ON track_merge_map_manual(session_id, merged_track_id)",
    "CREATE INDEX IF NOT EXISTS idx_merge_exclude_manual_session_source ON track_merge_exclude_manual(session_id, source_track_id)",
    "CREATE INDEX IF NOT EXISTS idx_exclusion_runs_session_created ON track_exclusion_runs(session_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_exclusion_map_session_track ON track_exclusion_map(session_id, track_id)",
    "CREATE INDEX IF NOT EXISTS idx_virtual_events_session_track ON track_virtual_events(session_id, track_id)",
    "CREATE INDEX IF NOT EXISTS idx_virtual_events_session_line ON track_virtual_events(session_id, line_id)",
    "CREATE INDEX IF NOT EXISTS idx_line_events_session_track ON track_line_events(session_id, track_id)",
    "CREATE INDEX IF NOT EXISTS idx_line_events_session_line ON track_line_events(session_id, line_id)",
    "CREATE INDEX IF NOT EXISTS idx_line_summary_session_first ON track_line_summary(session_id, first_line_id)",
    "CREATE INDEX IF NOT EXISTS idx_line_summary_session_last ON track_line_summary(session_id, last_line_id)",
]


def init_db(db_path: Path) -> None:
    """Create tables and indexes if they do not exist."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(db_path)) as conn:
        cur = conn.cursor()
        cur.execute("PRAGMA busy_timeout = 30000")
        cur.execute(CREATE_SCHEMA_MIGRATIONS_SQL)
        cur.execute(CREATE_TABLE_SQL)
        cur.execute(CREATE_TRACK_TRAJ_SQL)
        cur.execute(CREATE_TRACK_MERGE_RUNS_SQL)
        cur.execute(CREATE_TRACK_MERGE_MAP_SQL)
        cur.execute(CREATE_TRACK_MERGE_MAP_AUTO_SQL)
        cur.execute(CREATE_TRACK_MERGE_MAP_MANUAL_SQL)
        cur.execute(CREATE_TRACK_MERGE_EXCLUDE_MANUAL_SQL)
        cur.execute(CREATE_TRACK_EXCLUSION_RUNS_SQL)
        cur.execute(CREATE_TRACK_EXCLUSION_MAP_SQL)
        cur.execute(CREATE_TRACK_VIRTUAL_EVENTS_SQL)
        cur.execute(CREATE_TRACK_LINE_EVENTS_SQL)
        cur.execute(CREATE_TRACK_LINE_SUMMARY_SQL)
        for stmt in INDEX_SQL:
            cur.execute(stmt)
        applied_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for version in range(1, DB_USER_VERSION + 1):
            cur.execute(
                "insert or ignore into schema_migrations(version, applied_at) values(?, ?)",
                (version, applied_at),
            )
        cur.execute(f"PRAGMA user_version = {DB_USER_VERSION}")
        conn.commit()
