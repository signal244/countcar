import json
import sqlite3
import time
import uuid
from bisect import bisect_left, bisect_right
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

from src.db.schema import init_db
from src.db.writer import decode_traj


TrackSummary = Dict[str, object]
Point = Tuple[float, float]
SQLITE_BUSY_TIMEOUT_MS = 15_000


def _connect_rw(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=max(1.0, SQLITE_BUSY_TIMEOUT_MS / 1000.0))
    try:
        conn.execute(f"PRAGMA busy_timeout={int(SQLITE_BUSY_TIMEOUT_MS)}")
    except Exception:
        pass
    return conn


def _unit_vec(a: Point, b: Point) -> Optional[Point]:
    vx = float(b[0] - a[0])
    vy = float(b[1] - a[1])
    norm = float(np.hypot(vx, vy))
    if norm < 1e-9:
        return None
    return (vx / norm, vy / norm)


def _cosine(v1: Optional[Point], v2: Optional[Point]) -> float:
    if v1 is None or v2 is None:
        return 0.0
    return float(v1[0] * v2[0] + v1[1] * v2[1])


def _segment_speed(a_xy: Point, a_ts: float, b_xy: Point, b_ts: float) -> float:
    dt = float(b_ts) - float(a_ts)
    if dt <= 1e-6:
        return 0.0
    dist = float(np.hypot(float(b_xy[0]) - float(a_xy[0]), float(b_xy[1]) - float(a_xy[1])))
    return dist / dt


def _project_point(pt: Point, direction: Optional[Point], distance: float) -> Optional[Point]:
    if direction is None or distance <= 0.0:
        return None
    return (
        float(pt[0]) + (float(direction[0]) * float(distance)),
        float(pt[1]) + (float(direction[1]) * float(distance)),
    )


def _bridge_distance(parent: TrackSummary, child: TrackSummary, gap: float) -> Dict[str, float]:
    px, py = parent["end_xy"]  # type: ignore[misc]
    cx, cy = child["start_xy"]  # type: ignore[misc]
    raw_dist = float(np.hypot(float(cx) - float(px), float(cy) - float(py)))
    if gap <= 1e-6:
        return {
            "raw_dist": raw_dist,
            "dist": raw_dist,
            "tail_pred_dist": raw_dist,
            "head_pred_dist": raw_dist,
            "bridge_pred_dist": raw_dist,
        }

    tail_pred_dist = float("inf")
    head_pred_dist = float("inf")
    bridge_pred_dist = float("inf")
    tail_lane_dist = float("inf")
    head_lane_dist = float("inf")

    parent_tail_speed = float(parent.get("tail_speed") or 0.0)
    child_head_speed = float(child.get("head_speed") or 0.0)
    parent_pred = _project_point((float(px), float(py)), parent.get("tail_vec"), parent_tail_speed * gap)
    child_back = _project_point((float(cx), float(cy)), child.get("head_vec"), -child_head_speed * gap)

    if parent_pred is not None:
        tail_pred_dist = float(np.hypot(float(parent_pred[0]) - float(cx), float(parent_pred[1]) - float(cy)))
    if child_back is not None:
        head_pred_dist = float(np.hypot(float(px) - float(child_back[0]), float(py) - float(child_back[1])))
    if parent_pred is not None and child_back is not None:
        bridge_pred_dist = float(
            np.hypot(float(parent_pred[0]) - float(child_back[0]), float(parent_pred[1]) - float(child_back[1]))
        )
    parent_tail_vec = parent.get("tail_vec")
    child_head_vec = child.get("head_vec")
    if parent_tail_vec is not None:
        dx = float(cx) - float(px)
        dy = float(cy) - float(py)
        along = (dx * float(parent_tail_vec[0])) + (dy * float(parent_tail_vec[1]))
        lateral = abs((dx * float(parent_tail_vec[1])) - (dy * float(parent_tail_vec[0])))
        expected = parent_tail_speed * gap
        tail_lane_dist = lateral + (0.25 * abs(along - expected))
    if child_head_vec is not None:
        dx = float(cx) - float(px)
        dy = float(cy) - float(py)
        along = (dx * float(child_head_vec[0])) + (dy * float(child_head_vec[1]))
        lateral = abs((dx * float(child_head_vec[1])) - (dy * float(child_head_vec[0])))
        expected = child_head_speed * gap
        head_lane_dist = lateral + (0.25 * abs(along - expected))

    effective_dist = min(raw_dist, tail_pred_dist, head_pred_dist, bridge_pred_dist, tail_lane_dist, head_lane_dist)
    return {
        "raw_dist": raw_dist,
        "dist": float(effective_dist),
        "tail_pred_dist": float(tail_pred_dist if np.isfinite(tail_pred_dist) else raw_dist),
        "head_pred_dist": float(head_pred_dist if np.isfinite(head_pred_dist) else raw_dist),
        "bridge_pred_dist": float(bridge_pred_dist if np.isfinite(bridge_pred_dist) else raw_dist),
        "tail_lane_dist": float(tail_lane_dist if np.isfinite(tail_lane_dist) else raw_dist),
        "head_lane_dist": float(head_lane_dist if np.isfinite(head_lane_dist) else raw_dist),
    }


def _dist_point_to_segment(pt: Point, a: Point, b: Point) -> float:
    ax, ay = float(a[0]), float(a[1])
    bx, by = float(b[0]), float(b[1])
    px, py = float(pt[0]), float(pt[1])
    abx = bx - ax
    aby = by - ay
    denom = (abx * abx) + (aby * aby)
    if denom <= 1e-9:
        return float(np.hypot(px - ax, py - ay))
    t = ((px - ax) * abx + (py - ay) * aby) / denom
    t = max(0.0, min(1.0, t))
    qx = ax + (t * abx)
    qy = ay + (t * aby)
    return float(np.hypot(px - qx, py - qy))


def _point_to_polyline_distance(pt: Point, polyline: List[Point]) -> float:
    if len(polyline) < 2:
        return float('inf')
    return min(_dist_point_to_segment(pt, polyline[i], polyline[i + 1]) for i in range(len(polyline) - 1))


def _point_in_polygon(pt: Point, polygon: List[Point]) -> bool:
    if len(polygon) < 3:
        return False
    x, y = float(pt[0]), float(pt[1])
    inside = False
    j = len(polygon) - 1
    for i in range(len(polygon)):
        xi, yi = float(polygon[i][0]), float(polygon[i][1])
        xj, yj = float(polygon[j][0]), float(polygon[j][1])
        intersects = ((yi > y) != (yj > y)) and (
            x < (xj - xi) * (y - yi) / ((yj - yi) if abs(yj - yi) > 1e-12 else 1e-12) + xi
        )
        if intersects:
            inside = not inside
        j = i
    return inside


def _load_gate_context(lines_path: Optional[Path]) -> Dict[str, List[Dict[str, object]]]:
    empty = {"lines": [], "rois": []}
    if not lines_path:
        return empty
    try:
        if not lines_path.exists():
            return empty
        payload = json.loads(lines_path.read_text(encoding="utf-8"))
    except Exception:
        return empty

    lines_out: List[Dict[str, object]] = []
    for idx, ln in enumerate(payload.get("lines", []) or []):
        pts_raw = ln.get("points") or []
        pts: List[Point] = []
        for item in pts_raw:
            if not isinstance(item, (list, tuple)) or len(item) < 2:
                continue
            try:
                pts.append((float(item[0]), float(item[1])))
            except Exception:
                continue
        if len(pts) < 2:
            continue
        lines_out.append(
            {
                "id": str(ln.get("id") or f"line_{idx + 1}"),
                "bound": str(ln.get("bound") or "").strip(),
                "points": pts,
            }
        )

    rois_out: List[Dict[str, object]] = []
    # 병합 전용 ROI는 기존 lines.json의 roi와 분리해서 merge_roi 키만 사용한다.
    for idx, roi in enumerate(payload.get("merge_roi", []) or []):
        pts_raw = roi.get("points") or roi.get("polygon") or []
        pts: List[Point] = []
        for item in pts_raw:
            if not isinstance(item, (list, tuple)) or len(item) < 2:
                continue
            try:
                pts.append((float(item[0]), float(item[1])))
            except Exception:
                continue
        if len(pts) < 3:
            continue
        rois_out.append(
            {
                "id": str(roi.get("id") or roi.get("name") or f"roi_{idx + 1}"),
                "points": pts,
            }
        )
    return {"lines": lines_out, "rois": rois_out}


def _infer_point_context(pt: Point, gate_ctx: Dict[str, List[Dict[str, object]]], max_dist_px: float) -> Dict[str, object]:
    roi_id = ""
    for roi in gate_ctx.get("rois", []):
        try:
            if _point_in_polygon(pt, roi.get("points") or []):
                roi_id = str(roi.get("id") or "")
                break
        except Exception:
            continue

    gate_id = ""
    bound = ""
    gate_dist = float("inf")
    for line in gate_ctx.get("lines", []):
        pts = line.get("points") or []
        try:
            dist = _point_to_polyline_distance(pt, pts)
        except Exception:
            continue
        if dist < gate_dist:
            gate_dist = dist
            gate_id = str(line.get("id") or "")
            bound = str(line.get("bound") or "")

    if gate_dist > float(max_dist_px):
        gate_id = ""
        bound = ""

    return {
        "roi_id": roi_id,
        "gate_id": gate_id,
        "bound": bound,
        "gate_dist": float(gate_dist if np.isfinite(gate_dist) else -1.0),
    }


def _load_track_summaries(
    db_path: Path,
    session_id: str,
    gate_ctx: Optional[Dict[str, List[Dict[str, object]]]] = None,
    gate_match_dist_px: float = 60.0,
    *,
    slot_index: Optional[int] = None,
    slot_interval_ms: int = 15 * 60 * 1000,
    max_tracks: int = 0,
    trail_seconds: float = 0.0,
) -> List[TrackSummary]:
    rows: List[TrackSummary] = []
    gate_ctx = gate_ctx or {"lines": [], "rois": []}
    with closing(sqlite3.connect(db_path)) as conn, conn:
        sql = (
            "select track_id, coalesce(vehicle_type, class_name) as cls_name, start_ts_ms, end_ts_ms, traj, track_len "
            "from track_trajs where traj is not null and session_id = ?"
        )
        params: List[object] = [session_id]
        if slot_index is not None:
            sql += " and (start_ts_ms / ?) = ?"
            params.extend([int(slot_interval_ms), int(slot_index)])
        sql += " order by start_ts_ms, track_id"
        if int(max_tracks) > 0:
            sql += " limit ?"
            params.append(int(max_tracks))
        for track_id, cls_name, start_ts_ms, end_ts_ms, blob, track_len in conn.execute(sql, tuple(params)):
            pts = decode_traj(blob) or []
            if len(pts) < 2:
                continue
            try:
                p0 = pts[0]
                p1 = pts[min(4, len(pts) - 1)]
                pm1 = pts[max(0, len(pts) - 5)]
                pm2 = pts[-1]
                start_xy = (float(p0[2]), float(p0[3]))
                end_xy = (float(pm2[2]), float(pm2[3]))
                head_vec = _unit_vec(start_xy, (float(p1[2]), float(p1[3])))
                tail_vec = _unit_vec((float(pm1[2]), float(pm1[3])), end_xy)
                head_speed = _segment_speed(
                    start_xy,
                    float(p0[1]) / 1000.0,
                    (float(p1[2]), float(p1[3])),
                    float(p1[1]) / 1000.0,
                )
                tail_speed = _segment_speed(
                    (float(pm1[2]), float(pm1[3])),
                    float(pm1[1]) / 1000.0,
                    end_xy,
                    float(pm2[1]) / 1000.0,
                )
                start_ctx = _infer_point_context(start_xy, gate_ctx, gate_match_dist_px)
                end_ctx = _infer_point_context(end_xy, gate_ctx, gate_match_dist_px)
            except Exception:
                continue
            duration_sec = max(0.0, (float(end_ts_ms or 0.0) - float(start_ts_ms or 0.0)) / 1000.0)
            if float(trail_seconds) > 0.0 and duration_sec > float(trail_seconds):
                continue
            boundary_roi_ids: List[str] = []
            try:
                for roi in gate_ctx.get("rois", []) or []:
                    roi_id = str(roi.get("id") or "")
                    poly = roi.get("points") or []
                    if not roi_id or len(poly) < 3:
                        continue
                    start_in = _point_in_polygon(start_xy, poly)
                    end_in = _point_in_polygon(end_xy, poly)
                    if bool(start_in) != bool(end_in):
                        boundary_roi_ids.append(roi_id)
            except Exception:
                boundary_roi_ids = []
            rows.append(
                {
                    "track_id": str(track_id),
                    "cls_name": str(cls_name or ""),
                    "start_ts": float(start_ts_ms or 0.0) / 1000.0,
                    "end_ts": float(end_ts_ms or 0.0) / 1000.0,
                    "start_xy": start_xy,
                    "end_xy": end_xy,
                    "head_vec": head_vec,
                    "tail_vec": tail_vec,
                    "head_speed": float(head_speed),
                    "tail_speed": float(tail_speed),
                    "track_len": int(track_len or len(pts)),
                    "start_gate_id": str(start_ctx.get("gate_id") or ""),
                    "start_bound": str(start_ctx.get("bound") or ""),
                    "start_roi_id": str(start_ctx.get("roi_id") or ""),
                    "end_gate_id": str(end_ctx.get("gate_id") or ""),
                    "end_bound": str(end_ctx.get("bound") or ""),
                    "end_roi_id": str(end_ctx.get("roi_id") or ""),
                    "boundary_roi_ids": boundary_roi_ids,
                    "duration_sec": duration_sec,
                    "traj_points": pts,
                }
            )
    return rows


def _compute_roi_speed_stats_from_tracks(
    tracks: List[TrackSummary],
    gate_ctx: Dict[str, List[Dict[str, object]]],
    *,
    top_percent: float,
    cap_multiplier: float,
) -> Dict[str, Dict[str, float]]:
    roi_speeds: Dict[str, List[float]] = {}
    rois = gate_ctx.get("rois", []) or []
    if not rois:
        return {}
    for track in tracks:
        pts = track.get("traj_points") or []
        if not isinstance(pts, list) or len(pts) < 2:
            continue
        try:
            xyts = [(float(row[2]), float(row[3]), float(row[1])) for row in pts if isinstance(row, list) and len(row) >= 4]
        except Exception:
            continue
        if len(xyts) < 2:
            continue
        for roi in rois:
            roi_id = str(roi.get("id") or "")
            poly = roi.get("points") or []
            inside_pts: List[Tuple[float, float, float]] = []
            for x, y, ts in xyts:
                try:
                    if _point_in_polygon((x, y), poly):
                        inside_pts.append((x, y, ts))
                except Exception:
                    continue
            if len(inside_pts) < 2:
                continue
            dist = 0.0
            for i in range(len(inside_pts) - 1):
                ax, ay, _ = inside_pts[i]
                bx, by, _ = inside_pts[i + 1]
                dist += float(np.hypot(float(bx) - float(ax), float(by) - float(ay)))
            duration = max(0.0, (float(inside_pts[-1][2]) - float(inside_pts[0][2])) / 1000.0)
            if duration <= 0.0 or dist <= 0.0:
                continue
            roi_speeds.setdefault(roi_id, []).append(float(dist / duration))

    stats: Dict[str, Dict[str, float]] = {}
    pct = min(max(float(top_percent), 0.01), 1.0)
    mult = max(float(cap_multiplier), 1.0)
    for roi_id, speeds in roi_speeds.items():
        if not speeds:
            continue
        vals = sorted((float(x) for x in speeds if float(x) > 0.0), reverse=True)
        if not vals:
            continue
        top_n = max(1, int(np.ceil(len(vals) * pct)))
        top_vals = vals[:top_n]
        top_mean = float(np.mean(top_vals))
        stats[roi_id] = {
            "sample_count": float(len(vals)),
            "top_mean_speed": top_mean,
            "speed_cap": top_mean * mult,
        }
    return stats


def _score_candidate(parent: TrackSummary, child: TrackSummary, cfg: Dict[str, object]) -> Optional[Dict[str, float]]:
    max_gap = float(cfg.get("max_gap_sec", 1.2))
    max_dist = float(cfg.get("max_dist_px", 80.0))
    min_direction_cos = float(cfg.get("min_direction_cos", 0.4))
    require_class_match = bool(cfg.get("require_class_match", True))
    use_gate_condition = bool(cfg.get("use_gate_condition", False))
    use_roi_condition = bool(cfg.get("use_roi_condition", False))
    use_roi_speed_cap = bool(cfg.get("use_roi_speed_cap", False))
    require_roi_boundary_cross = bool(cfg.get("require_roi_boundary_cross", False))
    roi_speed_stats = cfg.get("roi_speed_stats") or {}

    gap = float(child["start_ts"]) - float(parent["end_ts"])
    if gap < 0.0 or gap > max_gap:
        return None

    dist_meta = _bridge_distance(parent, child, gap)
    dist = float(dist_meta["dist"])
    if dist > max_dist:
        return None

    parent_cls = str(parent.get("cls_name") or "")
    child_cls = str(child.get("cls_name") or "")
    class_match = 1 if parent_cls and child_cls and parent_cls == child_cls else 0
    class_mismatch = 1 if parent_cls and child_cls and parent_cls != child_cls else 0
    if require_class_match and class_mismatch:
        return None

    direction_score = _cosine(parent.get("tail_vec"), child.get("head_vec"))
    if direction_score < min_direction_cos:
        return None

    boundary_roi_match = 0
    if require_roi_boundary_cross:
        parent_boundary = {str(x) for x in (parent.get("boundary_roi_ids") or []) if str(x)}
        child_boundary = {str(x) for x in (child.get("boundary_roi_ids") or []) if str(x)}
        shared_boundary_rois = parent_boundary & child_boundary
        if not shared_boundary_rois:
            return None
        boundary_roi_match = 1

    candidate_speed = float("inf") if gap <= 1e-9 and dist > 1e-6 else (dist / max(gap, 1e-6))
    roi_speed_cap_hit = 0
    if use_roi_speed_cap:
        parent_roi = str(parent.get("end_roi_id") or "")
        child_roi = str(child.get("start_roi_id") or "")
        if parent_roi and child_roi and parent_roi == child_roi:
            roi_stat = roi_speed_stats.get(parent_roi) or {}
            try:
                speed_cap = float(roi_stat.get("speed_cap") or 0.0)
            except Exception:
                speed_cap = 0.0
            if speed_cap > 0.0 and candidate_speed >= speed_cap:
                roi_speed_cap_hit = 1
                return None

    gate_match = 0
    bound_match = 0
    roi_match = 0
    gate_score = 0.0
    roi_score = 0.0

    if use_gate_condition:
        parent_gate = str(parent.get("end_gate_id") or "")
        child_gate = str(child.get("start_gate_id") or "")
        parent_bound = str(parent.get("end_bound") or "")
        child_bound = str(child.get("start_bound") or "")
        if parent_gate and child_gate:
            if parent_gate == child_gate:
                gate_match = 1
                gate_score += 14.0
            else:
                gate_score -= 6.0
        if parent_bound and child_bound:
            if parent_bound == child_bound:
                bound_match = 1
                gate_score += 6.0
            else:
                gate_score -= 4.0

    if use_roi_condition:
        parent_roi = str(parent.get("end_roi_id") or "")
        child_roi = str(child.get("start_roi_id") or "")
        if parent_roi and child_roi:
            if parent_roi == child_roi:
                roi_match = 1
                roi_score += 12.0
            else:
                roi_score -= 10.0

    gap_score = max(0.0, 1.0 - (gap / max_gap)) * 30.0 if max_gap > 0 else 0.0
    dist_score = max(0.0, 1.0 - (dist / max_dist)) * 30.0 if max_dist > 0 else 0.0
    dir_score = max(0.0, min(1.0, (direction_score + 1.0) / 2.0)) * 25.0
    if class_match:
        cls_score = 8.0
    elif class_mismatch:
        # require_class_match=False일 때만 여기에 도달하며, 불일치는 약한 감점으로 반영한다.
        cls_score = -3.0
    else:
        cls_score = 0.0
    total = gap_score + dist_score + dir_score + cls_score + gate_score + roi_score
    return {
        "score": float(total),
        "gap": float(gap),
        "dist": float(dist),
        "raw_dist": float(dist_meta["raw_dist"]),
        "tail_pred_dist": float(dist_meta["tail_pred_dist"]),
        "head_pred_dist": float(dist_meta["head_pred_dist"]),
        "bridge_pred_dist": float(dist_meta["bridge_pred_dist"]),
        "tail_lane_dist": float(dist_meta["tail_lane_dist"]),
        "head_lane_dist": float(dist_meta["head_lane_dist"]),
        "direction_score": float(direction_score),
        "class_match": float(class_match),
        "class_mismatch": float(class_mismatch),
        "gate_match": float(gate_match),
        "bound_match": float(bound_match),
        "roi_match": float(roi_match),
        "boundary_roi_match": float(boundary_roi_match),
        "candidate_speed": float(candidate_speed if np.isfinite(candidate_speed) else 0.0),
        "roi_speed_cap_hit": float(roi_speed_cap_hit),
    }


def analyze_track_candidates(
    db_path: Path,
    session_id: str,
    *,
    lines_path: Optional[Path] = None,
    slot_index: Optional[int] = None,
    max_tracks: int = 0,
    trail_seconds: float = 0.0,
    max_gap_sec: float = 5.0,
    max_dist_px: float = 350.0,
    min_direction_cos: float = 0.1,
    score_threshold: float = 20.0,
    only_short_tracks: bool = False,
    short_track_max_len: int = 20,
    use_gate_condition: bool = False,
    gate_match_dist_px: float = 180.0,
    use_roi_condition: bool = False,
    require_roi_boundary_cross: bool = False,
    use_roi_speed_cap: bool = True,
    roi_speed_top_percent: float = 0.10,
    roi_speed_cap_multiplier: float = 1.5,
) -> Dict[str, object]:
    init_db(db_path)
    cfg = {
        "max_gap_sec": float(max_gap_sec),
        "max_dist_px": float(max_dist_px),
        "min_direction_cos": float(min_direction_cos),
        "score_threshold": float(score_threshold),
        "require_class_match": False,
        "only_short_tracks": bool(only_short_tracks),
        "short_track_max_len": int(short_track_max_len),
        "use_gate_condition": bool(use_gate_condition),
        "gate_match_dist_px": float(gate_match_dist_px),
        "use_roi_condition": bool(use_roi_condition),
        "require_roi_boundary_cross": bool(require_roi_boundary_cross),
        "use_roi_speed_cap": bool(use_roi_speed_cap),
        "roi_speed_top_percent": float(roi_speed_top_percent),
        "roi_speed_cap_multiplier": float(roi_speed_cap_multiplier),
    }
    gate_ctx = _load_gate_context(lines_path)
    tracks = _load_track_summaries(
        db_path,
        session_id,
        gate_ctx=gate_ctx,
        gate_match_dist_px=float(cfg["gate_match_dist_px"]),
        slot_index=slot_index,
        max_tracks=max_tracks,
        trail_seconds=trail_seconds,
    )
    cfg["roi_speed_stats"] = _compute_roi_speed_stats_from_tracks(
        tracks,
        gate_ctx,
        top_percent=float(cfg["roi_speed_top_percent"]),
        cap_multiplier=float(cfg["roi_speed_cap_multiplier"]),
    )
    tracks = sorted(tracks, key=lambda x: (float(x.get("start_ts", 0.0)), str(x.get("track_id") or "")))
    parents_by_end = sorted(tracks, key=lambda x: float(x.get("end_ts", 0.0)))
    parent_end_values = [float(track.get("end_ts", 0.0)) for track in parents_by_end]

    occlusion_ids: set[str] = set()
    class_instability_ids: set[str] = set()
    pair_rows: List[Dict[str, object]] = []

    for child in tracks:
        is_short = int(child.get("track_len") or 0) <= int(cfg["short_track_max_len"])
        if bool(cfg["only_short_tracks"]) and not is_short:
            continue

        best_parent: Optional[TrackSummary] = None
        best_meta: Optional[Dict[str, float]] = None
        child_start = float(child.get("start_ts", 0.0))
        lower = bisect_left(parent_end_values, child_start - float(cfg["max_gap_sec"]))
        upper = bisect_right(parent_end_values, child_start)
        for parent in parents_by_end[lower:upper]:
            if parent is child:
                continue
            meta = _score_candidate(parent, child, cfg)
            if meta is None:
                continue
            if meta["score"] < float(cfg["score_threshold"]):
                continue
            if best_meta is None or meta["score"] > best_meta["score"]:
                best_parent = parent
                best_meta = meta

        if best_parent is None or best_meta is None:
            continue

        src_id = str(child.get("track_id") or "")
        parent_id = str(best_parent.get("track_id") or "")
        row = {
            "source_track_id": src_id,
            "parent_track_id": parent_id,
            "score": float(best_meta["score"]),
            "gap": float(best_meta["gap"]),
            "dist": float(best_meta["dist"]),
            "direction_score": float(best_meta["direction_score"]),
            "class_mismatch": int(best_meta.get("class_mismatch") or 0),
        }
        pair_rows.append(row)
        if int(best_meta.get("class_mismatch") or 0) > 0:
            class_instability_ids.add(src_id)
            class_instability_ids.add(parent_id)
        else:
            occlusion_ids.add(src_id)
            occlusion_ids.add(parent_id)

    reused_track_ids: set[str] = set()
    per_track_counts: Dict[str, int] = {}
    for tr in tracks:
        tid = str(tr.get("track_id") or "")
        per_track_counts[tid] = int(per_track_counts.get(tid, 0)) + 1
    for tid, cnt in per_track_counts.items():
        if cnt >= 2:
            reused_track_ids.add(str(tid))

    return {
        "occlusion_ids": sorted(occlusion_ids),
        "class_instability_ids": sorted(class_instability_ids),
        "reused_track_ids": sorted(reused_track_ids),
        "candidate_pairs": pair_rows,
        "roi_speed_stats": cfg.get("roi_speed_stats") or {},
    }


def save_manual_track_merge(
    db_path: Path,
    session_id: str,
    source_track_id: str,
    merged_track_id: str,
    *,
    score: float = 999.0,
    time_gap_sec: float = 0.0,
    end_start_dist: float = 0.0,
    direction_score: float = 1.0,
    class_match: int = 0,
) -> Dict[str, object]:
    init_db(db_path)
    created_dt = datetime.now()
    created_at = created_dt.isoformat(timespec="seconds")
    last_exc: Exception | None = None
    for _ in range(3):
        try:
            with _connect_rw(db_path) as conn:
                cur = conn.cursor()
                cur.execute(
                    "delete from track_merge_exclude_manual where session_id = ? and source_track_id = ? and merged_track_id = ?",
                    (session_id, str(source_track_id), str(merged_track_id)),
                )
                cur.execute(
                    "insert or replace into track_merge_map_manual(session_id, source_track_id, merged_track_id, reason, created_at) values(?,?,?,?,?)",
                    (
                        session_id,
                        str(source_track_id),
                        str(merged_track_id),
                        "trajectory_viewer_manual_merge",
                        created_at,
                    ),
                )
                conn.commit()
                last_exc = None
                break
        except sqlite3.OperationalError as exc:
            last_exc = exc
            if "locked" not in str(exc).lower():
                raise
            time.sleep(0.25)
    if last_exc is not None:
        raise last_exc
    return {
        "created_at": created_at,
        "source_track_id": str(source_track_id),
        "merged_track_id": str(merged_track_id),
        "score": float(score),
        "time_gap_sec": float(time_gap_sec),
        "end_start_dist": float(end_start_dist),
        "direction_score": float(direction_score),
        "class_match": int(class_match),
    }


def delete_manual_track_merge(
    db_path: Path,
    session_id: str,
    source_track_id: str,
    merged_track_id: Optional[str] = None,
) -> Dict[str, object]:
    init_db(db_path)
    removed = 0
    created_at = datetime.now().isoformat(timespec="seconds")
    last_exc: Exception | None = None
    for _ in range(3):
        try:
            with _connect_rw(db_path) as conn:
                cur = conn.cursor()
                cur.execute(
                    "delete from track_merge_map_manual where session_id = ? and source_track_id = ?",
                    (session_id, str(source_track_id)),
                )
                removed += int(cur.rowcount or 0)
                if merged_track_id:
                    cur.execute(
                        "insert or replace into track_merge_exclude_manual(session_id, source_track_id, merged_track_id, reason, created_at) values(?,?,?,?,?)",
                        (
                            session_id,
                            str(source_track_id),
                            str(merged_track_id),
                            "trajectory_viewer_manual_unmerge",
                            created_at,
                        ),
                    )
                    removed += 1
                conn.commit()
                last_exc = None
                break
        except sqlite3.OperationalError as exc:
            last_exc = exc
            if "locked" not in str(exc).lower():
                raise
            time.sleep(0.25)
    if last_exc is not None:
        raise last_exc
    return {
        "session_id": str(session_id),
        "source_track_id": str(source_track_id),
        "merged_track_id": str(merged_track_id or ""),
        "removed": int(removed),
    }


def load_effective_track_merge_map(db_path: Path, session_id: Optional[str]) -> Dict[str, str]:
    if not db_path.exists() or not session_id:
        return {}
    init_db(db_path)
    auto_map: Dict[str, str] = {}
    manual_map: Dict[str, str] = {}
    exclude_pairs: set[Tuple[str, str]] = set()
    with closing(sqlite3.connect(db_path)) as conn, conn:
        for table, target in (
            ("track_merge_map_auto", auto_map),
            ("track_merge_map_manual", manual_map),
        ):
            exists = conn.execute(
                "select 1 from sqlite_master where type='table' and name=? limit 1",
                (table,),
            ).fetchone()
            if not exists:
                continue
            for src, dst in conn.execute(
                f"select source_track_id, merged_track_id from {table} where session_id = ?",
                (session_id,),
            ):
                if src and dst:
                    target[str(src)] = str(dst)

        exists = conn.execute(
            "select 1 from sqlite_master where type='table' and name='track_merge_exclude_manual' limit 1"
        ).fetchone()
        if exists:
            for src, dst in conn.execute(
                "select source_track_id, merged_track_id from track_merge_exclude_manual where session_id = ?",
                (session_id,),
            ):
                if src and dst:
                    exclude_pairs.add((str(src), str(dst)))

        # legacy fallback for old DBs: if new auto table is empty, read the old merge table.
        if not auto_map:
            exists = conn.execute(
                "select 1 from sqlite_master where type='table' and name='track_merge_map' limit 1"
            ).fetchone()
            if exists:
                for src, dst in conn.execute(
                    "select source_track_id, merged_track_id from track_merge_map where session_id = ?",
                    (session_id,),
                ):
                    if src and dst:
                        auto_map[str(src)] = str(dst)

    effective: Dict[str, str] = {}
    for src, dst in auto_map.items():
        if (src, dst) in exclude_pairs:
            continue
        effective[src] = dst
    for src, dst in manual_map.items():
        effective[src] = dst

    flattened: Dict[str, str] = {}
    for source in effective:
        current = source
        seen: set[str] = set()
        while current in effective and current not in seen:
            seen.add(current)
            current = str(effective[current])
        if current in seen:
            # Ignore cyclic manual mappings instead of producing unstable IDs.
            continue
        if current != source:
            flattened[source] = current
    return flattened


def run_track_merge(
    db_path: Path,
    session_id: str,
    *,
    lines_path: Optional[Path] = None,
    slot_index: Optional[int] = None,
    max_tracks: int = 0,
    trail_seconds: float = 0.0,
    max_gap_sec: float = 5.0,
    max_dist_px: float = 350.0,
    min_direction_cos: float = 0.1,
    score_threshold: float = 20.0,
    require_class_match: bool = False,
    only_short_tracks: bool = False,
    short_track_max_len: int = 20,
    use_gate_condition: bool = False,
    gate_match_dist_px: float = 180.0,
    use_roi_condition: bool = False,
    require_roi_boundary_cross: bool = False,
    use_roi_speed_cap: bool = True,
    roi_speed_top_percent: float = 0.10,
    roi_speed_cap_multiplier: float = 1.5,
) -> Dict[str, object]:
    init_db(db_path)
    cfg = {
        "max_gap_sec": float(max_gap_sec),
        "max_dist_px": float(max_dist_px),
        "min_direction_cos": float(min_direction_cos),
        "score_threshold": float(score_threshold),
        "require_class_match": bool(require_class_match),
        "only_short_tracks": bool(only_short_tracks),
        "short_track_max_len": int(short_track_max_len),
        "use_gate_condition": bool(use_gate_condition),
        "gate_match_dist_px": float(gate_match_dist_px),
        "use_roi_condition": bool(use_roi_condition),
        "require_roi_boundary_cross": bool(require_roi_boundary_cross),
        "use_roi_speed_cap": bool(use_roi_speed_cap),
        "roi_speed_top_percent": float(roi_speed_top_percent),
        "roi_speed_cap_multiplier": float(roi_speed_cap_multiplier),
        "lines_path": str(lines_path) if lines_path else "",
    }
    gate_ctx = _load_gate_context(lines_path)
    tracks = _load_track_summaries(
        db_path,
        session_id,
        gate_ctx=gate_ctx,
        gate_match_dist_px=float(cfg["gate_match_dist_px"]),
        slot_index=slot_index,
        max_tracks=max_tracks,
        trail_seconds=trail_seconds,
    )
    cfg["roi_speed_stats"] = _compute_roi_speed_stats_from_tracks(
        tracks,
        gate_ctx,
        top_percent=float(cfg["roi_speed_top_percent"]),
        cap_multiplier=float(cfg["roi_speed_cap_multiplier"]),
    )
    tracks = sorted(tracks, key=lambda x: (float(x.get("start_ts", 0.0)), str(x.get("track_id") or "")))
    total_tracks = len(tracks)

    roots: List[TrackSummary] = []
    active_roots: List[TrackSummary] = []
    merge_rows: List[Dict[str, object]] = []

    for track in tracks:
        track_start = float(track.get("start_ts", 0.0))
        oldest_allowed = track_start - float(cfg["max_gap_sec"])
        active_roots = [
            root for root in active_roots if float(root.get("end_ts", 0.0)) >= oldest_allowed
        ]
        is_short = int(track.get("track_len") or 0) <= int(cfg["short_track_max_len"])
        should_try_merge = (not bool(cfg["only_short_tracks"])) or is_short
        best_parent: Optional[TrackSummary] = None
        best_meta: Optional[Dict[str, float]] = None

        if should_try_merge:
            for root in active_roots:
                meta = _score_candidate(root, track, cfg)
                if meta is None:
                    continue
                if meta["score"] < float(cfg["score_threshold"]):
                    continue
                if best_meta is None or meta["score"] > best_meta["score"]:
                    best_parent = root
                    best_meta = meta

        if best_parent is None or best_meta is None:
            roots.append(track)
            active_roots.append(track)
            continue

        merge_rows.append(
            {
                "source_track_id": str(track["track_id"]),
                "merged_track_id": str(best_parent["track_id"]),
                "merge_score": float(best_meta["score"]),
                "time_gap_sec": float(best_meta["gap"]),
                "end_start_dist": float(best_meta["dist"]),
                "direction_score": float(best_meta["direction_score"]),
                "class_match": int(best_meta["class_match"]),
            }
        )

        if float(track["end_ts"]) >= float(best_parent["end_ts"]):
            best_parent["end_ts"] = track["end_ts"]
            best_parent["end_xy"] = track["end_xy"]
            best_parent["tail_vec"] = track["tail_vec"]
            best_parent["track_len"] = int(best_parent.get("track_len") or 0) + int(track.get("track_len") or 0)
            best_parent["end_gate_id"] = track.get("end_gate_id")
            best_parent["end_bound"] = track.get("end_bound")
            best_parent["end_roi_id"] = track.get("end_roi_id")

    root_track_count = len(roots)
    merged_count = len(merge_rows)
    merge_ratio = (float(merged_count) / float(total_tracks)) if total_tracks > 0 else 0.0
    cfg["summary_total_tracks"] = int(total_tracks)
    cfg["summary_root_tracks"] = int(root_track_count)
    cfg["summary_merged_count"] = int(merged_count)
    cfg["summary_merge_ratio"] = float(merge_ratio)

    created_dt = datetime.now()
    run_id = f"{created_dt.strftime('merge_%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
    created_at = created_dt.isoformat(timespec="seconds")
    with closing(sqlite3.connect(db_path)) as conn, conn:
        cur = conn.cursor()
        cur.execute("delete from track_merge_map_auto where session_id = ?", (session_id,))
        cur.execute(
            "insert into track_merge_runs(run_id, session_id, db_path, merged_count, params_json, created_at) values(?,?,?,?,?,?)",
            (run_id, session_id, str(db_path), merged_count, json.dumps(cfg, ensure_ascii=False), created_at),
        )
        for row in merge_rows:
            cur.execute(
                "insert or replace into track_merge_map_auto(run_id, session_id, source_track_id, merged_track_id, merge_score, time_gap_sec, end_start_dist, direction_score, class_match, created_at) values(?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    session_id,
                    row["source_track_id"],
                    row["merged_track_id"],
                    row["merge_score"],
                    row["time_gap_sec"],
                    row["end_start_dist"],
                    row["direction_score"],
                    row["class_match"],
                    created_at,
                ),
            )
        conn.commit()

    return {
        "run_id": run_id,
        "session_id": session_id,
        "merged_count": merged_count,
        "created_at": created_at,
        "total_tracks": total_tracks,
        "root_track_count": root_track_count,
        "merge_ratio": merge_ratio,
        "params": cfg,
        "roi_speed_stats": cfg.get("roi_speed_stats") or {},
    }


def get_track_merge_status(db_path: Path, session_id: str) -> Optional[Dict[str, object]]:
    if not db_path.exists() or not session_id:
        return None
    init_db(db_path)
    with closing(sqlite3.connect(db_path)) as conn, conn:
        row = conn.execute(
            "select run_id, merged_count, created_at, params_json from track_merge_runs where session_id = ? order by created_at desc limit 1",
            (session_id,),
        ).fetchone()
        if not row:
            return None
        run_id, merged_count, created_at, params_json = row
        try:
            params = json.loads(str(params_json or "{}"))
        except Exception:
            params = {}
        return {
            "run_id": str(run_id),
            "merged_count": int(merged_count or 0),
            "created_at": str(created_at or ""),
            "total_tracks": int(params.get("summary_total_tracks", 0) or 0),
            "root_track_count": int(params.get("summary_root_tracks", 0) or 0),
            "merge_ratio": float(params.get("summary_merge_ratio", 0.0) or 0.0),
            "params": params,
        }
