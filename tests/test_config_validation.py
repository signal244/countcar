import unittest

from src.config.validation import validate_app_config, validate_line_settings


class ConfigValidationTests(unittest.TestCase):
    def test_valid_app_config(self) -> None:
        report = validate_app_config(
            {
                "model_path": "models/best.pt",
                "db_path": "output/tracks.sqlite",
                "confidence_threshold": 0.2,
                "target_fps": 10,
                "yolo_imgsz": 1280,
                "max_idle_frames": 30,
                "allowed_classes": [1, 2, 3],
            }
        )
        self.assertTrue(report.ok, report.errors)

    def test_invalid_threshold_is_rejected(self) -> None:
        report = validate_app_config(
            {"model_path": "model.pt", "db_path": "tracks.sqlite", "confidence_threshold": 1.5}
        )
        self.assertFalse(report.ok)

    def test_line_bound_warning_and_duplicate_id_error(self) -> None:
        report = validate_line_settings(
            {
                "image_width": 100,
                "image_height": 100,
                "roi": [],
                "lines": [
                    {"id": "A", "points": [[0, 0], [0, 100]], "bound": ""},
                    {"id": "A", "points": [[100, 0], [100, 100]], "bound": "east_bound"},
                ],
            }
        )
        self.assertFalse(report.ok)
        self.assertTrue(any("중복" in message for message in report.errors))
        self.assertTrue(any("bound" in message for message in report.warnings))


class NumericValidationTests(unittest.TestCase):
    def test_nonfinite_and_boolean_numbers_are_rejected(self):
        for key in ("confidence_threshold", "target_fps", "yolo_imgsz", "max_idle_frames", "flush_interval_minutes"):
            for value in (float("nan"), float("inf"), float("-inf"), True):
                with self.subTest(key=key, value=value):
                    cfg = {"model_path": "m.pt", "db_path": "db.sqlite", key: value}
                    self.assertFalse(validate_app_config(cfg).ok)

    def test_class_ids_must_be_nonnegative_integers(self):
        for value in (1.9, -1, True, float("nan"), float("inf"), "1.5"):
            with self.subTest(value=value):
                self.assertFalse(validate_app_config({"model_path": "m.pt", "db_path": "db.sqlite", "allowed_classes": [value]}).ok)
        self.assertTrue(validate_app_config({"model_path": "m.pt", "db_path": "db.sqlite", "allowed_classes": [0, "1", 2.0]}).ok)

    def test_integer_fields_reject_fractional_values(self):
        for key, value in (("yolo_imgsz", 640.5), ("max_idle_frames", 1.9), ("flush_interval_minutes", .5)):
            self.assertFalse(validate_app_config({"model_path": "m.pt", "db_path": "db.sqlite", key: value}).ok)

    def test_points_roi_and_dimensions_must_be_finite(self):
        line = {"id": "A", "points": [[0, 0], [10, 10]], "bound": "west_bound"}
        for key, value in (("image_width", float("nan")), ("image_height", True),
                           ("roi", [[0, 0], [1, 0], [float("inf"), 1]])):
            cfg = {"lines": [line], "image_width": 100, "image_height": 100, key: value}
            self.assertFalse(validate_line_settings(cfg).ok)
        for key in ("points", "in_point"):
            invalid = dict(line)
            invalid[key] = [[0, 0], [float("nan"), 1]] if key == "points" else [0, float("inf")]
            self.assertFalse(validate_line_settings({"lines": [invalid]}).ok)

    def test_optional_resize_requires_both_positive_dimensions(self):
        cfg = {"model_path": "m.pt", "db_path": "db.sqlite", "resize_width": 640}
        self.assertFalse(validate_app_config(cfg).ok)
        cfg["resize_height"] = 480
        self.assertTrue(validate_app_config(cfg).ok)
        cfg["resize_height"] = 0
        self.assertFalse(validate_app_config(cfg).ok)


if __name__ == "__main__":
    unittest.main()
