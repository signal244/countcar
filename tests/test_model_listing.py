import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.export_openvino import input_shape
from src.config import runtime
from src.config.model_profiles import list_model_files
from src.services.colab_export import build_colab_config


class ModelListingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name in ("best.pt", "yolo26n-reid.onnx", "notes.txt", "plain.pt"):
            (self.root / name).write_bytes(b"")
        ov = self.root / "best_int8_openvino_model"
        ov.mkdir()
        (ov / "metadata.yaml").write_text("imgsz:\n- 736\n- 1280\n", encoding="utf-8")
        self.pt = str(self.root / "best.pt")
        self.ov = str(ov)

    def plan(self, mode, device="cpu"):
        with patch.object(runtime, "_auto_device", return_value=device):
            return runtime.plan_runtime(self.pt, mode)

    def test_list_shows_pt_only(self):
        self.assertEqual([p.name for p in list_model_files(self.root)], ["best.pt", "plain.pt"])

    def test_auto_without_gpu_uses_paired_openvino_model(self):
        plan = self.plan("auto", "cpu")
        self.assertTrue(plan.openvino)
        self.assertEqual(plan.model_path, self.ov)
        self.assertEqual(plan.fixed_imgsz, 1280)
        self.assertIn("INT8", plan.label)

    def test_auto_with_gpu_uses_pt(self):
        plan = self.plan("auto", "cuda")
        self.assertEqual((plan.mode, plan.device, plan.model_path, plan.openvino), ("gpu", "cuda", self.pt, False))

    def test_auto_skips_small_gpu(self):
        with patch.object(runtime, "_auto_device", return_value="cuda"), \
                patch.object(runtime, "_gpu_info", return_value=("GTX 750 Ti", 1.0)):
            plan = runtime.plan_runtime(self.pt, "auto")
            self.assertTrue(plan.openvino)
            self.assertIn("GTX 750 Ti", plan.warning)
            self.assertEqual(runtime.plan_runtime(self.pt, "gpu").mode, "gpu")
        with patch.object(runtime, "_auto_device", return_value="cuda"), \
                patch.object(runtime, "_gpu_info", return_value=("RTX 2070 SUPER", 8.0)):
            self.assertEqual(runtime.plan_runtime(self.pt, "auto").mode, "gpu")

    def test_cpu_without_converted_model_warns(self):
        with patch.object(runtime, "_auto_device", return_value="cpu"):
            plan = runtime.plan_runtime(str(self.root / "plain.pt"), "cpu")
        self.assertFalse(plan.openvino)
        self.assertIn("export_openvino", plan.warning)

    def test_forced_gpu_without_gpu_falls_back_to_cpu_plan(self):
        plan = self.plan("gpu", "cpu")
        self.assertTrue(plan.openvino)
        self.assertIn("GPU", plan.warning)

    def test_native_reid_tracker_is_swapped_only_for_openvino(self):
        native = self.root / "native.yaml"
        native.write_text("with_reid: True\nmodel: auto\n", encoding="utf-8")
        noreid = self.root / "noreid.yaml"
        noreid.write_text("with_reid: False\nmodel: auto\n", encoding="utf-8")
        cpu_plan, gpu_plan = self.plan("cpu"), self.plan("auto", "cuda")
        self.assertEqual(runtime.effective_tracker(cpu_plan, str(native), "cpu.yaml"), "cpu.yaml")
        self.assertEqual(runtime.effective_tracker(cpu_plan, str(noreid), "cpu.yaml"), str(noreid))
        self.assertEqual(runtime.effective_tracker(gpu_plan, str(native), "cpu.yaml"), str(native))

    def test_colab_export_always_uses_pt_on_gpu(self):
        exported = build_colab_config({"model_path": self.ov, "runtime_mode": "cpu"}, self.root)
        self.assertEqual(exported["runtime_mode"], "gpu")
        self.assertTrue(exported["model_path"].endswith("best.pt"))

    def test_export_shape_keeps_training_scale(self):
        self.assertEqual(input_shape(1280, "16:9"), [736, 1280])
        self.assertEqual(input_shape(1280, "4:3"), [960, 1280])
        self.assertEqual(input_shape(1280, "square"), [1280, 1280])


if __name__ == "__main__":
    unittest.main()
