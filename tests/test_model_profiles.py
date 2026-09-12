import json
import tempfile
import unittest
from pathlib import Path

from src.config.model_profiles import (
    ModelProfile,
    ModelProfileStore,
    suggest_detect_class_ids,
    suggest_excel_mapping,
)
from src.services.colab_export import build_colab_config, is_colab_path, to_colab_path

# 실제 모델에서 읽은 클래스 목록 (번호 순서가 category_mapping.json 과 다르다)
BEST_PT_CLASSES = {
    0: "person",
    1: "small_bus",
    2: "passenger_car",
    3: "medium_truck",
    4: "large_truck",
    5: "large_bus",
    6: "etc",
    7: "small_truck",
}
COCO_CLASSES = {
    0: "person",
    1: "bicycle",
    2: "car",
    3: "motorcycle",
    4: "airplane",
    5: "bus",
    6: "train",
    7: "truck",
    8: "boat",
    15: "cat",
    56: "chair",
}


class ExcelMappingTests(unittest.TestCase):
    def test_custom_model_maps_to_six_columns(self) -> None:
        mapping, columns = suggest_excel_mapping(list(BEST_PT_CLASSES.values()))
        self.assertEqual(columns, ["승용차", "소형버스", "대형버스", "소형화물", "중형화물", "대형화물"])
        # etc 와 medium_truck 은 같은 열로 합쳐진다.
        self.assertEqual(mapping["etc"], "중형화물")
        self.assertEqual(mapping["medium_truck"], "중형화물")
        self.assertNotIn("person", mapping)

    def test_coco_model_maps_car_bus_truck_to_three_columns(self) -> None:
        """YOLO의 car/bus/truck 3분류를 그대로 반영한다."""
        mapping, columns = suggest_excel_mapping(list(COCO_CLASSES.values()))
        self.assertEqual(columns, ["승용차", "버스", "화물"])
        self.assertEqual(mapping["car"], "승용차")
        self.assertEqual(mapping["bus"], "버스")
        self.assertEqual(mapping["truck"], "화물")
        self.assertNotIn("chair", mapping)

    def test_coco_two_wheelers_are_not_default_columns(self) -> None:
        mapping, columns = suggest_excel_mapping(list(COCO_CLASSES.values()))
        self.assertNotIn("이륜차", columns)
        self.assertNotIn("motorcycle", mapping)
        self.assertNotIn("bicycle", mapping)

    def test_unknown_model_keeps_original_names(self) -> None:
        mapping, columns = suggest_excel_mapping(["ev_car", "scooter"])
        self.assertEqual(mapping, {"ev_car": "ev_car", "scooter": "scooter"})
        self.assertEqual(columns, ["ev_car", "scooter"])


class DetectClassDefaultTests(unittest.TestCase):
    def test_coco_defaults_to_vehicles_only(self) -> None:
        """COCO 80클래스에서 새/의자까지 탐지 대상이 되면 안 된다."""
        mapping, _ = suggest_excel_mapping(list(COCO_CLASSES.values()))
        ids = suggest_detect_class_ids(COCO_CLASSES, mapping)
        self.assertEqual(ids, [1, 2, 3, 5, 7])
        names = [COCO_CLASSES[i] for i in ids]
        self.assertNotIn("airplane", names)
        self.assertNotIn("train", names)
        self.assertNotIn("chair", names)

    def test_two_wheelers_are_detected_even_though_not_counted(self) -> None:
        """집계 기본 열에 없어도 탐지는 해 둔다.

        탐지에서 빼면 DB에 남지 않아 나중에 재집계로 되돌릴 수 없다.
        """
        mapping, columns = suggest_excel_mapping(list(COCO_CLASSES.values()))
        ids = suggest_detect_class_ids(COCO_CLASSES, mapping)
        detected = [COCO_CLASSES[i] for i in ids]
        self.assertIn("motorcycle", detected)
        self.assertIn("bicycle", detected)
        self.assertNotIn("이륜차", columns)

    def test_custom_model_selects_all_vehicle_classes(self) -> None:
        mapping, _ = suggest_excel_mapping(list(BEST_PT_CLASSES.values()))
        ids = suggest_detect_class_ids(BEST_PT_CLASSES, mapping)
        self.assertEqual(ids, [1, 2, 3, 4, 5, 6, 7])
        self.assertNotIn(0, ids)  # person 제외

    def test_falls_back_when_no_mapping(self) -> None:
        ids = suggest_detect_class_ids({0: "person", 1: "ev_car"}, None)
        self.assertEqual(ids, [1])


class ProfileStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store_path = Path(self._tmp.name) / "model_profiles.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_profile_round_trips_through_disk(self) -> None:
        store = ModelProfileStore(self.store_path)
        profile = ModelProfile(
            key="best.pt",
            path="models/best.pt",
            classes=BEST_PT_CLASSES,
            detect_class_ids=[1, 2],
            excel_mapping={"passenger_car": "승용차"},
            excel_columns=["승용차"],
        )
        store.put(profile)

        reloaded = ModelProfileStore(self.store_path).get_cached("models/best.pt")
        self.assertIsNotNone(reloaded)
        self.assertEqual(reloaded.detect_class_ids, [1, 2])
        self.assertEqual(reloaded.excel_columns, ["승용차"])
        self.assertEqual(reloaded.classes[2], "passenger_car")

    def test_profile_is_keyed_by_filename_not_folder(self) -> None:
        store = ModelProfileStore(self.store_path)
        store.put(ModelProfile(key="best.pt", detect_class_ids=[3]))
        self.assertIsNotNone(store.get_cached(r"D:\other\place\best.pt"))

    def test_missing_model_does_not_raise(self) -> None:
        store = ModelProfileStore(self.store_path)
        profile = store.get("models/does_not_exist.pt")
        self.assertFalse(profile.has_classes)


class ColabExportTests(unittest.TestCase):
    ROOT = r"g:\내 드라이브\vm\count_car_ver6.0"

    def test_relative_path_resolves_against_project_root(self) -> None:
        self.assertEqual(
            to_colab_path("config/lines/설창리.json", self.ROOT),
            "/content/drive/MyDrive/vm/count_car_ver6.0/config/lines/설창리.json",
        )

    def test_windows_drive_path_is_converted(self) -> None:
        self.assertEqual(
            to_colab_path(r"G:\내 드라이브\vm\count_car_ver6.0\models\best.pt", self.ROOT),
            "/content/drive/MyDrive/vm/count_car_ver6.0/models/best.pt",
        )

    def test_existing_colab_path_is_left_alone(self) -> None:
        self.assertEqual(to_colab_path("/content/tracks.sqlite", self.ROOT), "/content/tracks.sqlite")

    def test_path_outside_drive_stays_recognisable(self) -> None:
        """드라이브 밖 경로를 그럴듯한 상대경로로 바꾸면 Colab에서 조용히 실패한다."""
        converted = to_colab_path(r"D:\videos\a.mp4", self.ROOT)
        self.assertEqual(converted, r"D:\videos\a.mp4")
        self.assertFalse(is_colab_path(converted))

    def test_class_settings_are_carried_into_exported_config(self) -> None:
        exported = build_colab_config(
            {"model_path": "models/best.pt", "root_dir": "should_be_dropped"},
            self.ROOT,
            allowed_classes=[2, 1],
            count_class_mapping={"passenger_car": "승용차"},
            count_class_columns=["승용차"],
        )
        self.assertNotIn("root_dir", exported)
        self.assertEqual(exported["allowed_classes"], [1, 2])
        self.assertEqual(exported["count_class_mapping"], {"passenger_car": "승용차"})
        self.assertTrue(is_colab_path(str(exported["model_path"])))
        # 생성된 설정은 그대로 JSON으로 저장 가능해야 한다.
        json.dumps(exported, ensure_ascii=False)


if __name__ == "__main__":
    unittest.main()
