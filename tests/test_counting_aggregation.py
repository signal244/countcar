"""카운팅 집계·방향 회귀 테스트.

기존 test_counting_logic.py 는 교차 판정과 디바운스만 다룬다. 여기서는
시간대·방향·차종별 집계의 핵심 순수 로직을 고정한다.

  - slot_id          : 시간대(슬롯) 버킷팅 - 시간대별 집계의 뼈대
  - _cross_event_records_from_pts 의 inout : 이동 방향 부호로 in/out 판정
  - count_from_pairs : (슬롯, line_from, line_to, 차종)별 건수 집계
"""

import unittest

import pandas as pd

from src.pipeline.count_tracks import (
    _cross_event_records_from_pts,
    _normalize_lines_detailed,
    count_from_pairs,
    slot_id,
)


class SlotIdTests(unittest.TestCase):
    def test_slot_id_buckets_by_interval(self) -> None:
        interval = 900  # 15분
        self.assertEqual(slot_id(0.0, interval), 0)
        self.assertEqual(slot_id(1.0, interval), 0)
        self.assertEqual(slot_id(899.9, interval), 0)
        # 정확히 경계면 다음 슬롯으로 넘어간다.
        self.assertEqual(slot_id(900.0, interval), 900)
        self.assertEqual(slot_id(901.0, interval), 900)
        self.assertEqual(slot_id(1800.5, interval), 1800)

    def test_slot_id_returns_slot_start_not_index(self) -> None:
        # 슬롯 '번호'가 아니라 슬롯 시작 '초'를 반환한다.
        self.assertEqual(slot_id(1234.0, 600), 1200)


class CrossDirectionTests(unittest.TestCase):
    """세로 라인(x=0), bound=west_bound 이면 in 방향 n_in=(+1,0)."""

    def _lines(self):
        return _normalize_lines_detailed(
            [{"id": "gate", "points": [[0.0, -100.0], [0.0, 100.0]], "bound": "west_bound"}]
        )

    def test_moving_positive_x_is_in(self) -> None:
        # x: -10 -> +10 (오른쪽 이동), n_in=(+1,0) 과 같은 방향 => in
        pts = [[0, 0, -10.0, 0.0], [1, 100, 10.0, 0.0]]
        events = _cross_event_records_from_pts(pts, self._lines())
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["inout"], "in")

    def test_moving_negative_x_is_out(self) -> None:
        # x: +10 -> -10 (왼쪽 이동), n_in 반대 방향 => out
        pts = [[0, 0, 10.0, 0.0], [1, 100, -10.0, 0.0]]
        events = _cross_event_records_from_pts(pts, self._lines())
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["inout"], "out")


class CountFromPairsTests(unittest.TestCase):
    def _make(self, rows_first, rows_last):
        first = pd.DataFrame(
            rows_first, columns=["track_id", "line_name", "ts_sec", "cls_name"]
        )
        last = pd.DataFrame(rows_last, columns=["track_id", "line_name"])
        return count_from_pairs(first, last, interval_sec=900)

    def test_same_slot_from_to_class_aggregates(self) -> None:
        # 두 트랙 모두 같은 슬롯(0~900), A->B, car => count 2 인 한 줄.
        out = self._make(
            rows_first=[(1, "A", 10.0, "car"), (2, "A", 500.0, "car")],
            rows_last=[(1, "B"), (2, "B")],
        )
        self.assertEqual(len(out), 1)
        row = out.iloc[0]
        self.assertEqual(int(row["count"]), 2)
        self.assertEqual(row["line_from"], "A")
        self.assertEqual(row["line_to"], "B")
        self.assertEqual(row["cls_name"], "car")
        self.assertEqual(int(row["slot"]), 0)

    def test_different_class_splits_rows(self) -> None:
        out = self._make(
            rows_first=[(1, "A", 10.0, "car"), (2, "A", 20.0, "truck")],
            rows_last=[(1, "B"), (2, "B")],
        )
        self.assertEqual(len(out), 2)
        self.assertEqual(set(out["cls_name"]), {"car", "truck"})
        self.assertTrue((out["count"] == 1).all())

    def test_slot_assigned_from_first_crossing_time(self) -> None:
        # 첫 교차 시각이 다른 슬롯이면 서로 다른 슬롯으로 분리된다.
        out = self._make(
            rows_first=[(1, "A", 100.0, "car"), (2, "A", 1000.0, "car")],
            rows_last=[(1, "B"), (2, "B")],
        )
        self.assertEqual(set(out["slot"]), {0, 900})


if __name__ == "__main__":
    unittest.main()
