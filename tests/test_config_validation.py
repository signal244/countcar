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


if __name__ == "__main__":
    unittest.main()
