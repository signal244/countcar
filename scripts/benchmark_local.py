"""로컬 PC 에서 모델별 처리 속도(초당 처리 프레임)를 잰다. 모델 로드·첫 프레임 준비 시간은 뺀다.

    python scripts/benchmark_local.py --video <영상> --model models/best_int8_openvino_model models/best.pt

모델마다 (1) 탐지만, (2) 탐지+추적(--tracker, 기본 CPU 용 BoT-SORT+ReID) 두 번 잰다.
영상 디코딩과 프레임 건너뛰기(target_fps)를 포함한 실제 처리 속도이며,
결과의 '초당 프레임'이 target_fps 이상이면 실시간 처리가 가능하다.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import cv2

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

WARMUP = 5


def measure(model_path: str, video: str, *, track: bool, tracker: str, imgsz: int, conf: float,
            stride: int, frames: int, device: str, classes: list[int] | None) -> float:
    from ultralytics import YOLO

    model = YOLO(model_path, task="detect")
    kwargs = dict(source=video, stream=True, imgsz=imgsz, rect=True, conf=conf, vid_stride=stride,
                  device=device, classes=classes, verbose=False)
    stream = model.track(tracker=tracker, **kwargs) if track else model.predict(**kwargs)
    start, n = None, 0
    for _ in stream:
        n += 1
        if n == WARMUP:
            start = time.perf_counter()
        if n >= WARMUP + frames:
            break
    if start is None or n <= WARMUP:
        raise RuntimeError("영상이 너무 짧습니다. --frames 를 줄이세요.")
    return (n - WARMUP) / (time.perf_counter() - start)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--video", required=True)
    ap.add_argument("--model", nargs="+", required=True, help="비교할 모델들(.pt 또는 *_openvino_model 폴더)")
    ap.add_argument("--tracker", nargs="+", default=["config/botsort_cpu.yaml"],
                    help="비교할 추적 설정들 (예: config/botsort_cpu.yaml ocsort.yaml)")
    ap.add_argument("--classes", type=int, nargs="*", default=None,
                    help="탐지할 클래스 번호. 생략하면 config/app_config.json 의 allowed_classes")
    ap.add_argument("--imgsz", type=int, default=1280)
    ap.add_argument("--conf", type=float, default=0.1)
    ap.add_argument("--target-fps", type=float, default=10.0)
    ap.add_argument("--frames", type=int, default=200, help="측정할 프레임 수(샘플링 후 기준)")
    ap.add_argument("--device", default="cpu")
    args = ap.parse_args()

    cap = cv2.VideoCapture(args.video)
    src_fps = cap.get(cv2.CAP_PROP_FPS) or args.target_fps
    cap.release()
    stride = max(1, round(src_fps / args.target_fps))
    print(f"영상 {src_fps:.1f}fps -> {stride}프레임마다 1장 처리 (목표 {args.target_fps:g}fps), 측정 {args.frames}프레임\n")

    classes = args.classes
    if classes is None:
        import json

        classes = json.loads((ROOT / "config" / "app_config.json").read_text(encoding="utf-8-sig")).get("allowed_classes")

    rows = []
    for model_path in args.model:
        model_classes = classes
        if classes == "auto":
            from src.config.model_profiles import read_model_classes, suggest_detect_class_ids, suggest_excel_mapping

            names = read_model_classes(model_path)
            model_classes = suggest_detect_class_ids(names, suggest_excel_mapping([names[c] for c in sorted(names)])[0])
        common = dict(imgsz=args.imgsz, conf=args.conf, stride=stride, frames=args.frames,
                      device=args.device, classes=model_classes)
        rows.append((model_path, "(탐지만)", measure(model_path, args.video, track=False, tracker="", **common)))
        for tracker in args.tracker:
            rows.append((model_path, tracker, measure(model_path, args.video, track=True, tracker=tracker, **common)))

    print(f"\n{'모델':<40}{'추적 설정':<28}{'초당 프레임':>10}")
    for model_path, tracker, fps in rows:
        ok = "실시간 가능" if fps >= args.target_fps else "실시간 불가"
        print(f"{model_path:<40}{tracker:<28}{fps:>8.1f}fps   {ok}")


if __name__ == "__main__":
    main()
