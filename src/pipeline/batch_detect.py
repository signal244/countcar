"""여러 영상을 차례로 탐지·궤적 저장 (교차로별 DB, 영상별 세션).

    python -m src.pipeline.batch_detect 경원교차로_오전첨두.mp4 경원교차로_오후첨두.mp4 봉황교사거리_오전첨두.mp4
    python -m src.pipeline.batch_detect --folder D:\\videos            # 폴더의 영상 전체
    python -m src.pipeline.batch_detect --existing overwrite ...       # 완료 기록 없는 기존 세션도 다시 처리

영상 이름은 <교차로명>_<세션명>. 완료한 영상은 다시 실행해도 건너뛴다(이어서 하기).
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

from src.services.batch_detection import (
    DEFAULT_DB_DIR,
    VIDEO_SUFFIXES,
    BatchJob,
    db_path_for,
    parse_video_name,
    run_batch,
)


def collect_videos(paths: list[str], folder: str | None) -> list[Path]:
    videos = [Path(p) for p in paths]
    if folder:
        videos += sorted(p for p in Path(folder).iterdir() if p.suffix.lower() in VIDEO_SUFFIXES)
    return videos


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("videos", nargs="*", help="영상 파일들 (처리 순서대로)")
    ap.add_argument("--folder", help="이 폴더의 영상을 이름순으로 모두 처리")
    ap.add_argument("--config", default="config/app_config.json")
    ap.add_argument("--db-dir", default=str(DEFAULT_DB_DIR), help="교차로별 DB 를 둘 폴더")
    ap.add_argument("--model", default="", help="모든 영상에 쓸 모델 (생략하면 설정 파일 값)")
    ap.add_argument("--imgsz", default="", help="모든 영상에 쓸 이미지 크기 (auto 또는 숫자)")
    ap.add_argument("--existing", choices=["skip", "overwrite"], default="skip",
                    help="완료 기록 없이 이미 있는 세션: skip(건너뜀, 기본) / overwrite(다시 처리)")
    args = ap.parse_args()

    videos = collect_videos(args.videos, args.folder)
    if not videos:
        ap.error("처리할 영상을 지정하세요.")
    jobs = []
    for video in videos:
        junction, session = parse_video_name(video)
        if not session:
            sys.exit(f"영상 이름에서 세션명을 찾지 못했습니다(<교차로명>_<세션명> 형식이어야 함): {video.name}")
        jobs.append(BatchJob(video, junction, session, args.model, args.imgsz))
    dup = {j.session_id for j in jobs if sum(k.session_id == j.session_id for k in jobs) > 1}
    if dup:
        sys.exit(f"같은 세션이 여러 번 있습니다: {', '.join(sorted(dup))}")

    print(f"영상 {len(jobs)}개")
    for n, job in enumerate(jobs, 1):
        print(f"  {n}. {job.video.name} -> {db_path_for(job.junction, args.db_dir).name} / 세션 '{job.session_id}'")

    last = {"t": 0.0}

    def progress(i: int, message: str) -> None:
        if message.startswith(("[info] runtime", "[warn]", "[batch]", "[error]")):
            print(f"  [{i + 1}/{len(jobs)}] {message}", flush=True)
        elif re.match(r"\[progress\] frame \d+", message) and time.time() - last["t"] >= 60:
            last["t"] = time.time()
            print(f"  [{i + 1}/{len(jobs)}] {message}", flush=True)

    def status(i: int, state: str, detail: str) -> None:
        label = {"running": "시작", "done": "완료", "skipped": "건너뜀", "failed": "실패", "stopped": "중지"}.get(state, state)
        print(f"[{i + 1}/{len(jobs)}] {label}: {jobs[i].video.name} {detail}".rstrip(), flush=True)

    results = run_batch(jobs, cfg_path=Path(args.config), db_dir=args.db_dir, existing=args.existing,
                        progress_cb=progress, status_cb=status)
    counts = {s: sum(r.status == s for r in results) for s in ("done", "skipped", "failed", "stopped")}
    print(f"\n완료 {counts['done']} / 건너뜀 {counts['skipped']} / 실패 {counts['failed']}")
    for r in results:
        if r.status == "failed":
            print(f"  실패: {r.job.video.name} — {r.message}")
    if counts["failed"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
