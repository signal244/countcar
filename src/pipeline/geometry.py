from __future__ import annotations

from typing import Optional, Tuple


Point = Tuple[float, float]


def segment_intersection(
    a1: Point,
    a2: Point,
    b1: Point,
    b2: Point,
) -> Optional[Tuple[float, float, float]]:
    """Return trajectory ratio and intersection coordinates for two segments."""
    ax, ay = a1
    bx, by = a2
    cx, cy = b1
    dx, dy = b2
    denominator = (bx - ax) * (dy - cy) - (by - ay) * (dx - cx)
    if abs(denominator) < 1e-9:
        return None
    t = ((cx - ax) * (dy - cy) - (cy - ay) * (dx - cx)) / denominator
    u = ((cx - ax) * (by - ay) - (cy - ay) * (bx - ax)) / denominator
    if t < -1e-9 or t > 1.0 + 1e-9 or u < -1e-9 or u > 1.0 + 1e-9:
        return None
    return float(t), float(ax + t * (bx - ax)), float(ay + t * (by - ay))


def bound_in_direction(bound: str) -> Point:
    normalized = str(bound or "").strip().lower()
    mapping = {
        "north_bound": (0.0, 1.0),
        "south_bound": (0.0, -1.0),
        "east_bound": (-1.0, 0.0),
        "west_bound": (1.0, 0.0),
    }
    return mapping.get(normalized, (0.0, 0.0))
