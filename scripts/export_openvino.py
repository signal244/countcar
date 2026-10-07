"""학습한 YOLO 모델(.pt)을 로컬 CPU 용 OpenVINO 모델로 변환한다(기본 INT8).

    python scripts/export_openvino.py --model models/best.pt --data <학습 데이터 yaml>

- INT8 보정은 모델당 한 번. data yaml 의 이미지(라벨 불필요)로 값 범위를 잰다.
  보정 이미지가 있는 곳(학습한 Colab 등)에서 돌리면 편하다. 결과 폴더는 어느 PC 의 CPU 에서나 쓴다.
- 입력은 학습 크기(--imgsz, 긴 변)에 맞춘 직사각형. 2560x1440 영상을 학습 때와 같은
  배율로 줄이면서 회색 여백만 덜어낸다. 비율이 다른 카메라는 --aspect 를 바꿔 따로 변환한다.
- 결과: 모델 옆에 <이름>_int8_openvino_model/ (FP32 는 <이름>_openvino_model/) 폴더.
"""
from __future__ import annotations

import argparse
import math
from pathlib import Path


def input_shape(long_side: int, aspect: str) -> list[int]:
    if aspect == "square":
        return [long_side, long_side]
    w, h = (float(x) for x in aspect.split(":"))
    short = math.ceil(long_side * min(w, h) / max(w, h) / 32) * 32
    return [short, long_side] if w >= h else [long_side, short]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", required=True, help="학습한 .pt 모델")
    ap.add_argument("--data", help="INT8 보정용 데이터셋 yaml (학습에 쓴 것). --fp32 면 불필요")
    ap.add_argument("--imgsz", type=int, default=1280, help="학습 imgsz(긴 변)")
    ap.add_argument("--aspect", default="16:9", help="영상 가로:세로 (예: 16:9, 4:3) 또는 square")
    ap.add_argument("--fraction", type=float, default=1.0, help="보정에 쓸 이미지 비율(0~1)")
    ap.add_argument("--fp32", action="store_true", help="INT8 대신 FP32 로 변환")
    args = ap.parse_args()

    if not args.fp32 and not args.data:
        ap.error("INT8 변환에는 --data 가 필요합니다(또는 --fp32).")

    from ultralytics import YOLO

    shape = input_shape(args.imgsz, args.aspect)
    kwargs = {"format": "openvino", "imgsz": shape}
    if not args.fp32:
        kwargs.update(quantize=8, data=args.data, fraction=args.fraction)
    print(f"[export] {args.model} -> OpenVINO {'FP32' if args.fp32 else 'INT8'}, 입력 {shape[0]}x{shape[1]} (세로x가로)")
    out = Path(YOLO(args.model).export(**kwargs))
    print(f"[done] {out}")
    print("로컬 app_config.json 에서 다음처럼 쓰면 된다:")
    print(f'  "model_path": "models/{out.name}",  "yolo_imgsz": {args.imgsz},  "tracker_config": "config/botsort_cpu.yaml",  "device": "cpu"')


if __name__ == "__main__":
    main()
