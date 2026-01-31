import sqlite3
import json
import zlib
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .schema import SCHEMA_VERSION, init_db

FIELDS: List[str] = [
    "schema_version",
    "session_id",
    "camera_id",
    "frame_id",
    "timestamp_ms",
    "track_id",
    "status",
    "class_id",
    "class_name",
    "vehicle_type",
    "confidence",
    "bbox_x1",
    "bbox_y1",
    "bbox_x2",
    "bbox_y2",
    "center_x",
    "center_y",
    "roi_id",
    "speed",
    "speed_unit",
    "direction_hint",
    "track_len",
    "entry_x",
    "entry_y",
    "exit_x",
    "exit_y",
    "extra",
]

INSERT_SQL = f"""
INSERT INTO tracks ({", ".join(FIELDS)})
VALUES ({", ".join("?" for _ in FIELDS)});
"""


class TrackDBWriter:
    """Buffered writer for track records."""

    def __init__(self, db_path: Path, buffer_size: int = 200):
        self.db_path = db_path
        init_db(db_path)
        self.conn = sqlite3.connect(db_path)
        self.buffer_size = buffer_size
        self._buffer: List[Iterable[Any]] = []

    def _to_row(self, record: Dict[str, Any]) -> List[Any]:
        return [record.get(field) for field in FIELDS]

    def add_record(self, record: Dict[str, Any]) -> None:
        if "schema_version" not in record:
            record["schema_version"] = SCHEMA_VERSION
        self._buffer.append(self._to_row(record))
        if len(self._buffer) >= self.buffer_size:
            self.flush()

    def flush(self) -> None:
        if not self._buffer:
            return
        cur = self.conn.cursor()
        cur.executemany(INSERT_SQL, self._buffer)
        self.conn.commit()
        self._buffer.clear()

    def close(self) -> None:
        self.flush()
        self.conn.close()

    def __enter__(self) -> "TrackDBWriter":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> Optional[bool]:
        self.close()
        return None


TRACK_TRAJ_FIELDS: List[str] = [
    "schema_version",
    "session_id",
    "camera_id",
    "track_id",
    "class_id",
    "class_name",
    "vehicle_type",
    "start_frame",
    "end_frame",
    "start_ts_ms",
    "end_ts_ms",
    "track_len",
    "entry_x",
    "entry_y",
    "exit_x",
    "exit_y",
    "direction_hint",
    "traj",
    "extra",
]

TRACK_TRAJ_INSERT_SQL = f"""
INSERT INTO track_trajs ({", ".join(TRACK_TRAJ_FIELDS)})
VALUES ({", ".join("?" for _ in TRACK_TRAJ_FIELDS)});
"""


def _encode_traj(points: List[List[float]]) -> bytes:
    # points: [[frame_id, ts_ms, x, y], ...]
    payload = json.dumps(points, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return zlib.compress(payload, level=3)


def decode_traj(blob: bytes) -> List[List[float]]:
    if blob is None:
        return []
    try:
        raw = zlib.decompress(blob)
        return json.loads(raw.decode("utf-8"))
    except Exception:
        try:
            return json.loads(blob.decode("utf-8"))
        except Exception:
            return []


class TrackTrajDBWriter:
    """Track-level writer (1 row per track_id with compressed trajectory)."""

    def __init__(self, db_path: Path, buffer_size: int = 50):
        self.db_path = db_path
        init_db(db_path)
        self.conn = sqlite3.connect(db_path)
        self.buffer_size = buffer_size
        self._buffer: List[Iterable[Any]] = []

    def add_track(self, record: Dict[str, Any], points: List[List[float]]) -> None:
        if "schema_version" not in record:
            record["schema_version"] = SCHEMA_VERSION
        record["traj"] = _encode_traj(points)
        row = [record.get(field) for field in TRACK_TRAJ_FIELDS]
        self._buffer.append(row)
        if len(self._buffer) >= self.buffer_size:
            self.flush()

    def flush(self) -> None:
        if not self._buffer:
            return
        cur = self.conn.cursor()
        cur.executemany(TRACK_TRAJ_INSERT_SQL, self._buffer)
        self.conn.commit()
        self._buffer.clear()

    def close(self) -> None:
        self.flush()
        self.conn.close()

    def __enter__(self) -> "TrackTrajDBWriter":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> Optional[bool]:
        self.close()
        return None
