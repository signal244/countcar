"""
Count vehicle tracks from tracks.sqlite with reconnect + extrapolation heuristic.

Steps:
- 1차: 교차 2회 이상 트랙을 바로 카운트 (첫 라인 → 마지막 라인, 15분 등 슬롯, 차종별)
- 2차: 교차 1회 트랙을 시간/거리 근접 기준으로 재연결 (여러 패스 가능)
- 3차: 여전히 1회 트랙을 외삽해 다른 라인과 교차하면 두 번째 교차로 추가
"""

import logging
import argparse
import bisect
import json
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple

import numpy as np
import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

from src.db.schema import SCHEMA_VERSION, init_db
from src.db.writer import decode_traj
from src.pipeline.geometry import bound_in_direction as _bound_in_dir
from src.pipeline.geometry import segment_intersection as _segment_intersection
from src.pipeline.track_merge import load_effective_track_merge_map
from src.pipeline.virtual_events import load_virtual_events

EXTRAP_LINE_OVERSHOOT_PX = 20.0


logger = logging.getLogger(__name__)

def slot_id(ts: float, interval_sec: int) -> int:
    return int(ts // interval_sec) * interval_sec


def segment_intersects(a1, a2, b1, b2) -> bool:
    return _segment_intersection(a1, a2, b1, b2) is not None


def _pt_xy(p) -> Optional[Tuple[float, float]]:
    if not isinstance(p, (list, tuple)) or len(p) < 2:
        return None
    try:
        if len(p) >= 4:
            return float(p[2]), float(p[3])
        return float(p[0]), float(p[1])
    except Exception:
        logger.debug("Suppressed error", exc_info=True)
        return None


def _line_segments(points: List) -> List[Tuple[Tuple[float, float], Tuple[float, float]]]:
    out: List[Tuple[Tuple[float, float], Tuple[float, float]]] = []
    prev: Optional[Tuple[float, float]] = None
    for raw in points or []:
        cur = _pt_xy(raw)
        if cur is None:
            continue
        if prev is not None and (abs(cur[0] - prev[0]) > 1e-9 or abs(cur[1] - prev[1]) > 1e-9):
            out.append((prev, cur))
        prev = cur
    return out


def _segment_hit(p_from: Tuple[float, float], p_to: Tuple[float, float], lines: List[Dict]) -> Optional[str]:
    for ln in lines:
        segments = _line_segments(ln.get("points") or [])
        if any(segment_intersects(p_from, p_to, b1, b2) for b1, b2 in segments):
            return ln.get("id") or ln.get("name") or "line"
    return None


def extrapolate_line_hits(traj_pts, lines: List[Dict], horizon: float) -> Tuple[Optional[str], Optional[str]]:
    """궤적의 시작/끝에서 각각 외삽해 라인 교차 후보를 찾는다.

    - head: 첫 두 점 방향을 반대로(뒤로) 연장
    - tail: 마지막 두 점 방향으로(앞으로) 연장
    """
    if not traj_pts or len(traj_pts) < 2:
        return None, None

    xy = []
    for p in traj_pts:
        pt = _pt_xy(p)
        if pt is not None:
            xy.append(pt)
    if len(xy) < 2:
        return None, None

    eff_horizon = max(0.0, float(horizon)) + EXTRAP_LINE_OVERSHOOT_PX

    def _extrap(p1: Tuple[float, float], p2: Tuple[float, float], forward: bool) -> Optional[str]:
        v = np.array([p2[0] - p1[0], p2[1] - p1[1]], float)
        n = float(np.linalg.norm(v))
        if n < 1e-6:
            return None
        v = v / n * float(eff_horizon)
        if forward:
            a = (float(p2[0]), float(p2[1]))
            b = (float(p2[0] + v[0]), float(p2[1] + v[1]))
        else:
            a = (float(p1[0] - v[0]), float(p1[1] - v[1]))
            b = (float(p1[0]), float(p1[1]))
        return _segment_hit(a, b, lines)

    head_hit = _extrap(xy[0], xy[1], forward=False)
    tail_hit = _extrap(xy[-2], xy[-1], forward=True)
    return head_hit, tail_hit


def extrapolate_line_hits_detailed(
    pts_raw: List[List[float]],
    norm_lines: List[
        Tuple[str, str, List[Tuple[Tuple[float, float], Tuple[float, float]]], Tuple[float, float]]
    ],
    horizon: float,
) -> Tuple[Optional[Tuple[str, str, str]], Optional[Tuple[str, str, str]]]:
    """시작/끝 외삽으로 교차 라인 + bound + in/out을 만든다.

    Returns: (head_event, tail_event)
      - event: (line_id, bound, inout)
    """
    if not pts_raw or len(pts_raw) < 2 or not norm_lines:
        return None, None

    eff_horizon = max(0.0, float(horizon)) + EXTRAP_LINE_OVERSHOOT_PX

    def _pick_xy(p) -> Optional[Tuple[float, float]]:
        try:
            return float(p[2]), float(p[3])
        except Exception:
            logger.debug("Suppressed error", exc_info=True)
            return None

    p0 = _pick_xy(pts_raw[0])
    p1 = _pick_xy(pts_raw[1])
    p_prev = _pick_xy(pts_raw[-2])
    p_last = _pick_xy(pts_raw[-1])
    if not p0 or not p1 or not p_prev or not p_last:
        return None, None

    def _unit(vx: float, vy: float) -> Optional[Tuple[float, float]]:
        n = float(np.hypot(vx, vy))
        if n < 1e-9:
            return None
        return (vx / n, vy / n)

    def _hit(seg_a: Tuple[float, float], seg_b: Tuple[float, float], vdir: Tuple[float, float]) -> Optional[Tuple[str, str, str]]:
        vx, vy = float(vdir[0]), float(vdir[1])
        for line_id, bound, segments, n_in in norm_lines:
            if any(segment_intersects(seg_a, seg_b, b1, b2) for b1, b2 in segments):
                nx, ny = n_in
                s = vx * nx + vy * ny
                if s > 0:
                    io = "in"
                elif s < 0:
                    io = "out"
                else:
                    io = "unk"
                return (str(line_id), str(bound), io)
        return None

    # head: p0 이전으로 뒤로 연장 (이동 방향은 p0->p1와 동일)
    u0 = _unit(p1[0] - p0[0], p1[1] - p0[1])
    head_event = None
    if u0 is not None:
        a = (float(p0[0] - u0[0] * eff_horizon), float(p0[1] - u0[1] * eff_horizon))
        b = (float(p0[0]), float(p0[1]))
        head_event = _hit(a, b, u0)

    # tail: 마지막 점에서 앞으로 연장 (이동 방향은 p_prev->p_last)
    u1 = _unit(p_last[0] - p_prev[0], p_last[1] - p_prev[1])
    tail_event = None
    if u1 is not None:
        a = (float(p_last[0]), float(p_last[1]))
        b = (float(p_last[0] + u1[0] * eff_horizon), float(p_last[1] + u1[1] * eff_horizon))
        tail_event = _hit(a, b, u1)

    return head_event, tail_event


def count_from_pairs(first_df: pd.DataFrame, last_df: pd.DataFrame, interval_sec: int) -> pd.DataFrame:
    merged = first_df[["track_id", "line_name", "ts_sec", "cls_name"]].merge(
        last_df[["track_id", "line_name"]], on="track_id", suffixes=("_first", "_last")
    )
    ts_col = "ts_sec_first" if "ts_sec_first" in merged.columns else "ts_sec"
    cls_col = "cls_name_first" if "cls_name_first" in merged.columns else "cls_name"
    merged["slot"] = merged[ts_col].apply(lambda t: slot_id(t, interval_sec))
    return (
        merged.groupby(["slot", "line_name_first", "line_name_last", cls_col])
        .size()
        .reset_index(name="count")
        .rename(columns={"line_name_first": "line_from", "line_name_last": "line_to", cls_col: "cls_name"})
    )


def endpoints(df: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame]:
    start = df.sort_values("ts_sec").groupby("track_id").first().reset_index()
    end = df.sort_values("ts_sec").groupby("track_id").last().reset_index()
    return (
        start.rename(columns={"ts_sec": "start_ts", "x": "start_x", "y": "start_y"}),
        end.rename(columns={"ts_sec": "end_ts", "x": "end_x", "y": "end_y"}),
    )


def attempt_reconnect(
    candidate_ids: Iterable,
    cross_df: pd.DataFrame,
    start_df: pd.DataFrame,
    end_df: pd.DataFrame,
    max_gap: float,
    max_dist: float,
) -> Dict[int, int]:
    mapping: Dict[int, int] = {}
    cand_start = start_df[start_df.track_id.isin(candidate_ids)]
    other_end = end_df[~end_df.track_id.isin(candidate_ids)]
    for _, row in cand_start.iterrows():
        tid = row.track_id
        t0 = row.start_ts
        cand = other_end[(t0 - other_end.end_ts >= 0) & (t0 - other_end.end_ts <= max_gap)].copy()
        if cand.empty:
            continue
        cand["dist"] = np.hypot(cand.end_x - row.start_x, cand.end_y - row.start_y)
        cand = cand[cand.dist <= max_dist]
        if cand.empty:
            continue
        best = cand.sort_values(["dist", "end_ts"]).iloc[0]
        mapping[tid] = int(best.track_id)
    return mapping


def merge_crossings(cross_df: pd.DataFrame, mapping: Dict[int, int]) -> pd.DataFrame:
    rows = []
    for _, r in cross_df.iterrows():
        tgt = mapping.get(r.track_id, r.track_id)
        rows.append({"track_id": tgt, "ts_sec": r.ts_sec, "line_name": r.line_name, "cls_name": r.cls_name})
    return pd.DataFrame(rows)


def load_traj_table(db_path: Path, session_id: Optional[str] = None) -> pd.DataFrame:
    """Load per-point trajectory table.

    - If `track_trajs` exists: decode compressed per-track trajectories into rows.
    - Else: fallback to legacy frame-level `tracks` table.
    """
    with closing(sqlite3.connect(db_path)) as conn, conn:
        try:
            row = conn.execute(
                "select name from sqlite_master where type='table' and name='track_trajs'"
            ).fetchone()
            has_traj = bool(row)
        except Exception:
            has_traj = False

        if has_traj:
            sql = (
                "select track_id, coalesce(vehicle_type, class_name) as cls_name, traj "
                "from track_trajs where traj is not null"
            )
            params = None
            if session_id:
                sql += " and session_id = ?"
                params = (session_id,)
            rows = conn.execute(sql, params or ()).fetchall()
            out_rows: List[Dict] = []
            for tid, cls, blob in rows:
                pts = decode_traj(blob) or []
                for _fid, tms, x, y in pts:
                    out_rows.append(
                        {"track_id": str(tid), "ts_sec": float(tms) / 1000.0, "x": float(x), "y": float(y), "cls_name": str(cls or "")}
                    )
            return pd.DataFrame(out_rows) if out_rows else pd.DataFrame(columns=["track_id", "ts_sec", "x", "y", "cls_name"])

        sql = (
            "select track_id, timestamp_ms/1000.0 as ts_sec, center_x as x, center_y as y, "
            "coalesce(vehicle_type, class_name) as cls_name "
            "from tracks where center_x is not null and center_y is not null"
        )
        params = None
        if session_id:
            sql += " and session_id = ?"
            params = (session_id,)
        traj = pd.read_sql(sql, conn, params=params)
    return traj


def _has_track_trajs(db_path: Path) -> bool:
    try:
        with closing(sqlite3.connect(db_path)) as conn, conn:
            row = conn.execute("select 1 from sqlite_master where type='table' and name='track_trajs'").fetchone()
            return bool(row)
    except Exception:
        logger.debug("Suppressed error", exc_info=True)
        return False


def _load_track_merge_map(db_path: Path, session_id: Optional[str]) -> Dict[str, str]:
    return load_effective_track_merge_map(Path(db_path), session_id)


def _normalize_lines(lines: List[Dict]) -> List[Tuple[str, List[Tuple[Tuple[float, float], Tuple[float, float]]]]]:
    out = []
    for ln in lines:
        segments = _line_segments(ln.get("points") or [])
        if not segments:
            continue
        line_id = ln.get("id") or ln.get("name") or "line"
        out.append((str(line_id), segments))
    return out


def _line_in_normal(
    points: List,
    bound: str,
    in_point: Optional[object] = None,
) -> Tuple[float, float]:
    segments = _line_segments(points)
    if not segments:
        return (0.0, 0.0)
    p1 = segments[0][0]
    p2 = segments[-1][1]
    dx = float(p2[0] - p1[0])
    dy = float(p2[1] - p1[1])
    nx, ny = (-dy, dx)
    n_norm = float(np.hypot(nx, ny))
    if n_norm > 1e-9:
        nx /= n_norm
        ny /= n_norm
    if isinstance(in_point, (list, tuple)) and len(in_point) >= 2 and n_norm > 1e-9:
        try:
            ix = float(in_point[0])
            iy = float(in_point[1])
            mx = sum(float(a[0] + b[0]) for a, b in segments) / float(len(segments) * 2)
            my = sum(float(a[1] + b[1]) for a, b in segments) / float(len(segments) * 2)
            side = (ix - mx) * nx + (iy - my) * ny
            if side < 0:
                nx, ny = (-nx, -ny)
            return (float(nx), float(ny))
        except Exception:
            logger.debug("Suppressed error", exc_info=True)
    inx, iny = _bound_in_dir(bound)
    if (inx, iny) != (0.0, 0.0):
        return (float(inx), float(iny))
    return (float(nx), float(ny))


def _normalize_lines_detailed(
    lines: List[Dict],
) -> List[Tuple[str, str, List[Tuple[Tuple[float, float], Tuple[float, float]]], Tuple[float, float]]]:
    """라인을 (id, bound, p1, p2, n_in)로 정규화.

    - n_in: 해당 bound의 'in' 방향 벡터(화면 좌표계 기준)
      - 접근로(approach) in/out 판단은 "라인 교차"는 기하로 판단하고,
        "in/out"은 bound별 이동 방향(vx/vy 부호)으로 판정한다.
    """
    out: List[Tuple[str, str, List[Tuple[Tuple[float, float], Tuple[float, float]]], Tuple[float, float]]] = []
    for ln in lines or []:
        pts = ln.get("points") or []
        if len(pts) < 2:
            continue
        line_id = str(ln.get("id") or ln.get("name") or "line")
        bound = str(ln.get("bound") or "").strip()
        segments = _line_segments(pts)
        if not segments:
            continue
        nx, ny = _line_in_normal(pts, bound, ln.get("in_point"))
        out.append((line_id, bound, segments, (float(nx), float(ny))))
    return out


def _segment_speed(a_xy: Tuple[float, float], a_ts: float, b_xy: Tuple[float, float], b_ts: float) -> float:
    dt = float(b_ts) - float(a_ts)
    if dt <= 1e-6:
        return 0.0
    return float(np.hypot(float(b_xy[0]) - float(a_xy[0]), float(b_xy[1]) - float(a_xy[1]))) / dt


def _project_point(
    pt: Tuple[float, float],
    ref_pair: List[Tuple[float, float, float]],
    distance: float,
    *,
    forward: bool,
) -> Optional[Tuple[float, float]]:
    if len(ref_pair) < 2 or distance <= 0.0:
        return None
    a = ref_pair[0]
    b = ref_pair[1]
    vx = float(b[0]) - float(a[0])
    vy = float(b[1]) - float(a[1])
    norm = float(np.hypot(vx, vy))
    if norm <= 1e-6:
        return None
    ux = vx / norm
    uy = vy / norm
    sign = 1.0 if forward else -1.0
    origin = b if forward else a
    return (float(origin[0]) + (ux * distance * sign), float(origin[1]) + (uy * distance * sign))


def _effective_reconnect_distance(
    parent_tail: List[Tuple[float, float, float]],
    child_head: List[Tuple[float, float, float]],
    gap_sec: float,
) -> float:
    if len(parent_tail) < 2 or len(child_head) < 2:
        return float("inf")
    parent_end = parent_tail[1]
    child_start = child_head[0]
    raw_dist = float(np.hypot(float(parent_end[0]) - float(child_start[0]), float(parent_end[1]) - float(child_start[1])))
    if gap_sec <= 1e-6:
        return raw_dist

    parent_speed = _segment_speed(
        (float(parent_tail[0][0]), float(parent_tail[0][1])),
        float(parent_tail[0][2]),
        (float(parent_tail[1][0]), float(parent_tail[1][1])),
        float(parent_tail[1][2]),
    )
    child_speed = _segment_speed(
        (float(child_head[0][0]), float(child_head[0][1])),
        float(child_head[0][2]),
        (float(child_head[1][0]), float(child_head[1][1])),
        float(child_head[1][2]),
    )
    parent_pred = _project_point(parent_tail, parent_tail, parent_speed * gap_sec, forward=True)
    child_back = _project_point(child_head, child_head, child_speed * gap_sec, forward=False)
    dists = [raw_dist]
    if parent_pred is not None:
        dists.append(
            float(np.hypot(float(parent_pred[0]) - float(child_start[0]), float(parent_pred[1]) - float(child_start[1])))
        )
    if child_back is not None:
        dists.append(
            float(np.hypot(float(parent_end[0]) - float(child_back[0]), float(parent_end[1]) - float(child_back[1])))
        )
    if parent_pred is not None and child_back is not None:
        dists.append(
            float(np.hypot(float(parent_pred[0]) - float(child_back[0]), float(parent_pred[1]) - float(child_back[1])))
        )
    dx = float(child_start[0]) - float(parent_end[0])
    dy = float(child_start[1]) - float(parent_end[1])
    parent_dir = _unit_direction((float(parent_tail[0][0]), float(parent_tail[0][1])), (float(parent_tail[1][0]), float(parent_tail[1][1])))
    child_dir = _unit_direction((float(child_head[0][0]), float(child_head[0][1])), (float(child_head[1][0]), float(child_head[1][1])))
    if parent_dir is not None:
        along = (dx * float(parent_dir[0])) + (dy * float(parent_dir[1]))
        lateral = abs((dx * float(parent_dir[1])) - (dy * float(parent_dir[0])))
        dists.append(lateral + (0.25 * abs(along - (parent_speed * gap_sec))))
    if child_dir is not None:
        along = (dx * float(child_dir[0])) + (dy * float(child_dir[1]))
        lateral = abs((dx * float(child_dir[1])) - (dy * float(child_dir[0])))
        dists.append(lateral + (0.25 * abs(along - (child_speed * gap_sec))))
    return min(dists)


def _unit_direction(a_xy: Tuple[float, float], b_xy: Tuple[float, float]) -> Optional[Tuple[float, float]]:
    vx = float(b_xy[0]) - float(a_xy[0])
    vy = float(b_xy[1]) - float(a_xy[1])
    norm = float(np.hypot(vx, vy))
    if norm <= 1e-6:
        return None
    return (vx / norm, vy / norm)


def _cross_events_from_pts(
    pts_raw: List[List[float]],
    norm_lines: List[Tuple[str, List[Tuple[Tuple[float, float], Tuple[float, float]]]]],
) -> List[Tuple[float, str]]:
    # pts_raw: [[frame_id, ts_ms, x, y], ...]
    if not pts_raw or len(pts_raw) < 2 or not norm_lines:
        return []
    last_hit = None
    events: List[Tuple[float, str]] = []
    for i in range(len(pts_raw) - 1):
        try:
            _fid1, _tms1, x1, y1 = pts_raw[i]
            _fid2, tms2, x2, y2 = pts_raw[i + 1]
        except Exception:
            continue
        a1 = (float(x1), float(y1))
        a2 = (float(x2), float(y2))
        for line_id, segments in norm_lines:
            if any(segment_intersects(a1, a2, b1, b2) for b1, b2 in segments):
                if last_hit == line_id:
                    break
                events.append((float(tms2) / 1000.0, str(line_id)))
                last_hit = line_id
                break
    return events


def _cross_events_detailed_from_pts(
    pts_raw: List[List[float]],
    norm_lines: List[
        Tuple[str, str, List[Tuple[Tuple[float, float], Tuple[float, float]]], Tuple[float, float]]
    ],
) -> List[Tuple[float, str, str, str]]:
    """교차 이벤트 + in/out(법선 기반) 산출.

    Returns: [(ts_sec, line_id, bound, inout), ...]
    """
    return [
        (float(rec["ts_sec"]), str(rec["line_id"]), str(rec["bound"]), str(rec["inout"]))
        for rec in _cross_event_records_from_pts(pts_raw, norm_lines)
    ]


def _cross_event_records_from_pts(
    pts_raw: List[List[float]],
    norm_lines: List[
        Tuple[str, str, List[Tuple[Tuple[float, float], Tuple[float, float]]], Tuple[float, float]]
    ],
) -> List[Dict[str, object]]:
    if not pts_raw or len(pts_raw) < 2 or not norm_lines:
        return []
    rows: List[Dict[str, object]] = []
    cross_order = 0
    last_hit_by_line: Dict[str, int] = {}
    last_event_by_line: Dict[str, Tuple[float, float, float]] = {}
    debounce_ms = 750.0
    rearm_distance_px = 12.0
    for i in range(len(pts_raw) - 1):
        try:
            _fid1, tms1, x1, y1 = pts_raw[i]
            _fid2, tms2, x2, y2 = pts_raw[i + 1]
        except Exception:
            continue
        a1 = (float(x1), float(y1))
        a2 = (float(x2), float(y2))
        vx = float(a2[0] - a1[0])
        vy = float(a2[1] - a1[1])
        if abs(vx) + abs(vy) < 1e-9:
            continue
        hits: List[Tuple[float, float, float, str, str, str]] = []
        for line_id, bound, segments, n_in in norm_lines:
            hit_best: Optional[Tuple[float, float, float]] = None
            for b1, b2 in segments:
                hit = _segment_intersection(a1, a2, b1, b2)
                if hit is None:
                    continue
                if hit_best is None or hit[0] < hit_best[0]:
                    hit_best = hit
            if hit_best is None:
                continue
            nx, ny = n_in
            s = vx * nx + vy * ny
            if s > 0:
                inout = "in"
            elif s < 0:
                inout = "out"
            else:
                inout = "unk"
            ratio, ix, iy = hit_best
            hits.append((float(ratio), float(ix), float(iy), str(line_id), str(bound), str(inout)))
        if not hits:
            continue
        hits.sort(key=lambda row: (row[0], row[3]))
        for ratio, ix, iy, line_id, bound, inout in hits:
            if last_hit_by_line.get(line_id) == i - 1:
                continue
            cross_tms = float(tms1) + (float(tms2) - float(tms1)) * float(ratio)
            previous = last_event_by_line.get(line_id)
            if previous is not None:
                elapsed_ms = float(cross_tms) - float(previous[0])
                moved_px = float(np.hypot(float(ix) - float(previous[1]), float(iy) - float(previous[2])))
                if elapsed_ms < debounce_ms and moved_px < rearm_distance_px:
                    last_hit_by_line[line_id] = i
                    continue
            cross_order += 1
            rows.append(
                {
                    "cross_order": int(cross_order),
                    "ts_sec": float(cross_tms) / 1000.0,
                    "ts_ms": int(round(float(cross_tms))),
                    "line_id": str(line_id),
                    "bound": str(bound),
                    "inout": str(inout),
                    "cross_x": float(ix),
                    "cross_y": float(iy),
                    "dir_x": float(vx),
                    "dir_y": float(vy),
                }
            )
            last_hit_by_line[line_id] = i
            last_event_by_line[line_id] = (float(cross_tms), float(ix), float(iy))
    return rows


def _persist_detected_line_events(
    db_path: Path,
    detected_records_by_tid: Dict[str, List[Dict[str, object]]],
    session_by_tid: Dict[str, str],
    camera_by_tid: Dict[str, str],
    session_filter: Optional[str],
) -> int:
    init_db(Path(db_path))
    created_at = datetime.now().isoformat(timespec="seconds")
    deleted_session = str(session_filter or "")
    inserted = 0
    with closing(sqlite3.connect(Path(db_path))) as conn, conn:
        if session_filter:
            conn.execute(
                "delete from track_line_events where session_id = ? and event_source = 'detected'",
                (deleted_session,),
            )
            conn.execute(
                "delete from track_line_summary where session_id = ?",
                (deleted_session,),
            )
        else:
            conn.execute("delete from track_line_events where event_source = 'detected'")
            conn.execute("delete from track_line_summary")

        event_rows: List[Tuple] = []
        summary_rows: List[Tuple] = []
        for tid, records in detected_records_by_tid.items():
            sess = str(session_by_tid.get(str(tid), deleted_session))
            cam = str(camera_by_tid.get(str(tid), ""))
            sorted_records = sorted(records, key=lambda r: (int(r.get("cross_order") or 0), int(r.get("ts_ms") or 0)))
            for rec in sorted_records:
                event_rows.append(
                    (
                        SCHEMA_VERSION,
                        sess,
                        cam,
                        str(tid),
                        str(rec.get("line_id") or ""),
                        str(rec.get("bound") or ""),
                        int(rec.get("cross_order") or 0),
                        int(rec.get("ts_ms") or 0),
                        float(rec.get("cross_x") or 0.0),
                        float(rec.get("cross_y") or 0.0),
                        str(rec.get("inout") or "unk"),
                        float(rec.get("dir_x") or 0.0),
                        float(rec.get("dir_y") or 0.0),
                        "detected",
                        created_at,
                        "",
                    )
                )
            if sorted_records:
                first = sorted_records[0]
                last = sorted_records[-1]
                summary_rows.append(
                    (
                        SCHEMA_VERSION,
                        sess,
                        cam,
                        str(tid),
                        str(first.get("line_id") or ""),
                        str(first.get("bound") or ""),
                        int(first.get("ts_ms") or 0),
                        str(last.get("line_id") or ""),
                        str(last.get("bound") or ""),
                        int(last.get("ts_ms") or 0),
                        int(len(sorted_records)),
                        created_at,
                        "",
                    )
                )
            else:
                summary_rows.append(
                    (
                        SCHEMA_VERSION,
                        sess,
                        cam,
                        str(tid),
                        "",
                        "",
                        None,
                        "",
                        "",
                        None,
                        0,
                        created_at,
                        "",
                    )
                )
        if event_rows:
            conn.executemany(
                """
                insert or replace into track_line_events
                (schema_version, session_id, camera_id, track_id, line_id, bound, cross_order, ts_ms,
                 cross_x, cross_y, inout, dir_x, dir_y, event_source, created_at, extra)
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                event_rows,
            )
        if summary_rows:
            conn.executemany(
                """
                insert or replace into track_line_summary
                (schema_version, session_id, camera_id, track_id, first_line_id, first_bound, first_ts_ms,
                 last_line_id, last_bound, last_ts_ms, cross_count, created_at, extra)
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                summary_rows,
            )
        conn.commit()
        inserted = len(event_rows)
    return int(inserted)


def _count_from_events(
    events_by_tid: Dict[str, List[Tuple[float, str]]],
    cls_by_tid: Dict[str, str],
    interval_sec: int,
) -> pd.DataFrame:
    rows: List[Dict] = []
    for tid, evts in events_by_tid.items():
        if not evts or len(evts) < 2:
            continue
        evts_sorted = sorted(evts, key=lambda x: x[0])
        first_ts, first_line = evts_sorted[0]
        last_ts, last_line = evts_sorted[-1]
        if not first_line or not last_line:
            continue
        rows.append(
            {
                "slot": slot_id(float(first_ts), interval_sec),
                "line_from": str(first_line),
                "line_to": str(last_line),
                "cls_name": str(cls_by_tid.get(tid, "") or ""),
                "count": 1,
            }
        )
    if not rows:
        return pd.DataFrame(columns=["slot", "line_from", "line_to", "cls_name", "count"])
    return (
        pd.DataFrame(rows)
        .groupby(["slot", "line_from", "line_to", "cls_name"], as_index=False)["count"]
        .sum()
        .sort_values(["slot", "line_from", "line_to", "cls_name"])
        .reset_index(drop=True)
    )


def count_track_trajs_streaming(
    db_path: Path,
    lines: List[Dict],
    interval_sec: int,
    reconnect_dist: float,
    reconnect_gap: float,
    reconnect_passes: int,
    extrap_horizon: float,
    session_id: Optional[str] = None,
    mode: str = "turn",
    log_cb: Optional[Callable[[str], None]] = None,
    use_track_merge: bool = False,
    use_virtual_events: bool = True,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    def log(msg: str) -> None:
        if log_cb is not None:
            log_cb(msg)

    mode_norm = str(mode or "turn").strip().lower()
    if mode_norm in ("approach", "ap"):
        norm_lines_d = _normalize_lines_detailed(lines)
        norm_lines = []  # type: ignore[assignment]
    else:
        norm_lines = _normalize_lines(lines)
        norm_lines_d = []  # type: ignore[assignment]
    norm_lines_detected = _normalize_lines_detailed(lines)

    cls_by_tid: Dict[str, str] = {}
    session_by_tid: Dict[str, str] = {}
    camera_by_tid: Dict[str, str] = {}
    start_ts_by_tid: Dict[str, float] = {}
    start_xy_by_tid: Dict[str, Tuple[float, float]] = {}
    end_ts_by_tid: Dict[str, float] = {}
    end_xy_by_tid: Dict[str, Tuple[float, float]] = {}
    head_xy_by_tid: Dict[str, List[Tuple[float, float]]] = {}
    tail_xy_by_tid: Dict[str, List[Tuple[float, float]]] = {}
    head_meta_by_tid: Dict[str, List[Tuple[float, float, float]]] = {}
    tail_meta_by_tid: Dict[str, List[Tuple[float, float, float]]] = {}
    events_by_tid: Dict[str, List] = {}
    detected_records_by_tid: Dict[str, List[Dict[str, object]]] = {}

    merge_map = _load_track_merge_map(Path(db_path), session_id) if use_track_merge else {}
    grouped_pts: Dict[str, List[List[float]]] = {}
    grouped_meta: Dict[str, Tuple[str, str, str]] = {}

    def process_track(
        tid_s: str,
        pts_value: List[List[float]],
        cls_name: str,
        sess: str,
        cam: str,
    ) -> None:
        pts_raw = sorted(pts_value, key=lambda point: (float(point[1]), float(point[0])))
        if len(pts_raw) < 2:
            return
        try:
            head_index = min(4, len(pts_raw) - 1)
            tail_index = max(0, len(pts_raw) - 5)
            _fid0, t0, x0, y0 = pts_raw[0]
            _fid_h, t_h, x_h, y_h = pts_raw[head_index]
            _fid1, t1, x1, y1 = pts_raw[-1]
            tail_start = pts_raw[tail_index]
            start_ts_by_tid[tid_s] = float(t0) / 1000.0
            start_xy_by_tid[tid_s] = (float(x0), float(y0))
            end_ts_by_tid[tid_s] = float(t1) / 1000.0
            end_xy_by_tid[tid_s] = (float(x1), float(y1))
            head_xy_by_tid[tid_s] = [(float(x0), float(y0)), (float(x_h), float(y_h))]
            head_meta_by_tid[tid_s] = [
                (float(x0), float(y0), float(t0) / 1000.0),
                (float(x_h), float(y_h), float(t_h) / 1000.0),
            ]
            tail_xy_by_tid[tid_s] = [
                (float(tail_start[2]), float(tail_start[3])),
                (float(x1), float(y1)),
            ]
            tail_meta_by_tid[tid_s] = [
                (float(tail_start[2]), float(tail_start[3]), float(tail_start[1]) / 1000.0),
                (float(x1), float(y1), float(t1) / 1000.0),
            ]
        except (TypeError, ValueError, IndexError):
            return

        cls_by_tid.setdefault(tid_s, str(cls_name or ""))
        session_by_tid.setdefault(tid_s, str(sess or ""))
        camera_by_tid.setdefault(tid_s, str(cam or ""))
        detected_records = _cross_event_records_from_pts(pts_raw, norm_lines_detected)
        detected_records_by_tid[tid_s] = list(detected_records)
        if mode_norm in ("approach", "ap"):
            events_by_tid[tid_s] = [
                (float(record["ts_sec"]), str(record["line_id"]), str(record["bound"]), str(record["inout"]))
                for record in detected_records
            ]
        else:
            events_by_tid[tid_s] = [
                (float(record["ts_sec"]), str(record["line_id"])) for record in detected_records
            ]

    with closing(sqlite3.connect(db_path)) as conn, conn:
        sql = (
            "select session_id, camera_id, track_id, coalesce(vehicle_type, class_name) as cls_name, traj "
            "from track_trajs where traj is not null"
        )
        params: List[object] = []
        if session_id:
            sql += " and session_id = ?"
            params.append(session_id)
        sql += " order by track_id"

        loaded = 0
        merged_sources = 0
        for sess, cam, tid, cls_name, blob in conn.execute(sql, tuple(params)):
            tid_s = str(tid)
            rep_tid = str(merge_map.get(tid_s, tid_s))
            if rep_tid != tid_s:
                merged_sources += 1
            pts_raw = decode_traj(blob) or []
            if len(pts_raw) < 2:
                continue
            loaded += 1
            if log_cb is not None and loaded % 500 == 0:
                log(f"[count] loaded tracks={loaded}")
            if merge_map:
                grouped_pts.setdefault(rep_tid, []).extend(pts_raw)
                grouped_meta.setdefault(rep_tid, (str(cls_name or ""), str(sess or ""), str(cam or "")))
            else:
                process_track(rep_tid, pts_raw, str(cls_name or ""), str(sess or ""), str(cam or ""))

    for tid_s, points in grouped_pts.items():
        cls_name, sess, cam = grouped_meta.get(tid_s, ("", str(session_id or ""), ""))
        process_track(tid_s, points, cls_name, sess, cam)

    log(f"[count] loaded track_trajs tracks={loaded} canonical_tracks={len(events_by_tid)} merged_sources={merged_sources}")
    detected_saved = _persist_detected_line_events(
        Path(db_path),
        detected_records_by_tid,
        session_by_tid,
        camera_by_tid,
        session_id,
    )
    log(f"[count] detected line events saved={detected_saved} summaries={len(detected_records_by_tid)}")

    virtual_events = load_virtual_events(Path(db_path), session_id, detailed=mode_norm in ("approach", "ap")) if use_virtual_events else {}
    if virtual_events:
        added = 0
        total = 0
        for tid, evs in virtual_events.items():
            rep_tid = str(merge_map.get(str(tid), str(tid)))
            total += len(evs)
            if rep_tid not in events_by_tid:
                events_by_tid[rep_tid] = []
            existing_lines = {str(e[1]) for e in events_by_tid.get(rep_tid, [])}
            for ev in evs:
                line_id = str(ev[1])
                if line_id in existing_lines:
                    continue
                events_by_tid[rep_tid].append(ev)
                existing_lines.add(line_id)
                added += 1
        log(f"[count] virtual events loaded={total} applied={added}")

    if mode_norm in ("approach", "ap"):
        counts_multi = pd.DataFrame(columns=["slot", "line_from", "line_to", "cls_name", "count"])
    else:
        counts_multi = _count_from_events(events_by_tid, cls_by_tid, interval_sec)

    for pass_idx in range(max(0, int(reconnect_passes))):
        candidates = [tid for tid, ev in events_by_tid.items() if len(ev) <= 1]
        if not candidates:
            break
        candidate_set = set(candidates)
        parents = [tid for tid in events_by_tid.keys() if tid not in candidate_set]
        if not parents:
            break

        parent_sorted = sorted(parents, key=lambda t: end_ts_by_tid.get(t, -1e18))
        parent_end_ts = [end_ts_by_tid.get(t, -1e18) for t in parent_sorted]

        merged = 0
        used_parents: set[str] = set()
        for tid in sorted(candidates, key=lambda t: start_ts_by_tid.get(t, 1e18)):
            t0 = start_ts_by_tid.get(tid)
            if t0 is None:
                continue
            lo = bisect.bisect_left(parent_end_ts, t0 - float(reconnect_gap))
            hi = bisect.bisect_right(parent_end_ts, t0)
            if lo >= hi:
                continue
            sx, sy = start_xy_by_tid.get(tid, (None, None))
            if sx is None:
                continue
            best_parent = None
            best_dist = None
            for j in range(lo, hi):
                pid = parent_sorted[j]
                if pid in used_parents:
                    continue
                ex, ey = end_xy_by_tid.get(pid, (None, None))
                if ex is None:
                    continue
                gap_sec = max(0.0, float(t0) - float(end_ts_by_tid.get(pid, t0)))
                dist = _effective_reconnect_distance(
                    tail_meta_by_tid.get(pid, []),
                    head_meta_by_tid.get(tid, []),
                    gap_sec,
                )
                if not np.isfinite(dist):
                    dist = float(np.hypot(float(ex) - float(sx), float(ey) - float(sy)))
                if dist > float(reconnect_dist):
                    continue
                if best_dist is None or dist < best_dist:
                    best_dist = dist
                    best_parent = pid
            if best_parent is None:
                continue

            used_parents.add(best_parent)
            events_by_tid[best_parent].extend(events_by_tid.get(tid, []))
            events_by_tid.pop(tid, None)
            cls_by_tid.pop(tid, None)
            start_ts_by_tid.pop(tid, None)
            start_xy_by_tid.pop(tid, None)
            head_xy_by_tid.pop(tid, None)
            head_meta_by_tid.pop(tid, None)

            if end_ts_by_tid.get(tid, -1e18) > end_ts_by_tid.get(best_parent, -1e18):
                end_ts_by_tid[best_parent] = end_ts_by_tid.get(tid, end_ts_by_tid[best_parent])
                end_xy_by_tid[best_parent] = end_xy_by_tid.get(tid, end_xy_by_tid[best_parent])
                tail_xy_by_tid[best_parent] = tail_xy_by_tid.get(tid, tail_xy_by_tid.get(best_parent, []))
                tail_meta_by_tid[best_parent] = tail_meta_by_tid.get(tid, tail_meta_by_tid.get(best_parent, []))

            end_ts_by_tid.pop(tid, None)
            end_xy_by_tid.pop(tid, None)
            tail_xy_by_tid.pop(tid, None)
            tail_meta_by_tid.pop(tid, None)
            merged += 1

        if merged == 0:
            break
        log(f"[count] reconnect pass={pass_idx+1} merged={merged}")

    if extrap_horizon and float(extrap_horizon) > 0:
        new_hits = 0
        shown = 0
        max_detail = 50
        for tid, ev in list(events_by_tid.items()):
            if len(ev) > 1:
                continue

            head = head_xy_by_tid.get(tid) or []
            tail = tail_xy_by_tid.get(tid) or []
            if mode_norm in ("approach", "ap"):
                # 정확한 in/out 판정을 위해 "시작 2점 + 마지막 2점"으로 외삽(재연결 후에도 tail/head만 있으면 계산 가능)
                hx = head_xy_by_tid.get(tid) or []
                tx = tail_xy_by_tid.get(tid) or []
                pseudo = []
                if len(hx) >= 2:
                    pseudo.append([0.0, 0.0, float(hx[0][0]), float(hx[0][1])])
                    pseudo.append([1.0, 0.0, float(hx[1][0]), float(hx[1][1])])
                if len(tx) >= 2:
                    pseudo.append([2.0, 0.0, float(tx[0][0]), float(tx[0][1])])
                    pseudo.append([3.0, 0.0, float(tx[1][0]), float(tx[1][1])])
                head_ev, tail_ev = extrapolate_line_hits_detailed(pseudo, norm_lines_d, float(extrap_horizon))
                head_hit = head_ev[0] if head_ev else None
                tail_hit = tail_ev[0] if tail_ev else None
            else:
                head_hit, tail_hit = extrapolate_line_hits((head + tail) if head and tail else (head or tail), lines, float(extrap_horizon))

            if len(ev) == 0:
                if head_hit and tail_hit and str(head_hit) != str(tail_hit):
                    t0 = float(start_ts_by_tid.get(tid, 0.0))
                    t1 = float(end_ts_by_tid.get(tid, t0))
                    if mode_norm in ("approach", "ap"):
                        hx = head_xy_by_tid.get(tid) or []
                        tx = tail_xy_by_tid.get(tid) or []
                        pseudo = []
                        if len(hx) >= 2:
                            pseudo.append([0.0, 0.0, float(hx[0][0]), float(hx[0][1])])
                            pseudo.append([1.0, 0.0, float(hx[1][0]), float(hx[1][1])])
                        if len(tx) >= 2:
                            pseudo.append([2.0, 0.0, float(tx[0][0]), float(tx[0][1])])
                            pseudo.append([3.0, 0.0, float(tx[1][0]), float(tx[1][1])])
                        head_ev, tail_ev = extrapolate_line_hits_detailed(pseudo, norm_lines_d, float(extrap_horizon))
                        if head_ev and tail_ev:
                            events_by_tid[tid] = [(t0, head_ev[0], head_ev[1], head_ev[2]), (t1, tail_ev[0], tail_ev[1], tail_ev[2])]
                        else:
                            events_by_tid[tid] = [(t0, str(head_hit), "", "unk"), (t1, str(tail_hit), "", "unk")]
                    else:
                        events_by_tid[tid] = [(t0, str(head_hit)), (t1, str(tail_hit))]
                    new_hits += 1
                    if log_cb is not None and shown < max_detail:
                        log(f"[count][extrap] tid={tid} 0hit -> head={head_hit} tail={tail_hit} (t0={t0:.3f}, t1={t1:.3f})")
                        shown += 1
                continue

            t_first = float(sorted(ev, key=lambda x: x[0])[0][0])
            if mode_norm in ("approach", "ap"):
                line_first = str(sorted(ev, key=lambda x: x[0])[0][1])
                bound_first = str(sorted(ev, key=lambda x: x[0])[0][2])
                io_first = str(sorted(ev, key=lambda x: x[0])[0][3])
                new_ev = [(t_first, line_first, bound_first, io_first)]
                hx = head_xy_by_tid.get(tid) or []
                tx = tail_xy_by_tid.get(tid) or []
                pseudo = []
                if len(hx) >= 2:
                    pseudo.append([0.0, 0.0, float(hx[0][0]), float(hx[0][1])])
                    pseudo.append([1.0, 0.0, float(hx[1][0]), float(hx[1][1])])
                if len(tx) >= 2:
                    pseudo.append([2.0, 0.0, float(tx[0][0]), float(tx[0][1])])
                    pseudo.append([3.0, 0.0, float(tx[1][0]), float(tx[1][1])])
                head_ev, tail_ev = extrapolate_line_hits_detailed(pseudo, norm_lines_d, float(extrap_horizon))
                if head_ev and str(head_ev[0]) != line_first:
                    new_ev.append((t_first - 1e-3, head_ev[0], head_ev[1], head_ev[2]))
                if tail_ev and str(tail_ev[0]) != line_first and str(tail_ev[0]) != str(head_ev[0] if head_ev else ""):
                    new_ev.append((t_first + 1e-3, tail_ev[0], tail_ev[1], tail_ev[2]))
            else:
                line_first = str(sorted(ev, key=lambda x: x[0])[0][1])
                new_ev = [(t_first, line_first)]
                if head_hit and str(head_hit) != line_first:
                    new_ev.append((t_first - 1e-3, str(head_hit)))
                if tail_hit and str(tail_hit) != line_first and str(tail_hit) != str(head_hit or ""):
                    new_ev.append((t_first + 1e-3, str(tail_hit)))
            if len(new_ev) > 1:
                events_by_tid[tid] = sorted(new_ev, key=lambda x: x[0])
                new_hits += 1
                if log_cb is not None and shown < max_detail:
                    log(
                        f"[count][extrap] tid={tid} 1hit({line_first}) -> head={head_hit} tail={tail_hit}"
                    )
                    shown += 1
        if new_hits:
            log(f"[count] extrap hits={new_hits}")
            if log_cb is not None and shown >= max_detail:
                log(f"[count] extrap detail logs truncated (max {max_detail})")

    if mode_norm in ("approach", "ap"):
        # 외삽에서 bound/io가 unk로 들어온 경우, 가능한 값이 있을 때만 집계(unk는 그대로 라벨에 포함)
        # line_id/bound를 보강: lines 정보로 bound가 비어있으면 채워준다.
        id_to_bound: Dict[str, str] = {}
        for ln in lines or []:
            lid = ln.get("id") or ln.get("name")
            if lid:
                id_to_bound[str(lid)] = str(ln.get("bound") or "")
        fixed: Dict[str, List[Tuple[float, str, str, str]]] = {}
        for tid, ev in events_by_tid.items():
            ev2 = []
            for t, lid, b, io in ev:
                b2 = str(b or id_to_bound.get(str(lid), "") or "")
                ev2.append((float(t), str(lid), b2, str(io)))
            fixed[str(tid)] = ev2
        counts_final = _count_approach_from_events(fixed, cls_by_tid, interval_sec)
    else:
        counts_final = _count_from_events(events_by_tid, cls_by_tid, interval_sec)
    return counts_multi, counts_final


def compute_crossings(traj: pd.DataFrame, lines: List[Dict]) -> pd.DataFrame:
    """DB의 roi_id(라인교차 결과)는 쓰지 않고, 궤적 점과 lines.json을 비교해 교차 이벤트를 계산."""
    rows: List[Dict] = []
    if traj.empty or not lines:
        return pd.DataFrame(columns=["track_id", "ts_sec", "line_name", "cls_name"])

    norm_lines = []
    for ln in lines:
        pts = ln.get("points") or []
        if len(pts) < 2:
            continue
        line_id = ln.get("id") or ln.get("name") or "line"
        norm_lines.append(
            (str(line_id), (float(pts[0][0]), float(pts[0][1])), (float(pts[1][0]), float(pts[1][1])))
        )

    for track_id, g in traj.sort_values("ts_sec").groupby("track_id"):
        pts = g[["x", "y", "ts_sec"]].values.tolist()
        if len(pts) < 2:
            continue
        cls_series = g["cls_name"].dropna()
        cls_name = str(cls_series.iloc[0]) if not cls_series.empty else ""
        last_hit = None
        for i in range(len(pts) - 1):
            x1, y1, _t1 = pts[i]
            x2, y2, t2 = pts[i + 1]
            a1 = (float(x1), float(y1))
            a2 = (float(x2), float(y2))
            for line_id, b1, b2 in norm_lines:
                if segment_intersects(a1, a2, b1, b2):
                    if last_hit == line_id:
                        continue
                    rows.append({"track_id": track_id, "ts_sec": float(t2), "line_name": line_id, "cls_name": cls_name})
                    last_hit = line_id
                    break

    if not rows:
        return pd.DataFrame(columns=["track_id", "ts_sec", "line_name", "cls_name"])
    return pd.DataFrame(rows)


def _count_approach_from_events(
    events_by_tid: Dict[str, List[Tuple[float, str, str, str]]],
    cls_by_tid: Dict[str, str],
    interval_sec: int,
) -> pd.DataFrame:
    """접근로 카운트: 트랙이 라인을 한 번이라도 교차하면 line_id_bound_in/out 기준으로 1회 집계."""
    rows: List[Dict] = []
    for tid, evts in events_by_tid.items():
        if not evts:
            continue
        seen_keys: set[Tuple[str, str, str]] = set()
        for ts, line_id, bound, inout in sorted(evts, key=lambda x: x[0]):
            dedupe_key = (str(line_id), str(bound), str(inout))
            if dedupe_key in seen_keys:
                continue
            seen_keys.add(dedupe_key)
            key = f"{line_id}_{bound}_{inout}".strip("_")
            rows.append(
                {
                    "slot": slot_id(float(ts), interval_sec),
                    "line_from": key,
                    "line_to": "",
                    "cls_name": cls_by_tid.get(tid, ""),
                    "count": 1,
                }
            )
    if not rows:
        return pd.DataFrame(columns=["slot", "line_from", "line_to", "cls_name", "count"])
    return (
        pd.DataFrame(rows)
        .groupby(["slot", "line_from", "line_to", "cls_name"], as_index=False)["count"]
        .sum()
    )


def load_lines_with_scale(lines_path: Path) -> Tuple[List[Dict], float, float]:
    data = json.loads(lines_path.read_text(encoding="utf-8"))
    lines = data.get("lines", [])
    orig_w = float(data.get("image_width") or 0.0)
    orig_h = float(data.get("image_height") or 0.0)
    return lines, orig_w, orig_h


def run_count(
    db_path: Path,
    lines_path: Path,
    interval_min: int = 15,
    reconnect_dist: float = 50.0,
    reconnect_gap: float = 3.0,
    reconnect_passes: int = 2,
    extrap_horizon: float = 200.0,
    out_csv: Optional[Path] = None,
    out_xlsx: Optional[Path] = None,
    resize: Optional[Tuple[int, int]] = None,
    session_id: Optional[str] = None,
    mode: str = "turn",
    log_cb: Optional[Callable[[str], None]] = None,
    use_track_merge: bool = False,
    use_virtual_events: bool = True,
    class_mapping: Optional[Dict[str, str]] = None,
    class_columns: Optional[List[str]] = None,
):
    """차종 매핑을 명시하면 그대로 쓰고, 없으면 기존 자동 판별로 넘어간다.

    class_mapping: {DB에 저장된 차종 이름: 엑셀 집계 열 이름}
    class_columns: 엑셀에 출력할 열 순서
    """

    def log(msg: str) -> None:
        if log_cb is not None:
            log_cb(msg)

    def _probe_analysis_end_sec() -> Optional[float]:
        try:
            with closing(sqlite3.connect(Path(db_path))) as conn, conn:
                sql = "select max(end_ts_ms) from track_trajs"
                params: List[object] = []
                if session_id:
                    sql += " where session_id = ?"
                    params.append(session_id)
                row = conn.execute(sql, tuple(params)).fetchone()
                if row and row[0] is not None:
                    return float(row[0]) / 1000.0
        except Exception:
            logger.debug("Suppressed error", exc_info=True)
            return None
        return None

    interval_sec = interval_min * 60
    analysis_end_sec = _probe_analysis_end_sec()
    lines_raw, orig_w, orig_h = load_lines_with_scale(Path(lines_path))
    # 라인 좌표를 트랙 좌표계(리사이즈 기준)로 스케일
    def _scale_lines(lines: List[Dict]) -> List[Dict]:
        if not resize or orig_w <= 0 or orig_h <= 0:
            return lines
        sx = resize[0] / orig_w
        sy = resize[1] / orig_h
        out = []
        for ln in lines:
            pts = ln.get("points") or []
            scaled = [[p[0] * sx, p[1] * sy] for p in pts]
            ln2 = dict(ln)
            ln2["points"] = scaled
            out.append(ln2)
        return out

    lines = _scale_lines(lines_raw)
    mode_norm = str(mode or "turn").strip().lower()

    if _has_track_trajs(Path(db_path)):
        sessions: List[Optional[str]] = [session_id]
        if not session_id:
            with closing(sqlite3.connect(Path(db_path))) as conn, conn:
                sessions = [
                    str(row[0])
                    for row in conn.execute(
                        "select distinct session_id from track_trajs where session_id is not null order by session_id"
                    )
                    if str(row[0] or "").strip()
                ]
            if not sessions:
                sessions = [None]
            elif len(sessions) > 1:
                log(f"[count] 전체 선택: {len(sessions)}개 세션을 각각 계산한 뒤 합산합니다")

        multi_parts: List[pd.DataFrame] = []
        final_parts: List[pd.DataFrame] = []
        for selected_session in sessions:
            part_multi, part_final = count_track_trajs_streaming(
                db_path=Path(db_path),
                lines=lines,
                interval_sec=interval_sec,
                reconnect_dist=reconnect_dist,
                reconnect_gap=reconnect_gap,
                reconnect_passes=reconnect_passes,
                extrap_horizon=extrap_horizon,
                session_id=selected_session,
                mode=mode_norm,
                log_cb=log_cb,
                use_track_merge=use_track_merge,
                use_virtual_events=use_virtual_events,
            )
            multi_parts.append(part_multi)
            final_parts.append(part_final)

        def _sum_session_parts(parts: List[pd.DataFrame]) -> pd.DataFrame:
            non_empty = [part for part in parts if not part.empty]
            if not non_empty:
                return pd.DataFrame(columns=["slot", "line_from", "line_to", "cls_name", "count"])
            return (
                pd.concat(non_empty, ignore_index=True)
                .groupby(["slot", "line_from", "line_to", "cls_name"], as_index=False)["count"]
                .sum()
                .sort_values(["slot", "line_from", "line_to", "cls_name"])
                .reset_index(drop=True)
            )

        counts_multi = _sum_session_parts(multi_parts)
        counts_final = _sum_session_parts(final_parts)
        log(f"[count] multi-cross rows={len(counts_multi)} final rows={len(counts_final)}")
    else:
        traj = load_traj_table(Path(db_path), session_id=session_id)
        cross = compute_crossings(traj, lines)
        log(f"[count] rows: crossings={len(cross)} traj={len(traj)}")

        cross_sorted = cross.sort_values("ts_sec")
        grouped = cross_sorted.groupby("track_id")

        # 1) 2회 이상 교차
        multi_ids = [tid for tid, g in grouped if len(g) >= 2]
        multi = cross_sorted[cross_sorted.track_id.isin(multi_ids)]
        counts_multi = count_from_pairs(
            multi.groupby("track_id").first().reset_index(),
            multi.groupby("track_id").last().reset_index(),
            interval_sec,
        )

        # 2) 단일 교차 및 미교차 트랙 재연결
        candidate_ids = [tid for tid, g in grouped if len(g) <= 1]
        traj_start, traj_end = endpoints(traj)
        cross_work = cross_sorted.copy()
        candidate_work = set(candidate_ids)
        for _ in range(max(0, reconnect_passes)):
            mapping = attempt_reconnect(candidate_work, cross_work, traj_start, traj_end, reconnect_gap, reconnect_dist)
            if not mapping:
                break
            cross_work = merge_crossings(cross_work, mapping)
            grouped = cross_work.sort_values("ts_sec").groupby("track_id")
            candidate_work = {tid for tid, g in grouped if len(g) <= 1}

        # 3) 외삽으로 2번째 교차 추가 (옵션: extrap_horizon > 0 일 때만)
        grouped = cross_work.sort_values("ts_sec").groupby("track_id")
        still_single = {tid for tid, g in grouped if len(g) == 1}
        still_need = {tid for tid, g in grouped if len(g) <= 1}
        if still_need and extrap_horizon and float(extrap_horizon) > 0:
            traj_pts = {
                tid: g[["x", "y"]].to_numpy().tolist()
                for tid, g in traj.sort_values("ts_sec").groupby("track_id")
            }
            new_rows = []
            start_by_tid = {int(r.track_id): float(r.start_ts) for _, r in traj_start.iterrows()}
            end_by_tid = {int(r.track_id): float(r.end_ts) for _, r in traj_end.iterrows()}
            cls_by_tid_local = {}
            for tid, g in traj.sort_values("ts_sec").groupby("track_id"):
                cls_series = g["cls_name"].dropna()
                cls_by_tid_local[int(tid)] = str(cls_series.iloc[0]) if not cls_series.empty else ""

            shown = 0
            max_detail = 50
            for tid in still_need:
                pts = traj_pts.get(tid, [])
                head_hit, tail_hit = extrapolate_line_hits(pts, lines, extrap_horizon)

                if tid in still_single:
                    existing = grouped.get_group(tid)
                    t0 = float(existing.ts_sec.iloc[0])
                    line0 = str(existing.line_name.iloc[0])
                    cls0 = str(existing.cls_name.iloc[0])
                    if head_hit and str(head_hit) != line0:
                        new_rows.append({"track_id": tid, "ts_sec": t0 - 1e-3, "line_name": str(head_hit), "cls_name": cls0})
                    new_rows.append({"track_id": tid, "ts_sec": t0, "line_name": line0, "cls_name": cls0})
                    if tail_hit and str(tail_hit) != line0 and str(tail_hit) != str(head_hit or ""):
                        new_rows.append({"track_id": tid, "ts_sec": t0 + 1e-3, "line_name": str(tail_hit), "cls_name": cls0})
                    if log_cb is not None and (head_hit or tail_hit) and shown < max_detail:
                        log(f"[count][extrap] tid={tid} 1hit({line0}) -> head={head_hit} tail={tail_hit}")
                        shown += 1
                    continue

                # 미교차(0회): head/tail 둘 다 있으면 2회 교차로 간주
                if head_hit and tail_hit and str(head_hit) != str(tail_hit):
                    t0 = float(start_by_tid.get(tid, 0.0))
                    t1 = float(end_by_tid.get(tid, t0))
                    cls0 = str(cls_by_tid_local.get(tid, ""))
                    new_rows.append({"track_id": tid, "ts_sec": t0, "line_name": str(head_hit), "cls_name": cls0})
                    new_rows.append({"track_id": tid, "ts_sec": t1, "line_name": str(tail_hit), "cls_name": cls0})
                    if log_cb is not None and shown < max_detail:
                        log(f"[count][extrap] tid={tid} 0hit -> head={head_hit} tail={tail_hit} (t0={t0:.3f}, t1={t1:.3f})")
                        shown += 1
            if new_rows:
                cross_work = pd.concat([cross_work, pd.DataFrame(new_rows)], ignore_index=True)
                if log_cb is not None and shown >= max_detail:
                    log(f"[count] extrap detail logs truncated (max {max_detail})")

        # 최종 카운트
        grouped = cross_work.sort_values("ts_sec").groupby("track_id")
        multi_ids_final = [tid for tid, g in grouped if len(g) >= 2]
        multi_final = cross_work[cross_work.track_id.isin(multi_ids_final)]
        counts_final = count_from_pairs(
            multi_final.groupby("track_id").first().reset_index(),
            multi_final.groupby("track_id").last().reset_index(),
            interval_sec,
        )

        if log_cb is not None:
            log(f"[count] multi-cross rows={len(counts_multi)} final rows={len(counts_final)}")
        else:
            print("[초기 2회 이상 교차 카운트]")
            print(counts_multi.head())
            print("\n[재연결/외삽 후 최종 카운트]")
            print(counts_final.head())

    counts_out = counts_final

    def _slot_label(slot_sec: float) -> str:
        start_min = int(float(slot_sec) // 60)
        end_min = start_min + int(interval_min)

        # 마지막 슬롯이 15분을 못 채우면(대부분의 마지막 구간) 실제 분석시간(분/초)을 표시
        if analysis_end_sec and analysis_end_sec > 0:
            last_slot_sec = float(int(float(analysis_end_sec) // float(interval_sec)) * int(interval_sec))
            if abs(float(slot_sec) - last_slot_sec) < 1e-6:
                remainder = float(analysis_end_sec) - last_slot_sec
                if 0 < remainder < float(interval_sec) - 1e-6:
                    mm = int(remainder // 60.0)
                    ss = int(round(remainder - mm * 60.0))
                    if ss >= 60:
                        mm += 1
                        ss -= 60
                    return f"{mm}분{ss}초"

        return f"{start_min}~{end_min}분"

    def _write_excel(path: Path) -> None:
        # 커스텀 best.pt 계열: 8클래스 -> 엑셀 집계(6그룹)로 표시명 통일
        custom_cls_map = {
            "passenger_car": "승용차",
            "small_bus": "소형버스",
            "large_bus": "대형버스",
            "small_truck": "소형화물",
            "medium_truck": "중형화물",
            "etc": "중형화물",
            "large_truck": "대형화물",
            # Some older DBs already store mapped Korean class names.
            "승용차": "승용차",
            "소형버스": "소형버스",
            "대형버스": "대형버스",
            "소형화물": "소형화물",
            "중형화물": "중형화물",
            "대형화물": "대형화물",
        }
        custom_cls_cols = ["승용차", "소형버스", "대형버스", "소형화물", "중형화물", "대형화물"]

        # 공식 YOLO 계열: 원본 분류를 일반 차량군으로 유지
        yolo_cls_map = {
            "car": "승용차",
            "bus": "버스",
            "truck": "화물",
            "motorcycle": "이륜차",
            "bicycle": "자전거",
            # Support already-translated generic labels if they were persisted as-is.
            "승용차": "승용차",
            "버스": "버스",
            "화물": "화물",
            "이륜차": "이륜차",
            "자전거": "자전거",
        }
        yolo_cls_cols = ["승용차", "버스", "화물", "이륜차", "자전거"]

        df = counts_out.copy()
        raw_cls = df["cls_name"].astype(str).str.strip()
        raw_cls_norm = raw_cls.str.lower()
        custom_hits = int(raw_cls.isin(custom_cls_map.keys()).sum()) + int(raw_cls_norm.isin(custom_cls_map.keys()).sum())
        yolo_hits = int(raw_cls.isin(yolo_cls_map.keys()).sum()) + int(raw_cls_norm.isin(yolo_cls_map.keys()).sum())

        if class_mapping:
            # UI/설정에서 지정한 매핑을 그대로 사용한다(모델 프로파일 기준).
            cls_map = {str(k).strip(): str(v).strip() for k, v in class_mapping.items() if str(v).strip()}
            # 소문자 키도 함께 넣어 대소문자 차이로 누락되지 않게 한다.
            cls_map.update({k.lower(): v for k, v in list(cls_map.items())})
            if class_columns:
                fixed_cls_cols = [str(c).strip() for c in class_columns if str(c).strip()]
            else:
                fixed_cls_cols = list(dict.fromkeys(cls_map.values()))
        elif custom_hits > 0 and custom_hits >= yolo_hits:
            cls_map = custom_cls_map
            fixed_cls_cols = custom_cls_cols
        elif yolo_hits > 0:
            cls_map = yolo_cls_map
            fixed_cls_cols = yolo_cls_cols
        else:
            cls_map = {}
            fixed_cls_cols = sorted({str(v).strip() for v in df["cls_name"].dropna().tolist() if str(v).strip()})

        df["cls_kor"] = (
            df["cls_name"]
            .astype(str)
            .str.strip()
            .map(cls_map)
            .fillna(
                df["cls_name"]
                .astype(str)
                .str.strip()
                .str.lower()
                .map(cls_map)
            )
            .fillna(df["cls_name"].astype(str).str.strip())
        )
        # 선택된 출력 스키마에 맞는 컬럼만 집계
        if fixed_cls_cols:
            df = df[df["cls_kor"].isin(fixed_cls_cols)].copy()
        if df.empty:
            df = pd.DataFrame(columns=["slot", "line_from", "line_to", "cls_kor", "count"])
        else:
            df = (
                df.groupby(["slot", "line_from", "line_to", "cls_kor"], as_index=False)["count"]
                .sum()
            )

        wb = Workbook()
        ws = wb.active
        ws.title = "counts"

        thin = Side(style="thin", color="2f2f2f")
        border = Border(left=thin, right=thin, top=thin, bottom=thin)
        header_font = Font(bold=True)
        title_font = Font(bold=True, size=12)
        align_center = Alignment(horizontal="center", vertical="center")
        fill_header = PatternFill("solid", fgColor="E8EEF8")

        # 방향별(From->To) 블록으로 출력 (값이 없어도 포맷 유지)
        directions = (
            df[["line_from", "line_to"]]
            .dropna()
            .drop_duplicates()
            .sort_values(["line_from", "line_to"])
            .values.tolist()
        )
        if not directions:
            directions = [["", ""]]

        # 전체 슬롯(값이 없으면 0 슬롯 1개라도 만든다)
        slots_all = sorted({float(s) for s in df["slot"].dropna().unique().tolist()})
        if not slots_all:
            slots_all = [0.0]

        row = 1
        for line_from, line_to in directions:
            part = df[(df["line_from"] == line_from) & (df["line_to"] == line_to)].copy()
            if part.empty:
                pivot = pd.DataFrame(0, index=slots_all, columns=fixed_cls_cols, dtype=int)
            else:
                pivot = (
                    part.pivot_table(index="slot", columns="cls_kor", values="count", aggfunc="sum", fill_value=0)
                    .reindex(index=slots_all, fill_value=0)
                )
            # 지정 컬럼이 없어도 0으로 생성 (항상 고정 포맷 유지)
            for c in fixed_cls_cols:
                if c not in pivot.columns:
                    pivot[c] = 0
            pivot = pivot[fixed_cls_cols]
            pivot["합계"] = pivot[fixed_cls_cols].sum(axis=1)
            ordered_cols = fixed_cls_cols + ["합계"]

            # title
            if str(line_to).strip():
                title = f"{line_from}--> {line_to}".strip()
            else:
                title = str(line_from).strip()
            if not title or title == "-->":
                title = "미분류"
            ws.cell(row=row, column=1, value=title).font = title_font
            ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=2 + len(ordered_cols))
            row += 1

            # header
            ws.cell(row=row, column=1, value="구간").font = header_font
            ws.cell(row=row, column=1).alignment = align_center
            ws.cell(row=row, column=1).fill = fill_header
            ws.cell(row=row, column=1).border = border
            for j, cname in enumerate(ordered_cols, start=2):
                ws.cell(row=row, column=j, value=str(cname)).font = header_font
                ws.cell(row=row, column=j).alignment = align_center
                ws.cell(row=row, column=j).fill = fill_header
                ws.cell(row=row, column=j).border = border
            row += 1

            # rows
            for slot_sec, vals in pivot.iterrows():
                ws.cell(row=row, column=1, value=_slot_label(slot_sec)).alignment = align_center
                ws.cell(row=row, column=1).border = border
                for j, cname in enumerate(ordered_cols, start=2):
                    ws.cell(row=row, column=j, value=int(vals.get(cname, 0))).alignment = align_center
                    ws.cell(row=row, column=j).border = border
                row += 1

            row += 2  # blank

        ws.column_dimensions["A"].width = 14
        wb.save(path)

    def _safe_save_excel(path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            _write_excel(path)
            return path
        except PermissionError:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            for i in range(1, 50):
                alt = path.with_name(f"{path.stem}_{stamp}_{i}{path.suffix}")
                try:
                    _write_excel(alt)
                    return alt
                except PermissionError:
                    continue
            raise

    # 출력 경로 결정: 기본은 xlsx (CSV는 옵션)
    if out_xlsx:
        excel_path = Path(out_xlsx)
    elif out_csv:
        excel_path = Path(out_csv).with_suffix(".xlsx")
    else:
        excel_path = Path(db_path).with_name("counts.xlsx")

    saved_excel = _safe_save_excel(excel_path)
    log(f"[count] saved: {saved_excel}")

    # CSV를 명시적으로 원할 때만 저장(엑셀 호환 위해 utf-8-sig)
    if out_csv and Path(out_csv).suffix.lower() == ".csv":
        Path(out_csv).parent.mkdir(parents=True, exist_ok=True)
        counts_final.to_csv(out_csv, index=False, encoding="utf-8-sig")
        log(f"[count] saved: {out_csv}")

    return saved_excel, counts_final


def main():
    ap = argparse.ArgumentParser(description="Count tracks by line crossings.")
    ap.add_argument("--db", required=True, help="tracks.sqlite 경로")
    ap.add_argument("--lines", required=True, help="lines.json 경로")
    ap.add_argument("--interval-min", type=int, default=15, choices=[5, 15, 30, 60], help="슬롯 길이(분)")
    ap.add_argument("--reconnect-dist", type=float, default=50.0, help="재연결 최대 거리(px 등)")
    ap.add_argument("--reconnect-gap", type=float, default=3.0, help="재연결 최대 시간차(초)")
    ap.add_argument("--reconnect-passes", type=int, default=2, help="단일 교차 재연결 시도 횟수")
    ap.add_argument("--extrap-horizon", type=float, default=200.0, help="외삽 길이")
    ap.add_argument("--mode", type=str, default="turn", choices=["turn", "approach"], help="turn(교차로) / approach(접근로)")
    ap.add_argument("--session-id", default=None, help="집계할 session_id (생략하면 세션별 계산 후 합산)")
    ap.add_argument(
        "--analysis-basis",
        default="original",
        choices=["original", "postprocess"],
        help="original=원본 궤적, postprocess=병합/가상 이벤트 적용",
    )
    ap.add_argument("--out-csv", default=None, help="결과 CSV 저장 경로")
    ap.add_argument("--out-xlsx", default=None, help="결과 Excel 저장 경로")
    args = ap.parse_args()

    out, _ = run_count(
        db_path=Path(args.db),
        lines_path=Path(args.lines),
        interval_min=args.interval_min,
        reconnect_dist=args.reconnect_dist,
        reconnect_gap=args.reconnect_gap,
        reconnect_passes=args.reconnect_passes,
        extrap_horizon=args.extrap_horizon,
        mode=args.mode,
        session_id=args.session_id,
        out_csv=Path(args.out_csv) if args.out_csv else None,
        out_xlsx=Path(args.out_xlsx) if args.out_xlsx else None,
        use_track_merge=args.analysis_basis == "postprocess",
        use_virtual_events=args.analysis_basis == "postprocess",
    )
    print(f"saved: {out}")


if __name__ == "__main__":
    main()
