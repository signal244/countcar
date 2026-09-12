"""
(Colab) 원본 영상 -> BEV 영상 변환 전용

용도:
- 긴 영상을 먼저 BEV mp4로만 변환
- 변환 완료 후 별도 탐지/추적 코랩에서 _bev.mp4를 입력으로 사용

입력:
- COUNTCAR_PROJECT_NAME
- COUNTCAR_JUNCTION_NAME
- COUNTCAR_SESSION_NAME

경로 규칙:
- 원본 영상: /content/drive/MyDrive/video/<project>/<junction> <session>.mp4
- BEV 설정: /content/drive/MyDrive/vm/count_car_ver6.0/config/bev/<junction>.json
- 출력 영상: 원본 영상 폴더에 <원본파일명>_bev.mp4
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

import pytz

try:
    from google.colab import drive, runtime
except Exception:  # pragma: no cover - colab-only import
    drive = None
    runtime = None

THIS_FILE = Path(__file__).resolve()
PROJECT_ROOT = THIS_FILE.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.bev import export_bev_video, load_bev_config


tz = pytz.timezone("Asia/Seoul")

ENV_PROJECT_NAME = os.environ.get("COUNTCAR_PROJECT_NAME", "").strip()
ENV_JUNCTION_NAME = os.environ.get("COUNTCAR_JUNCTION_NAME", "").strip()
ENV_SESSION_NAME = os.environ.get("COUNTCAR_SESSION_NAME", "").strip()


def _safe_filename(name: str) -> str:
    name = (name or "").strip()
    if not name:
        return "junction"
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    name = re.sub(r"\s+", "_", name)
    return name[:120]


def safe_copy_drive_to_local(src: Path, dst: Path, retries: int = 3) -> None:
    last_exc: Exception | None = None
    for i in range(max(1, int(retries))):
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists():
                dst.unlink()
            try:
                shutil.copy2(src, dst)
                return
            except OSError:
                with src.open("rb") as fsrc, dst.open("wb") as fdst:
                    shutil.copyfileobj(fsrc, fdst, length=16 * 1024 * 1024)
                return
        except OSError as exc:
            last_exc = exc
            print(f"[warn] Drive->local 복사 실패 ({i+1}/{retries}): {exc}", flush=True)
            if drive is not None:
                print("[info] Drive 재마운트 시도...", flush=True)
                drive.mount("/content/drive", force_remount=True)
                time.sleep(2)
    raise RuntimeError(f"Drive->local 복사 실패: {last_exc}")


def main() -> None:
    if not ENV_PROJECT_NAME or not ENV_JUNCTION_NAME or not ENV_SESSION_NAME:
        raise RuntimeError("환경변수 COUNTCAR_PROJECT_NAME / COUNTCAR_JUNCTION_NAME / COUNTCAR_SESSION_NAME 이 필요합니다.")

    drive_root = Path("/content/drive")
    if not drive_root.exists():
        raise RuntimeError("/content/drive 가 없습니다. 노트북 셀에서 먼저 drive.mount() 하세요.")

    start_time = datetime.now(tz)
    print(f"🚀 BEV 변환 시작 시간: {start_time.strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
    print(
        f"✅ 적용됨(env): 프로젝트명='{ENV_PROJECT_NAME}', 교차로명='{ENV_JUNCTION_NAME}', 첨두시간대='{ENV_SESSION_NAME}'",
        flush=True,
    )

    root = Path("/content/drive/MyDrive/vm/count_car_ver6.0")
    drive_video = Path(f"/content/drive/MyDrive/video/{ENV_PROJECT_NAME}/{ENV_JUNCTION_NAME} {ENV_SESSION_NAME}.mp4")
    drive_bev_json = root / "config" / "bev" / f"{_safe_filename(ENV_JUNCTION_NAME)}.json"
    drive_bev_video = drive_video.with_name(f"{drive_video.stem}_bev.mp4")

    if not drive_video.exists():
        raise FileNotFoundError(f"원본 VIDEO가 없습니다: {drive_video}")
    if not drive_bev_json.exists():
        raise FileNotFoundError(f"BEV JSON이 없습니다: {drive_bev_json}")

    cfg = load_bev_config(drive_bev_json)
    if cfg is None:
        raise RuntimeError(f"유효한 BEV 설정 JSON을 읽을 수 없습니다: {drive_bev_json}")

    local_video = Path("/content/input_video.mp4")
    local_bev_video = Path("/content/input_video_bev.mp4")

    print("VIDEO(Drive):", drive_video, flush=True)
    print("BEV JSON(Drive):", drive_bev_json, flush=True)
    print("BEV VIDEO(Drive out):", drive_bev_video, flush=True)

    print("Copy Drive video -> local ...", flush=True)
    safe_copy_drive_to_local(drive_video, local_video, retries=3)

    last_print = {"ts": 0.0}

    def _on_progress(done: int, total: int) -> None:
        now = time.time()
        if now - last_print["ts"] < 2.0:
            return
        last_print["ts"] = now
        if total > 0:
            pct = (float(done) * 100.0) / float(total)
            print(f"[bev] {done}/{total} frames ({pct:.1f}%)", flush=True)
        else:
            print(f"[bev] {done} frames", flush=True)

    started = time.time()
    frame_count, fps = export_bev_video(local_video, local_bev_video, cfg, progress_cb=_on_progress)
    elapsed = time.time() - started
    print(f"[bev] 완료(local): {local_bev_video} frames={frame_count} fps={fps:.3f} elapsed={elapsed:.1f}s", flush=True)

    drive_bev_video.parent.mkdir(parents=True, exist_ok=True)
    print(f"[bev] Copy local -> Drive: {drive_bev_video}", flush=True)
    shutil.copy2(local_bev_video, drive_bev_video)

    end_time = datetime.now(tz)
    print(f"🏁 BEV 변환 종료 시간: {end_time.strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
    duration = end_time - start_time
    total_seconds = int(duration.total_seconds())
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60
    print(f"🏁 BEV 변환 소요 시간: {hours:02d}:{minutes:02d}:{seconds:02d}", flush=True)

    if runtime is not None:
        try:
            runtime.unassign()
        except Exception:
            pass


if __name__ == "__main__":
    main()
