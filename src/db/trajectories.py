"""Read both trajectory formats without hiding legacy rows or counting copies twice."""
from __future__ import annotations

import sqlite3
from itertools import groupby
from typing import Iterator

from src.db.writer import decode_traj


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("select name from sqlite_master where type='table'")}


def list_trajectory_sessions(conn: sqlite3.Connection) -> list[str]:
    tables = _tables(conn)
    sessions: set[str] = set()
    for table, valid in (("track_trajs", "traj is not null"),
                         ("tracks", "center_x is not null and center_y is not null")):
        if table in tables:
            sessions.update(str(row[0]) for row in conn.execute(
                f"select distinct coalesce(session_id, '') from {table} where {valid}"
            ))
    return sorted(sessions)


def trajectory_end_sec(conn: sqlite3.Connection, session_id: str | None = None) -> float | None:
    tables = _tables(conn)
    ends = []
    for table, column in (("track_trajs", "end_ts_ms"), ("tracks", "timestamp_ms")):
        if table not in tables:
            continue
        sql = f"select max({column}) from {table}"
        params = ()
        if session_id is not None:
            sql += " where coalesce(session_id, '') = ?"
            params = (session_id,)
        value = conn.execute(sql, params).fetchone()[0]
        if value is not None:
            ends.append(float(value) / 1000.0)
    return max(ends) if ends else None


def iter_trajectories(
    conn: sqlite3.Connection, session_id: str | None = None,
    *, slot_index: int | None = None, slot_interval_ms: int = 900000,
) -> Iterator[tuple[str, str, str, str, list]]:
    """Yield (session, camera, track, class, points); prefer a nonempty modern copy.

    None selects all sessions; an empty string selects unnamed legacy sessions.
    The caller owns the connection. Only one legacy track's points are buffered.
    """
    tables = _tables(conn)
    modern_keys = set()
    where = ""
    params = ()
    if session_id is not None:
        where = " and coalesce(session_id, '') = ?"
        params = (session_id,)
    identity = "coalesce(session_id, ''), coalesce(camera_id, ''), cast(track_id as text)"
    cls = "coalesce(nullif(trim(vehicle_type), ''), class_name, '')"
    if "track_trajs" in tables:
        sql = f"select {identity}, {cls}, traj, start_ts_ms from track_trajs where traj is not null{where} order by session_id, camera_id, track_id"
        for sess, cam, tid, name, blob, start_ms in conn.execute(sql, params):
            key = (sess, cam, str(tid))
            if slot_index is not None and start_ms is not None and int(start_ms // slot_interval_ms) != slot_index:
                modern_keys.add(key)
                continue
            points = decode_traj(blob)
            if not points:
                continue
            key = (sess, cam, str(tid))
            modern_keys.add(key)
            if slot_index is None or int(float(points[0][1]) // slot_interval_ms) == slot_index:
                yield (*key, str(name), points)
    if "tracks" in tables:
        sql = f"select {identity}, {cls}, frame_id, timestamp_ms, center_x, center_y from tracks where center_x is not null and center_y is not null{where} order by session_id, camera_id, track_id, timestamp_ms, frame_id"
        for key, rows in groupby(conn.execute(sql, params), key=lambda row: (row[0], row[1], str(row[2]))):
            if key in modern_keys:
                continue
            points = []
            name = ""
            for row in rows:
                if not name:
                    name = str(row[3] or "")
                point = [float(row[4] or 0), float(row[5] or 0), float(row[6]), float(row[7])]
                if not points or point != points[-1]:
                    points.append(point)
            if points and (slot_index is None or int(points[0][1] // slot_interval_ms) == slot_index):
                yield (*key, name, points)


def iter_trajectory_headers(conn: sqlite3.Connection, session_id: str | None = None):
    """Yield identity, class and start time for UI menus without decoding blobs."""
    tables = _tables(conn)
    keys = set()
    where = ""
    params = ()
    if session_id is not None:
        where = " and coalesce(session_id, '') = ?"
        params = (session_id,)
    identity = "coalesce(session_id, ''), coalesce(camera_id, ''), cast(track_id as text)"
    cls = "coalesce(nullif(trim(vehicle_type), ''), class_name, '')"
    if "track_trajs" in tables:
        for sess, cam, tid, name, start in conn.execute(
            f"select {identity}, {cls}, start_ts_ms from track_trajs where traj is not null{where}", params
        ):
            key = (sess, cam, str(tid))
            keys.add(key)
            yield (*key, str(name), start)
    if "tracks" in tables:
        sql = f"select {identity}, {cls}, timestamp_ms from tracks where center_x is not null and center_y is not null{where} order by session_id, camera_id, track_id, timestamp_ms, frame_id"
        for key, rows in groupby(conn.execute(sql, params), key=lambda row: (row[0], row[1], str(row[2]))):
            if key in keys:
                continue
            first = next(rows)
            name = str(first[3] or "")
            for row in rows:
                name = name or str(row[3] or "")
            yield (*key, name, first[4])


def trajectory_slots(conn: sqlite3.Connection, session_id: str | None = None, interval_ms: int = 900000) -> list[int]:
    return sorted({int(start // interval_ms) for *_rest, start in iter_trajectory_headers(conn, session_id)
                   if start is not None})
