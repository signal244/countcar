import sqlite3
from pathlib import Path
from typing import Iterable

SCHEMA_VERSION = "v1"


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

INDEX_SQL: Iterable[str] = [
    "CREATE INDEX IF NOT EXISTS idx_tracks_session_cam_time ON tracks(session_id, camera_id, timestamp_ms)",
    "CREATE INDEX IF NOT EXISTS idx_tracks_track ON tracks(session_id, camera_id, track_id)",
    "CREATE INDEX IF NOT EXISTS idx_tracks_session_track_frame ON tracks(session_id, track_id, frame_id)",
    "CREATE INDEX IF NOT EXISTS idx_traj_session_cam_track ON track_trajs(session_id, camera_id, track_id)",
    "CREATE INDEX IF NOT EXISTS idx_traj_session_cam_time ON track_trajs(session_id, camera_id, start_ts_ms, end_ts_ms)",
]


def init_db(db_path: Path) -> None:
    """Create tables and indexes if they do not exist."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        cur = conn.cursor()
        cur.execute(CREATE_TABLE_SQL)
        cur.execute(CREATE_TRACK_TRAJ_SQL)
        for stmt in INDEX_SQL:
            cur.execute(stmt)
        conn.commit()
