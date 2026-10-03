"""A/B 탐지 결과 DB 두 개의 궤적 유지 지표를 비교한다.

    python colab/ab_compare.py --a A.sqlite --b B.sqlite --lines "config/lines/경원교/경원교(3방향).json"

Colab 과 로컬(Windows) 어디서나 실행된다.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import Counter
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.db.trajectories import iter_trajectories  # noqa: E402
from src.pipeline.count_tracks import (  # noqa: E402
    _cross_events_from_pts,
    _normalize_lines,
    load_lines_with_scale,
)

BIG_TYPES = {"대형버스", "소형버스", "대형화물", "중형화물", "large_bus", "small_bus", "large_truck", "medium_truck"}
SHORT_SEC = 2.0


def analyze(db_path: Path, norm_lines, t0: float | None, t1: float | None) -> dict:
    tracks = short = c0 = c1 = c2 = 0
    big_c1 = big_c2 = 0
    pairs: Counter = Counter()
    with closing(sqlite3.connect(str(db_path))) as conn:
        for _sess, _cam, _tid, cls, pts in iter_trajectories(conn, None):
            if not pts:
                continue
            start = float(pts[0][1]) / 1000.0
            if (t0 is not None and start < t0) or (t1 is not None and start >= t1):
                continue
            tracks += 1
            if (float(pts[-1][1]) - float(pts[0][1])) / 1000.0 < SHORT_SEC:
                short += 1
            events = _cross_events_from_pts(pts, norm_lines)
            n = len(events)
            big = cls in BIG_TYPES
            if n == 0:
                c0 += 1
            elif n == 1:
                c1 += 1
                big_c1 += big
            else:
                c2 += 1
                big_c2 += big
                pairs[(events[0][1], events[-1][1])] += 1
    crossed = c1 + c2
    return {
        "tracks": tracks,
        "short": short,
        "c0": c0,
        "c1": c1,
        "c2": c2,
        "broken_pct": 100.0 * c1 / crossed if crossed else 0.0,
        "big_broken_pct": 100.0 * big_c1 / (big_c1 + big_c2) if (big_c1 + big_c2) else 0.0,
        "pairs": pairs,
    }


def report(a: dict, b: dict) -> str:
    rows = [
        ("전체 트랙 수", "tracks", "낮을수록 덜 끊김"),
        (f"{SHORT_SEC:.0f}초 미만 조각 트랙", "short", "낮을수록 좋음"),
        ("라인 미통과 트랙", "c0", "참고"),
        ("라인 1개만 통과 (끊긴 궤적)", "c1", "낮을수록 좋음"),
        ("라인 2개 이상 통과 (완결 궤적)", "c2", "높을수록 좋음"),
    ]
    out = [f"{'지표':<28}{'A':>9}{'B':>9}{'변화':>9}   비고", "-" * 72]
    for label, key, note in rows:
        out.append(f"{label:<28}{a[key]:>9}{b[key]:>9}{b[key] - a[key]:>+9}   {note}")
    for label, key in (("끊긴 궤적 비율 %", "broken_pct"), ("  └ 대형차만 %", "big_broken_pct")):
        out.append(f"{label:<28}{a[key]:>9.1f}{b[key]:>9.1f}{b[key] - a[key]:>+9.1f}   낮을수록 좋음")
    out += ["", "방향별 완결 궤적 수 (첫 라인 -> 마지막 라인)", "-" * 72]
    for key in sorted(set(a["pairs"]) | set(b["pairs"]), key=lambda k: -(a["pairs"][k] + b["pairs"][k])):
        va, vb = a["pairs"][key], b["pairs"][key]
        out.append(f"{key[0] + ' -> ' + key[1]:<28}{va:>9}{vb:>9}{vb - va:>+9}")
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--a", required=True, type=Path)
    ap.add_argument("--b", required=True, type=Path)
    ap.add_argument("--lines", required=True, type=Path)
    ap.add_argument("--start-sec", type=float, default=None, help="이 시각 이후 시작한 트랙만")
    ap.add_argument("--end-sec", type=float, default=None, help="이 시각 이전 시작한 트랙만")
    ap.add_argument("--out", type=Path, default=None, help="결과를 텍스트 파일로도 저장")
    args = ap.parse_args()

    lines, _w, _h = load_lines_with_scale(args.lines)
    norm_lines = _normalize_lines(lines)
    a = analyze(args.a, norm_lines, args.start_sec, args.end_sec)
    b = analyze(args.b, norm_lines, args.start_sec, args.end_sec)
    text = report(a, b)
    print(text)
    if args.out:
        args.out.write_text(text + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
