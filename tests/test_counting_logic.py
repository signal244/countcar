import unittest

from src.pipeline.count_tracks import _cross_event_records_from_pts, _normalize_lines_detailed
from src.pipeline.geometry import bound_in_direction, segment_intersection


class CountingGeometryTests(unittest.TestCase):
    def test_segment_intersection_includes_endpoint(self) -> None:
        hit = segment_intersection((0.0, 0.0), (10.0, 0.0), (10.0, 0.0), (10.0, 10.0))
        self.assertIsNotNone(hit)
        assert hit is not None
        self.assertAlmostEqual(hit[1], 10.0)

    def test_bound_direction(self) -> None:
        self.assertEqual(bound_in_direction("west_bound"), (1.0, 0.0))
        self.assertEqual(bound_in_direction("east_bound"), (-1.0, 0.0))

    def test_line_jitter_is_debounced(self) -> None:
        lines = _normalize_lines_detailed(
            [
                {
                    "id": "gate",
                    "points": [[0.0, -100.0], [0.0, 100.0]],
                    "bound": "west_bound",
                }
            ]
        )
        points = [
            [0, 0, -10.0, 0.0],
            [1, 100, 10.0, 0.0],
            [2, 200, -10.0, 0.0],
            [3, 300, 10.0, 0.0],
        ]
        events = _cross_event_records_from_pts(points, lines)
        self.assertEqual(len(events), 1)


if __name__ == "__main__":
    unittest.main()
