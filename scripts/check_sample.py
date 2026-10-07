"""샘플 영상으로 이 PC 의 처리 속도와 정확도(정답 대비 방향별 오차)를 확인한다.

    python scripts/check_sample.py
    python scripts/check_sample.py --tracker config/botsort_noreid.yaml     # ReID 끄고 비교
    python scripts/check_sample.py --model models/yolo26n_v1.pt

GUI 와 같은 탐지 서비스·실행 모드(GPU/CPU 자동)·후처리(자동 병합) 규칙을 쓴다.
결과 DB 는 output/sample_check/ 에 따로 저장하며 다른 DB 는 건드리지 않는다.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import cv2

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.pipeline.count_tracks import count_track_trajs_streaming, load_lines_with_scale  # noqa: E402
from src.pipeline.track_merge import merge_params_from_config, run_track_merge  # noqa: E402
from src.services.detection_service import DetectionRequest, DetectionService  # noqa: E402

SAMPLE = ROOT / "sample"
SESSION = "SAMPLE"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--truth", default=str(SAMPLE / "정답_15-30분.json"))
    ap.add_argument("--video", default=None, help="생략하면 정답 파일에 적힌 샘플 영상")
    ap.add_argument("--config", default=str(ROOT / "config" / "app_config.json"))
    ap.add_argument("--model", default=None, help="생략하면 설정 파일의 model_path")
    ap.add_argument("--tracker", default=None, help="추적 설정 yaml (생략하면 설정 파일 값)")
    ap.add_argument("--runtime", choices=["auto", "gpu", "cpu"], default=None)
    args = ap.parse_args()

    truth = json.loads(Path(args.truth).read_text(encoding="utf-8"))
    base = Path(args.truth).parent
    video = Path(args.video) if args.video else base / truth["video"]
    lines_path = base / truth["lines"]
    t0, t1 = truth["window_sec"]
    cfg = json.loads(Path(args.config).read_text(encoding="utf-8-sig"))

    out_dir = ROOT / "output" / "sample_check"
    out_dir.mkdir(parents=True, exist_ok=True)
    db = out_dir / f"sample_{datetime.now():%Y%m%d_%H%M%S}.sqlite"
    overrides = {"db_path": str(db), "count_db_path": str(db)}
    if args.model:
        overrides["model_path"] = args.model
    if args.tracker:
        overrides["tracker_config"] = args.tracker
    if args.runtime:
        overrides["runtime_mode"] = args.runtime

    cap = cv2.VideoCapture(str(video))
    total_src = max(1, int(cap.get(cv2.CAP_PROP_FRAME_COUNT)))
    cap.release()
    frames = 0
    last_report = time.perf_counter()

    def progress(msg: str) -> None:
        nonlocal frames, last_report
        if msg.startswith(("[info] runtime", "[warn]", "[info] yolo_imgsz", "[info] src_fps")):
            print(msg, flush=True)
        m = re.search(r"\[progress\] frame (\d+)", msg)
        if m and time.perf_counter() - last_report >= 30:
            last_report = time.perf_counter()
            print(f"  {100 * int(m.group(1)) / total_src:.0f}% 처리 중...", flush=True)
        m = re.search(r"total sampled frames: (\d+)", msg)
        if m:
            frames = int(m.group(1))

    print(f"샘플: {video.name}  (영상의 {t0 // 60}~{t1 // 60}분 구간을 정답과 비교, 처리 시간에는 모델 준비 시간 포함)")
    started = time.perf_counter()
    result = DetectionService().run(
        DetectionRequest(cfg_path=Path(args.config), video_path=video, line_path=lines_path,
                         overrides=overrides, session_id=SESSION),
        progress_cb=progress,
    )
    elapsed = time.perf_counter() - started

    lines = load_lines_with_scale(lines_path)[0]
    params = merge_params_from_config(cfg)
    if params:
        run_track_merge(db, result.session_id, lines_path=lines_path, **params)
    _multi, final = count_track_trajs_streaming(db, lines, 300, 0.0, 0.0, 0, 0.0, session_id=result.session_id,
                                                mode="turn", use_track_merge=bool(params), use_virtual_events=True)
    final = final[(final["slot"] >= t0) & (final["slot"] < t1)]
    got = {f"{a}->{b}": int(v) for (a, b), v in final.groupby(["line_from", "line_to"])["count"].sum().items()}

    print(f"\n{'방향':<24}{'정답':>6}{'결과':>6}{'차이':>6}")
    err = 0
    for key, want in sorted(truth["counts"].items(), key=lambda kv: -kv[1]):
        have = got.get(key, 0)
        err += abs(have - want)
        print(f"{key:<24}{want:>6}{have:>6}{have - want:>+6}")
    total = sum(got.get(k, 0) for k in truth["counts"])
    extra = sum(v for k, v in got.items() if k not in truth["counts"])
    want_total = truth["total"]
    print("-" * 42)
    print(f"{'합계':<24}{want_total:>6}{total:>6}{total - want_total:>+6}")
    print(f"\n정답 대비 집계 {100 * total / want_total:.1f}% | 방향별 오차 합 {err}대 ({100 * err / want_total:.1f}%)"
          f" | 정답에 없는 방향(유턴 등) {extra}대")
    info = result.tracker_info
    print(f"실행 방식: {info.get('runtime', '')} | 추적: {Path(str(info.get('tracker_config', ''))).name}")
    if frames:
        print(f"처리 속도: {frames / elapsed:.1f} 프레임/초 ({frames} 프레임, {elapsed / 60:.1f}분)")
    print(f"결과 DB: {db}")


if __name__ == "__main__":
    main()
