import logging
import math
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from src.db.schema import init_db
from src.db.writer import decode_traj
from src.pipeline.geometry import bound_in_direction as _bound_in_dir
from src.pipeline.geometry import segment_intersection as _segment_intersection

EXTRAP_EXTENSION_PX = 50.0
EXTRAP_POST_SOURCE_MAX_DIST_PX = 120.0
EXTRAP_POST_SOURCE_MAX_POINTS = 12
EXTRAP_FORWARD_MIN_COS = 0.65
EXTRAP_TAIL_POINTS = 20



logger = logging.getLogger(__name__)

def _line_segments(points: List) -> List[Tuple[Tuple[float, float], Tuple[float, float]]]:
    out: List[Tuple[Tuple[float, float], Tuple[float, float]]] = []
    prev: Optional[Tuple[float, float]] = None
    for raw in points or []:
        try:
            x = float(raw[0])
            y = float(raw[1])
        except Exception:
            continue
        cur = (x, y)
        if prev is not None and (abs(cur[0] - prev[0]) > 1e-9 or abs(cur[1] - prev[1]) > 1e-9):
            out.append((prev, cur))
        prev = cur
    return out


def _first_intersection_on_segment(
    a1: Tuple[float, float],
    a2: Tuple[float, float],
    segments: Iterable[Tuple[Tuple[float, float], Tuple[float, float]]],
) -> Optional[Tuple[float, float, float]]:
    best: Optional[Tuple[float, float, float]] = None
    for s1, s2 in segments:
        hit = _segment_intersection(a1, a2, s1, s2)
        if hit is None:
            continue
        if best is None or hit[0] < best[0]:
            best = hit
    return best


def _first_intersection_on_segment_with_segment(
    a1: Tuple[float, float],
    a2: Tuple[float, float],
    segments: Iterable[Tuple[Tuple[float, float], Tuple[float, float]]],
) -> Optional[Tuple[float, float, float, Tuple[float, float], Tuple[float, float]]]:
    best: Optional[Tuple[float, float, float, Tuple[float, float], Tuple[float, float]]] = None
    for s1, s2 in segments:
        hit = _segment_intersection(a1, a2, s1, s2)
        if hit is None:
            continue
        cand = (float(hit[0]), float(hit[1]), float(hit[2]), s1, s2)
        if best is None or cand[0] < best[0]:
            best = cand
    return best


def _first_intersection_on_ray(
    origin: Tuple[float, float],
    direction: Tuple[float, float],
    segments: Iterable[Tuple[Tuple[float, float], Tuple[float, float]]],
) -> Optional[Tuple[float, float, float]]:
    ox, oy = origin
    dx, dy = direction
    best: Optional[Tuple[float, float, float]] = None
    for s1, s2 in segments:
        sx1, sy1 = s1
        sx2, sy2 = s2
        seg_dx = sx2 - sx1
        seg_dy = sy2 - sy1
        denom = dx * seg_dy - dy * seg_dx
        if abs(denom) < 1e-9:
            continue
        t = ((sx1 - ox) * seg_dy - (sy1 - oy) * seg_dx) / denom
        u = ((sx1 - ox) * dy - (sy1 - oy) * dx) / denom
        if t < -1e-9 or u < -1e-9 or u > 1.0 + 1e-9:
            continue
        ix = ox + t * dx
        iy = oy + t * dy
        hit = (float(t), float(ix), float(iy))
        if best is None or hit[0] < best[0]:
            best = hit
    return best


def _track_crosses_segments(pts_raw: List[List[float]], segments) -> bool:
    if not pts_raw or len(pts_raw) < 2:
        return False
    for i in range(len(pts_raw) - 1):
        try:
            _f1, _t1, x1, y1 = pts_raw[i]
            _f2, _t2, x2, y2 = pts_raw[i + 1]
        except Exception:
            continue
        a1 = (float(x1), float(y1))
        a2 = (float(x2), float(y2))
        if _first_intersection_on_segment(a1, a2, segments) is not None:
            return True
    return False


def _crossed_line_ids(
    pts_raw: List[List[float]],
    lines: Iterable[Tuple[str, Iterable[Tuple[Tuple[float, float], Tuple[float, float]]]]],
) -> List[str]:
    out: List[str] = []
    last_hit: Optional[str] = None
    if not pts_raw or len(pts_raw) < 2:
        return out
    for i in range(len(pts_raw) - 1):
        try:
            _f1, _t1, x1, y1 = pts_raw[i]
            _f2, _t2, x2, y2 = pts_raw[i + 1]
        except Exception:
            continue
        a1 = (float(x1), float(y1))
        a2 = (float(x2), float(y2))
        for line_id, segments in lines:
            if _first_intersection_on_segment(a1, a2, segments) is None:
                continue
            if last_hit == line_id:
                break
            out.append(str(line_id))
            last_hit = str(line_id)
            break
    return out


def _line_in_normal(points: List, bound: str, in_point: Optional[object] = None) -> Tuple[float, float]:
    segments = _line_segments(points)
    if not segments:
        return (0.0, 0.0)
    p1 = segments[0][0]
    p2 = segments[-1][1]
    dx = float(p2[0] - p1[0])
    dy = float(p2[1] - p1[1])
    nx, ny = (-dy, dx)
    n_norm = float(math.hypot(nx, ny))
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
    return (nx, ny)


def _compute_inout(vx: float, vy: float, n_in: Tuple[float, float]) -> str:
    s = vx * float(n_in[0]) + vy * float(n_in[1])
    if s > 0:
        return "in"
    if s < 0:
        return "out"
    return "unk"


def _line_center(points: List) -> Optional[Tuple[float, float]]:
    pts = _line_segments(points)
    if not pts:
        return None
    xs: List[float] = []
    ys: List[float] = []
    for p1, p2 in pts:
        xs.extend([float(p1[0]), float(p2[0])])
        ys.extend([float(p1[1]), float(p2[1])])
    if not xs or not ys:
        return None
    return (sum(xs) / float(len(xs)), sum(ys) / float(len(ys)))


def _unit_vector(dx: float, dy: float) -> Optional[Tuple[float, float]]:
    norm = float(math.hypot(dx, dy))
    if norm <= 1e-9:
        return None
    return (dx / norm, dy / norm)


def _cosine_similarity(ax: float, ay: float, bx: float, by: float) -> float:
    au = _unit_vector(ax, ay)
    bu = _unit_vector(bx, by)
    if au is None or bu is None:
        return -1.0
    return float(au[0] * bu[0] + au[1] * bu[1])


def _subtrajectory_after_crossing(
    pts_raw: List[List[float]],
    source_lines: Iterable[Tuple[str, Iterable[Tuple[Tuple[float, float], Tuple[float, float]]]]],
) -> List[Tuple[float, float, float]]:
    if not pts_raw or len(pts_raw) < 2:
        return []
    for i in range(len(pts_raw) - 1):
        try:
            _f1, t1, x1, y1 = pts_raw[i]
            _f2, t2, x2, y2 = pts_raw[i + 1]
        except Exception:
            continue
        a1 = (float(x1), float(y1))
        a2 = (float(x2), float(y2))
        for _line_id, segments in source_lines:
            hit = _first_intersection_on_segment(a1, a2, segments)
            if hit is None:
                continue
            ratio, ix, iy = hit
            t_cross = float(t1) + (float(t2) - float(t1)) * float(ratio)
            out: List[Tuple[float, float, float]] = [(float(t_cross), float(ix), float(iy))]
            for j in range(i + 1, len(pts_raw)):
                try:
                    _fj, tj, xj, yj = pts_raw[j]
                    out.append((float(tj), float(xj), float(yj)))
                except Exception:
                    continue
            return out
    return []


def _subtrajectory_after_crossing_context(
    pts_raw: List[List[float]],
    source_lines: Iterable[Tuple[str, Iterable[Tuple[Tuple[float, float], Tuple[float, float]]]]],
) -> Optional[Dict[str, object]]:
    if not pts_raw or len(pts_raw) < 2:
        return None
    for i in range(len(pts_raw) - 1):
        try:
            _f1, t1, x1, y1 = pts_raw[i]
            _f2, t2, x2, y2 = pts_raw[i + 1]
        except Exception:
            continue
        a1 = (float(x1), float(y1))
        a2 = (float(x2), float(y2))
        for line_id, segments in source_lines:
            hit = _first_intersection_on_segment_with_segment(a1, a2, segments)
            if hit is None:
                continue
            ratio, ix, iy, s1, s2 = hit
            t_cross = float(t1) + (float(t2) - float(t1)) * float(ratio)
            out: List[Tuple[float, float, float]] = [(float(t_cross), float(ix), float(iy))]
            for j in range(i + 1, len(pts_raw)):
                try:
                    _fj, tj, xj, yj = pts_raw[j]
                    out.append((float(tj), float(xj), float(yj)))
                except Exception:
                    continue
            return {
                "line_id": str(line_id),
                "cross_t": float(t_cross),
                "cross_x": float(ix),
                "cross_y": float(iy),
                "traj_dx": float(a2[0] - a1[0]),
                "traj_dy": float(a2[1] - a1[1]),
                "segment_p1": (float(s1[0]), float(s1[1])),
                "segment_p2": (float(s2[0]), float(s2[1])),
                "samples": out,
            }
    return None


def _sample_polyline_points(samples: List[Tuple[float, float, float]], sample_count: int = 10) -> List[Tuple[float, float]]:
    if not samples:
        return []
    if len(samples) == 1:
        return [(samples[0][1], samples[0][2])]
    cum_dist: List[float] = [0.0]
    for i in range(1, len(samples)):
        dx = float(samples[i][1] - samples[i - 1][1])
        dy = float(samples[i][2] - samples[i - 1][2])
        cum_dist.append(cum_dist[-1] + float(math.hypot(dx, dy)))
    total = float(cum_dist[-1])
    if total <= 1e-9:
        return [(float(samples[0][1]), float(samples[0][2])), (float(samples[-1][1]), float(samples[-1][2]))]
    out: List[Tuple[float, float]] = []
    target_ds = [total * k / float(max(sample_count - 1, 1)) for k in range(sample_count)]
    idx = 0
    for target_d in target_ds:
        while idx + 1 < len(cum_dist) and cum_dist[idx + 1] < target_d:
            idx += 1
        if idx + 1 >= len(samples):
            out.append((float(samples[-1][1]), float(samples[-1][2])))
            continue
        d0 = float(cum_dist[idx])
        d1 = float(cum_dist[idx + 1])
        if d1 - d0 <= 1e-9:
            out.append((float(samples[idx + 1][1]), float(samples[idx + 1][2])))
            continue
        r = (target_d - d0) / (d1 - d0)
        x = float(samples[idx][1]) + (float(samples[idx + 1][1]) - float(samples[idx][1])) * r
        y = float(samples[idx][2]) + (float(samples[idx + 1][2]) - float(samples[idx][2])) * r
        out.append((x, y))
    return out


def _trim_initial_subtrajectory(
    samples: List[Tuple[float, float, float]],
    max_dist_px: float = EXTRAP_POST_SOURCE_MAX_DIST_PX,
    max_points: int = EXTRAP_POST_SOURCE_MAX_POINTS,
) -> List[Tuple[float, float, float]]:
    if not samples:
        return []
    out: List[Tuple[float, float, float]] = [samples[0]]
    accum = 0.0
    for sample in samples[1:]:
        prev = out[-1]
        step = float(math.hypot(float(sample[1] - prev[1]), float(sample[2] - prev[2])))
        next_accum = accum + step
        if len(out) >= max_points or next_accum > max_dist_px:
            break
        out.append(sample)
        accum = next_accum
    if len(out) < 2 and len(samples) >= 2:
        out.append(samples[1])
    return out


def _trend_vector_from_samples(samples: List[Tuple[float, float, float]]) -> Optional[Tuple[float, float]]:
    pts = _sample_polyline_points(samples, sample_count=10)
    if len(pts) < 2:
        return None
    vx = 0.0
    vy = 0.0
    for i in range(1, len(pts)):
        vx += float(pts[i][0] - pts[i - 1][0])
        vy += float(pts[i][1] - pts[i - 1][1])
    return _unit_vector(vx, vy)


def _tail_samples_from_raw(
    pts_raw: List[Tuple[float, float, float, float]],
    tail_points: int = EXTRAP_TAIL_POINTS,
) -> List[Tuple[float, float, float]]:
    tail = list(pts_raw[-tail_points:]) if len(pts_raw) > tail_points else list(pts_raw)
    out: List[Tuple[float, float, float]] = []
    for row in tail:
        try:
            _frame, t_ms, x, y = row
            out.append((float(t_ms), float(x), float(y)))
        except Exception:
            continue
    return out


def _percentile(values: List[float], q: float) -> float:
    if not values:
        return 0.0
    vals = sorted(float(v) for v in values)
    if len(vals) == 1:
        return vals[0]
    pos = max(0.0, min(1.0, float(q))) * float(len(vals) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return vals[lo]
    frac = pos - float(lo)
    return float(vals[lo] * (1.0 - frac) + vals[hi] * frac)


def _top_direction_group_threshold(scores: List[float]) -> float:
    valid = sorted(float(v) for v in scores if math.isfinite(float(v)))
    if not valid:
        return EXTRAP_FORWARD_MIN_COS
    positive = [v for v in valid if v >= EXTRAP_FORWARD_MIN_COS]
    if not positive:
        return max(EXTRAP_FORWARD_MIN_COS, valid[-1])
    vmax = positive[-1]
    q75 = _percentile(positive, 0.75)
    q90 = _percentile(positive, 0.90)
    if len(positive) >= 20:
        thresh = max(EXTRAP_FORWARD_MIN_COS, q75, vmax - 0.12)
    elif len(positive) >= 8:
        thresh = max(EXTRAP_FORWARD_MIN_COS, q90, vmax - 0.15)
    else:
        thresh = max(EXTRAP_FORWARD_MIN_COS, vmax - 0.10)
    return min(thresh, vmax)


def _point_side(px: float, py: float, ox: float, oy: float, nx: float, ny: float) -> float:
    return float((px - ox) * nx + (py - oy) * ny)


def load_virtual_events(
    db_path: Path,
    session_id: Optional[str] = None,
    detailed: bool = False,
) -> Dict[str, List[Tuple]]:
    out: Dict[str, List[Tuple]] = {}
    if not db_path.exists():
        return out
    with closing(sqlite3.connect(db_path)) as conn, conn:
        row = conn.execute(
            "select 1 from sqlite_master where type='table' and name='track_virtual_events'"
        ).fetchone()
        if not row:
            return out
        sql = (
            "select track_id, ts_ms, line_id, bound, inout "
            "from track_virtual_events where 1=1"
        )
        params: List[object] = []
        if session_id is not None:
            sql += " and coalesce(session_id, '') = ?"
            params.append(session_id)
        for tid, ts_ms, line_id, bound, inout in conn.execute(sql, tuple(params)):
            tid_s = str(tid)
            ts_sec = float(ts_ms or 0) / 1000.0
            if detailed:
                ev = (ts_sec, str(line_id or ""), str(bound or ""), str(inout or "unk"))
            else:
                ev = (ts_sec, str(line_id or ""))
            out.setdefault(tid_s, []).append(ev)
    return out


def _load_single_cross_source_map(
    db_path: Path,
    source_line_ids: Iterable[str],
    target_line_ids: Iterable[str],
    session_id: Optional[str] = None,
) -> Optional[Dict[str, Tuple[str, str]]]:
    source_ids = {str(x) for x in source_line_ids if str(x)}
    target_ids = {str(x) for x in target_line_ids if str(x)}
    if not source_ids:
        return {}
    if not db_path.exists():
        return None
    with closing(sqlite3.connect(db_path)) as conn, conn:
        row = conn.execute(
            "select 1 from sqlite_master where type='table' and name='track_line_summary'"
        ).fetchone()
        if not row:
            return None
        placeholders = ",".join("?" for _ in source_ids)
        sql = (
            f"select track_id, first_line_id, first_bound, last_line_id, cross_count "
            f"from track_line_summary where cross_count = 1 and first_line_id in ({placeholders})"
        )
        params: List[object] = list(source_ids)
        if session_id:
            sql += " and session_id = ?"
            params.append(str(session_id))
        out: Dict[str, Tuple[str, str]] = {}
        for track_id, first_line_id, first_bound, last_line_id, cross_count in conn.execute(sql, tuple(params)):
            if int(cross_count or 0) != 1:
                continue
            first_id = str(first_line_id or "")
            last_id = str(last_line_id or "")
            if first_id not in source_ids or last_id not in source_ids:
                continue
            if target_ids and (first_id in target_ids or last_id in target_ids):
                continue
            out[str(track_id)] = (first_id, str(first_bound or ""))
        return out


def _load_detected_source_candidates(
    db_path: Path,
    source_line_ids: Iterable[str],
    target_line_ids: Iterable[str],
    session_id: Optional[str] = None,
) -> Optional[set[str]]:
    source_map = _load_single_cross_source_map(
        db_path=db_path,
        source_line_ids=source_line_ids,
        target_line_ids=target_line_ids,
        session_id=session_id,
    )
    if source_map is None:
        return None
    return set(source_map.keys())


def load_direction_group_candidates(
    db_path: Path,
    lines: List[Dict],
    source_line_ids: Iterable[str],
    target_line_ids: Iterable[str],
    session_id: Optional[str] = None,
    tail_points: int = EXTRAP_TAIL_POINTS,
) -> Optional[Dict[str, object]]:
    source_line_ids_set = {str(x) for x in source_line_ids if str(x)}
    target_line_ids_set = {str(x) for x in target_line_ids if str(x)}
    if not source_line_ids_set or not target_line_ids_set:
        return {
            "threshold": EXTRAP_FORWARD_MIN_COS,
            "selected_track_ids": set(),
            "score_map": {},
            "candidate_count": 0,
            "positive_count": 0,
        }
    candidate_source_map = _load_single_cross_source_map(
        Path(db_path),
        source_line_ids_set,
        target_line_ids_set,
        session_id=session_id,
    )
    candidate_target_map = _load_single_cross_source_map(
        Path(db_path),
        target_line_ids_set,
        source_line_ids_set,
        session_id=session_id,
    )
    if candidate_source_map is None or candidate_target_map is None:
        return None
    candidate_track_ids = set(candidate_source_map.keys()) | set(candidate_target_map.keys())
    if not candidate_track_ids:
        return {
            "threshold": EXTRAP_FORWARD_MIN_COS,
            "selected_track_ids": set(),
            "source_selected_track_ids": set(),
            "target_selected_track_ids": set(),
            "score_map": {},
            "candidate_count": 0,
            "positive_count": 0,
        }

    all_lines: List[Tuple[str, List[Tuple[Tuple[float, float], Tuple[float, float]]]]] = []
    source_line_meta: Dict[str, Dict[str, object]] = {}
    source_centers: List[Tuple[float, float]] = []
    target_centers: List[Tuple[float, float]] = []
    for ln in lines or []:
        lid = str(ln.get("id") or ln.get("name") or "")
        bound = str(ln.get("bound") or "")
        pts = ln.get("points") or []
        segments = _line_segments(pts)
        if not segments:
            continue
        all_lines.append((lid, segments))
        center = _line_center(pts)
        if lid in source_line_ids_set:
            source_line_meta[lid] = {"bound": bound, "center": center}
            if center is not None:
                source_centers.append(center)
        if lid in target_line_ids_set and center is not None:
            target_centers.append(center)
    ref_dir: Optional[Tuple[float, float]] = None
    if source_centers and target_centers:
        sx = sum(x for x, _ in source_centers) / float(len(source_centers))
        sy = sum(y for _, y in source_centers) / float(len(source_centers))
        tx = sum(x for x, _ in target_centers) / float(len(target_centers))
        ty = sum(y for _, y in target_centers) / float(len(target_centers))
        ref_dir = _unit_vector(tx - sx, ty - sy)
    if ref_dir is None:
        return {
            "threshold": EXTRAP_FORWARD_MIN_COS,
            "selected_track_ids": set(),
            "score_map": {},
            "candidate_count": 0,
            "positive_count": 0,
        }

    score_map: Dict[str, Dict[str, object]] = {}
    with closing(sqlite3.connect(db_path)) as conn, conn:
        sql = "select track_id, traj from track_trajs where traj is not null"
        params: List[object] = []
        if session_id:
            sql += " and session_id = ?"
            params.append(str(session_id))
        placeholders = ",".join("?" for _ in candidate_track_ids)
        sql += f" and track_id in ({placeholders})"
        params.extend(sorted(candidate_track_ids))
        for tid, blob in conn.execute(sql, tuple(params)):
            pts_raw = decode_traj(blob) or []
            if len(pts_raw) < 2:
                continue
            tid_s = str(tid)
            role = ""
            matched_source = ""
            matched_bound = ""
            if tid_s in candidate_source_map:
                matched_source, matched_bound = candidate_source_map.get(tid_s) or ("", "")
                role = "source"
            elif tid_s in candidate_target_map:
                matched_source, matched_bound = candidate_target_map.get(tid_s) or ("", "")
                role = "target"
            if not matched_source:
                continue
            tail_pts = _tail_samples_from_raw(pts_raw, tail_points=tail_points)
            trend = _trend_vector_from_samples(tail_pts)
            if trend is None:
                continue
            score = _cosine_similarity(float(trend[0]), float(trend[1]), float(ref_dir[0]), float(ref_dir[1]))
            meta = source_line_meta.get(matched_source) or {"bound": matched_bound}
            score_map[tid_s] = {
                "line_id": matched_source,
                "bound": str(meta.get("bound") or ""),
                "score": float(score),
                "role": role,
            }

    positive_source_scores = [
        float(meta.get("score") or 0.0)
        for meta in score_map.values()
        if str(meta.get("role") or "") == "source" and float(meta.get("score") or 0.0) >= EXTRAP_FORWARD_MIN_COS
    ]
    positive_target_scores = [
        float(meta.get("score") or 0.0)
        for meta in score_map.values()
        if str(meta.get("role") or "") == "target" and float(meta.get("score") or 0.0) >= EXTRAP_FORWARD_MIN_COS
    ]
    threshold_source = _top_direction_group_threshold(positive_source_scores)
    threshold_target = _top_direction_group_threshold(positive_target_scores)
    source_selected_track_ids = {
        tid for tid, meta in score_map.items()
        if str(meta.get("role") or "") == "source" and float(meta.get("score") or 0.0) >= threshold_source
    }
    target_selected_track_ids = {
        tid for tid, meta in score_map.items()
        if str(meta.get("role") or "") == "target" and float(meta.get("score") or 0.0) >= threshold_target
    }
    selected_track_ids = set(source_selected_track_ids) | set(target_selected_track_ids)
    return {
        "threshold": float(min(threshold_source, threshold_target) if positive_source_scores and positive_target_scores else max(threshold_source, threshold_target)),
        "selected_track_ids": selected_track_ids,
        "source_selected_track_ids": source_selected_track_ids,
        "target_selected_track_ids": target_selected_track_ids,
        "score_map": score_map,
        "candidate_count": len(score_map),
        "positive_count": len(positive_source_scores) + len(positive_target_scores),
        "source_candidate_count": len(candidate_source_map),
        "target_candidate_count": len(candidate_target_map),
        "source_positive_count": len(positive_source_scores),
        "target_positive_count": len(positive_target_scores),
        "threshold_source": float(threshold_source),
        "threshold_target": float(threshold_target),
    }


def save_extrapolated_line_events(
    db_path: Path,
    lines: List[Dict],
    source_line_ids: Iterable[str],
    target_line_ids: Iterable[str],
    session_id: Optional[str] = None,
    log_cb=None,
    persist: bool = True,
    excluded_track_ids: Optional[Iterable[str]] = None,
) -> Dict[str, object]:
    def log(msg: str) -> None:
        if log_cb is not None:
            log_cb(msg)

    init_db(Path(db_path))
    source_line_ids_set = {str(x) for x in source_line_ids if str(x)}
    target_line_ids_set = {str(x) for x in target_line_ids if str(x)}
    excluded_track_ids_set = {str(x) for x in (excluded_track_ids or []) if str(x)}
    if not source_line_ids_set or not target_line_ids_set:
        return {"tracks": 0, "matched_source": 0, "inserted": 0, "skipped_hit": 0, "skipped_nohit": 0, "line_counts": {}, "track_ids": [], "segments": []}

    all_lines: List[Tuple[str, List[Tuple[Tuple[float, float], Tuple[float, float]]]]] = []
    source_lines: List[Tuple[str, List[Tuple[Tuple[float, float], Tuple[float, float]]]]] = []
    target_lines: List[Tuple[str, str, List[Tuple[Tuple[float, float], Tuple[float, float]]], Tuple[float, float]]] = []
    source_centers: List[Tuple[float, float]] = []
    target_centers: List[Tuple[float, float]] = []
    source_line_meta: Dict[str, Dict[str, object]] = {}
    for ln in lines or []:
        lid = str(ln.get("id") or ln.get("name") or "")
        bound = str(ln.get("bound") or "")
        pts = ln.get("points") or []
        segments = _line_segments(pts)
        if not segments:
            continue
        all_lines.append((lid, segments))
        center = _line_center(pts)
        if lid in source_line_ids_set and center is not None:
            source_centers.append(center)
        if lid in source_line_ids_set:
            source_lines.append((lid, segments))
            source_line_meta[lid] = {
                "bound": str(bound or ""),
                "normal": tuple(float(v) for v in _line_in_normal(pts, bound, ln.get("in_point"))),
                "center": center,
            }
        if lid not in target_line_ids_set:
            continue
        n_in = _line_in_normal(pts, bound, ln.get("in_point"))
        target_lines.append((lid, bound, segments, n_in))
        if center is not None:
            target_centers.append(center)

    if not target_lines:
        return {"tracks": 0, "matched_source": 0, "inserted": 0, "skipped_hit": 0, "skipped_nohit": 0, "line_counts": {}, "track_ids": [], "segments": []}

    ref_dir: Optional[Tuple[float, float]] = None
    if source_centers and target_centers:
        sx = sum(x for x, _y in source_centers) / float(len(source_centers))
        sy = sum(y for _x, y in source_centers) / float(len(source_centers))
        tx = sum(x for x, _y in target_centers) / float(len(target_centers))
        ty = sum(y for _x, y in target_centers) / float(len(target_centers))
        ref_dir = _unit_vector(tx - sx, ty - sy)

    matched_source = 0
    inserted = 0
    skipped_hit = 0
    skipped_nohit = 0
    processed = 0
    now = datetime.now().isoformat(timespec="seconds")
    line_counts: Dict[str, int] = {}
    track_ids: List[str] = []
    preview_segments: List[Dict[str, object]] = []
    group_info = load_direction_group_candidates(
        db_path=Path(db_path),
        lines=lines,
        source_line_ids=source_line_ids_set,
        target_line_ids=target_line_ids_set,
        session_id=session_id,
        tail_points=EXTRAP_TAIL_POINTS,
    )
    candidate_track_ids = None if group_info is None else set(group_info.get("selected_track_ids") or set())
    if group_info is not None:
        log(
            "[extrap] direction-group"
            f" candidates={int(group_info.get('candidate_count') or 0)}"
            f" positive={int(group_info.get('positive_count') or 0)}"
            f" selected={len(candidate_track_ids)}"
            f" threshold={float(group_info.get('threshold') or 0.0):.3f}"
        )
    selected_score_map = {} if group_info is None else dict(group_info.get("score_map") or {})

    with closing(sqlite3.connect(db_path)) as conn, conn:
        sql = (
            "select session_id, camera_id, track_id, traj "
            "from track_trajs where traj is not null"
        )
        params: List[object] = []
        if session_id:
            sql += " and session_id = ?"
            params.append(session_id)
        if candidate_track_ids is not None:
            if not candidate_track_ids:
                return {
                    "tracks": 0,
                    "matched_source": 0,
                    "inserted": 0,
                    "skipped_hit": 0,
                    "skipped_nohit": 0,
                    "line_counts": {},
                    "track_ids": [],
                    "segments": [],
                    "persisted": bool(persist),
                    "source_line_ids": sorted(source_line_ids_set),
                    "target_line_ids": sorted(target_line_ids_set),
                }
            placeholders = ",".join("?" for _ in candidate_track_ids)
            sql += f" and track_id in ({placeholders})"
            params.extend(sorted(candidate_track_ids))

        for sess, cam, tid, blob in conn.execute(sql, tuple(params)):
            pts_raw = decode_traj(blob) or []
            if len(pts_raw) < 2:
                continue
            if str(tid) in excluded_track_ids_set:
                continue
            processed += 1
            if processed % 500 == 0:
                log(f"[extrap] processed={processed}")

            crossed_ids: List[str] = []
            matched_source_info = selected_score_map.get(str(tid)) if selected_score_map else None
            if matched_source_info is not None:
                matched_source_id = str(matched_source_info.get("line_id") or "")
                if matched_source_id not in source_line_ids_set:
                    continue
                crossed_ids = [matched_source_id]
            else:
                crossed_ids = _crossed_line_ids(pts_raw, all_lines)
                if len(crossed_ids) != 1 or crossed_ids[0] not in source_line_ids_set:
                    continue
            matched_source += 1

            # Skip if already crosses target line.
            already = False
            for lid, _bound, segments, _n_in in target_lines:
                if _track_crosses_segments(pts_raw, segments):
                    already = True
                    break
            if already:
                skipped_hit += 1
                continue

            cross_ctx = _subtrajectory_after_crossing_context(pts_raw, source_lines)
            if not cross_ctx:
                skipped_nohit += 1
                continue
            sub_pts = list(cross_ctx.get("samples") or [])
            cross_x = float(cross_ctx.get("cross_x") or 0.0)
            cross_y = float(cross_ctx.get("cross_y") or 0.0)
            cross_line_id = str(cross_ctx.get("line_id") or "")
            traj_dx = float(cross_ctx.get("traj_dx") or 0.0)
            traj_dy = float(cross_ctx.get("traj_dy") or 0.0)
            seg_p1 = cross_ctx.get("segment_p1")
            seg_p2 = cross_ctx.get("segment_p2")
            if (
                len(sub_pts) < 2
                or not isinstance(seg_p1, tuple)
                or not isinstance(seg_p2, tuple)
                or len(seg_p1) != 2
                or len(seg_p2) != 2
            ):
                skipped_nohit += 1
                continue

            source_meta = source_line_meta.get(cross_line_id) or {}
            source_normal = source_meta.get("normal")
            cross_inout = "unk"
            expected_inout = "unk"
            cross_score = 1.0
            if isinstance(source_normal, tuple) and len(source_normal) == 2:
                cross_inout = _compute_inout(traj_dx, traj_dy, (float(source_normal[0]), float(source_normal[1])))
                if ref_dir is not None:
                    expected_inout = _compute_inout(float(ref_dir[0]), float(ref_dir[1]), (float(source_normal[0]), float(source_normal[1])))
            if ref_dir is not None:
                cross_score = _cosine_similarity(float(traj_dx), float(traj_dy), float(ref_dir[0]), float(ref_dir[1]))
                if cross_score < EXTRAP_FORWARD_MIN_COS:
                    skipped_nohit += 1
                    continue

            seg_dx = float(seg_p2[0] - seg_p1[0])
            seg_dy = float(seg_p2[1] - seg_p1[1])
            local_normal = _unit_vector(-seg_dy, seg_dx)
            if local_normal is not None and target_centers:
                tx = sum(x for x, _y in target_centers) / float(len(target_centers))
                ty = sum(y for _x, y in target_centers) / float(len(target_centers))
                target_side_sign = _point_side(tx, ty, cross_x, cross_y, float(local_normal[0]), float(local_normal[1]))
                if abs(target_side_sign) > 1e-9:
                    filtered_sub_pts: List[Tuple[float, float, float]] = []
                    for t_val, px, py in sub_pts:
                        side = _point_side(float(px), float(py), cross_x, cross_y, float(local_normal[0]), float(local_normal[1]))
                        if side * target_side_sign >= -1e-6:
                            filtered_sub_pts.append((float(t_val), float(px), float(py)))
                    if len(filtered_sub_pts) >= 2:
                        sub_pts = filtered_sub_pts

            tail_pts = _tail_samples_from_raw(pts_raw)
            if len(tail_pts) < 2:
                skipped_nohit += 1
                continue

            progress_score = 1.0
            if ref_dir is not None:
                move_dx = float(tail_pts[-1][1] - tail_pts[0][1])
                move_dy = float(tail_pts[-1][2] - tail_pts[0][2])
                progress_score = _cosine_similarity(move_dx, move_dy, float(ref_dir[0]), float(ref_dir[1]))
                if progress_score < EXTRAP_FORWARD_MIN_COS:
                    skipped_nohit += 1
                    continue

            trend = _trend_vector_from_samples(tail_pts)
            if trend is None:
                skipped_nohit += 1
                continue
            dir_score = 1.0
            if ref_dir is not None:
                dir_score = _cosine_similarity(float(trend[0]), float(trend[1]), float(ref_dir[0]), float(ref_dir[1]))
                if dir_score < EXTRAP_FORWARD_MIN_COS:
                    skipped_nohit += 1
                    continue

            prev_t, prev_x, prev_y = tail_pts[-2]
            cur_t, src_x, src_y = tail_pts[-1]
            seg_len = float(math.hypot(float(src_x - prev_x), float(src_y - prev_y)))
            ux, uy = float(trend[0]), float(trend[1])
            candidates: List[Tuple[float, str, str, float, float, float, float, int, float, float, str, str]] = []
            for lid, bound, segments, n_in in target_lines:
                hit = _first_intersection_on_ray((float(src_x), float(src_y)), (ux, uy), segments)
                if hit is None:
                    continue
                _ratio, ix, iy = hit
                end_x = float(ix + ux * EXTRAP_EXTENSION_PX)
                end_y = float(iy + uy * EXTRAP_EXTENSION_PX)
                dist = float(math.hypot(ix - float(src_x), iy - float(src_y)))
                dt_ms = float(cur_t - prev_t)
                extra_ms = dt_ms * (dist / seg_len) if seg_len > 1e-6 and dt_ms > 1e-6 else 0.0
                ts_ms = int(round(float(cur_t) + extra_ms))
                inout = _compute_inout(ux, uy, n_in)
                candidates.append(
                    (
                        dist,
                        str(lid),
                        str(bound or ""),
                        float(ix),
                        float(iy),
                        float(end_x),
                        float(end_y),
                        ts_ms,
                        float(src_x),
                        float(src_y),
                        str(inout),
                        "extrap_track_endpoint",
                    )
                )

            if not candidates:
                skipped_nohit += 1
                continue

            _dist, lid, bound, ix, iy, end_x, end_y, ts_ms, src_x, src_y, inout, method = min(candidates, key=lambda row: row[0])
            if persist:
                conn.execute(
                    """
                    insert or replace into track_virtual_events
                    (session_id, camera_id, track_id, ts_ms, line_id, bound, inout, method, horizon, src_point_x, src_point_y, hit_x, hit_y, created_at, extra)
                    values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(sess or ""),
                        str(cam or ""),
                        str(tid),
                        int(ts_ms),
                        str(lid),
                        str(bound or ""),
                        str(inout),
                        str(method),
                        0.0,
                        float(src_x),
                        float(src_y),
                        float(end_x),
                        float(end_y),
                        now,
                        "",
                    ),
                )
            inserted += 1
            line_counts[str(lid)] = int(line_counts.get(str(lid), 0)) + 1
            track_ids.append(str(tid))
            preview_segments.append(
                {
                    "track_id": str(tid),
                    "line_id": str(lid),
                    "bound": str(bound or ""),
                    "src_x": float(src_x),
                    "src_y": float(src_y),
                    "hit_x": float(end_x),
                    "hit_y": float(end_y),
                    "cross_x": float(ix),
                    "cross_y": float(iy),
                    "method": str(method),
                    "dir_score": float(dir_score),
                    "progress_score": float(progress_score),
                    "cross_score": float(cross_score),
                    "crossed_ids": list(crossed_ids),
                    "cross_inout": str(cross_inout),
                    "expected_inout": str(expected_inout),
                }
            )

        if persist:
            conn.commit()

    return {
        "tracks": int(processed),
        "matched_source": int(matched_source),
        "inserted": int(inserted),
        "skipped_hit": int(skipped_hit),
        "skipped_nohit": int(skipped_nohit),
        "line_counts": dict(line_counts),
        "track_ids": list(track_ids),
        "segments": list(preview_segments),
        "persisted": bool(persist),
        "source_line_ids": sorted(source_line_ids_set),
        "target_line_ids": sorted(target_line_ids_set),
    }
