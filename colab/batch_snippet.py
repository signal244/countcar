"""(Colab) 여러 영상 일괄 탐지·궤적 저장. colab/일괄탐지.ipynb 가 이 함수들을 부른다.

교차로마다: Drive 잠금 -> Drive DB 를 /content 로 복사 -> 영상마다(복사 -> 탐지 -> 완료 기록 -> Drive 에 DB 저장
-> 로컬 영상 삭제) -> 잠금 해제. 탐지는 로컬 GUI·CLI 와 같은 run_batch 를 쓴다.
세션이 끊기면 노트북을 다시 실행: 완료된 영상은 건너뛰고 끊긴 영상부터 다시 처리한다.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import time
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import Callable, Dict, List, Optional

from colab_io import acquire_lock, backup_sqlite, copy_file, heartbeat, release_lock
from src.services.batch_detection import (
    BatchJob,
    JobResult,
    db_path_for,
    jobs_from_dicts,
    jobs_from_folder,
    run_batch,
    safe_filename,
    session_state,
)
from src.services.detection_service import DetectionRequest, DetectionService

CHECKPOINT_MIN_SEC = 60
HEARTBEAT_SEC = 60


def plan_jobs(video_dir: Optional[Path] = None, files: Optional[List[str]] = None,
              jobs_file: Optional[Path] = None) -> List[BatchJob]:
    """A: video_dir(+files) 의 `<교차로명>_<세션명>` 영상, B: GUI 에서 내보낸 jobs_file."""
    if jobs_file:
        jobs = jobs_from_dicts(json.loads(Path(jobs_file).read_text(encoding="utf-8"))["jobs"])
    else:
        jobs, rejected = jobs_from_folder(video_dir, files or None)
        for name in rejected:
            print(f"[skip] 이름이 <교차로명>_<세션명> 형식이 아님: {name}")
    missing = [j.video for j in jobs if not Path(j.video).is_file()]
    if missing:
        raise FileNotFoundError("영상이 없습니다:\n" + "\n".join(str(p) for p in missing))
    ids = [j.session_id for j in jobs]
    dups = sorted({s for s in ids if ids.count(s) > 1})
    if dups:
        raise ValueError(f"같은 교차로·세션이 두 번 이상 있습니다: {dups}")
    return jobs


def print_plan(jobs: List[BatchJob], drive_db_dir: Path) -> None:
    print(f"영상 {len(jobs)}개")
    for n, j in enumerate(jobs, 1):
        state = session_state(drive_db_dir / db_path_for(j.junction).name, j.session_id)
        note = {"done": " (이미 완료 -> 건너뜀)", "existing": " (기존 세션 있음)"}.get(state, "")
        extra = f" | 모델 {Path(j.model_path).name}" if j.model_path else ""
        print(f"  {n}. {Path(j.video).name} -> {db_path_for(j.junction).name} / '{j.session_id}'{extra}{note}")


def run_colab_batch(jobs: List[BatchJob], *, root: Path, work_dir: Path = Path("/content/batch"),
                    base_overrides: Optional[Dict] = None, existing: str = "skip",
                    remount: Optional[Callable[[], None]] = None, use_lock: bool = True,
                    service: Optional[DetectionService] = None) -> List[JobResult]:
    root = Path(root)
    cfg_path = root / "colab" / "app_config_colab.json"
    drive_db_dir = root / "output" / "db_snapshots"
    local_db_dir, local_video_dir = Path(work_dir) / "db", Path(work_dir) / "video"
    local_db_dir.mkdir(parents=True, exist_ok=True)

    groups: Dict[str, List[BatchJob]] = {}
    for job in jobs:
        groups.setdefault(job.junction, []).append(job)

    results: List[JobResult] = []
    total, done_n = len(jobs), 0
    for junction, group in groups.items():
        name = db_path_for(junction).name
        drive_db, local_db = drive_db_dir / name, local_db_dir / name
        lock_dir = drive_db_dir / f".lock_{safe_filename(junction)}"
        print(f"\n===== {junction} ({len(group)}개) =====", flush=True)
        if use_lock:
            acquire_lock(lock_dir)
        try:
            if local_db.exists():
                local_db.unlink()
            if drive_db.exists():
                copy_file(drive_db, local_db, remount=remount)
            for job in group:
                done_n += 1
                tag = f"[{done_n}/{total}]"
                state = session_state(local_db, job.session_id)
                if state == "done" or (state == "existing" and existing == "skip"):
                    reason = "이미 완료됨" if state == "done" else "기존 세션이 있어 건너뜀"
                    print(f"{tag} 건너뜀: {Path(job.video).name} ({reason})", flush=True)
                    results.append(JobResult(job, "skipped", drive_db, reason))
                    continue
                local_video = local_video_dir / Path(job.video).name
                print(f"{tag} 영상 복사: {Path(job.video).name}", flush=True)
                copy_file(Path(job.video), local_video, remount=remount)
                timers = {"ckpt": time.time(), "hb": time.time()}

                def progress(_i: int, msg: str, tag: str = tag) -> None:
                    now = time.time()
                    if use_lock and now - timers["hb"] >= HEARTBEAT_SEC:
                        timers["hb"] = now
                        heartbeat(lock_dir)
                    if msg.startswith(("[info] runtime", "[warn]", "[error]", "[batch]")):
                        print(f"{tag} {msg}", flush=True)
                    elif msg.startswith("[progress] frame") and int(msg.split()[-1]) % 6000 == 0:
                        print(f"{tag} {msg}", flush=True)
                    elif msg.startswith("[flush]") and now - timers["ckpt"] >= CHECKPOINT_MIN_SEC:
                        timers["ckpt"] = now
                        if backup_sqlite(local_db, drive_db):
                            print(f"{tag} [checkpoint] Drive 에 중간 저장", flush=True)

                def status(_i: int, st: str, detail: str, tag: str = tag) -> None:
                    label = {"running": "시작", "done": "완료", "failed": "실패", "stopped": "중지"}.get(st, st)
                    print(f"{tag} {label}: {Path(job.video).name} {detail}".rstrip(), flush=True)

                try:
                    res = run_batch([replace(job, video=local_video)], cfg_path=cfg_path,
                                    base_overrides=base_overrides, db_dir=local_db_dir, existing=existing,
                                    progress_cb=progress, status_cb=status, service=service)
                finally:
                    local_video.unlink(missing_ok=True)
                    _free_gpu_memory()
                if backup_sqlite(local_db, drive_db):
                    print(f"{tag} Drive 에 저장: {drive_db.name}", flush=True)
                results += [replace(r, job=job, db_path=drive_db) for r in res]
        finally:
            if use_lock:
                release_lock(lock_dir)
    return results


def print_summary(results: List[JobResult]) -> None:
    counts = {s: sum(r.status == s for r in results) for s in ("done", "skipped", "failed", "stopped")}
    print(f"\n완료 {counts['done']} / 건너뜀 {counts['skipped']} / 실패 {counts['failed']}")
    for r in results:
        if r.status == "failed":
            print(f"  실패: {Path(r.job.video).name} — {r.message}")


def _free_gpu_memory() -> None:
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:  # noqa: BLE001
        pass


def fp16_check(video: Path, *, root: Path, minutes: float = 3.0, work_dir: Path = Path("/content/fp16_check"),
               base_overrides: Optional[Dict] = None) -> None:
    """영상 앞부분으로 FP32 와 FP16 의 속도·결과를 비교한다(결과는 임시 DB, 실제 DB 는 건드리지 않음)."""
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    clip = work_dir / "clip.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(video), "-t",
                    str(int(minutes * 60)), "-map", "0:v:0", "-c", "copy", str(clip)], check=True)
    rows = []
    for precision in ("fp32", "fp16"):
        db = work_dir / f"{precision}.sqlite"
        db.unlink(missing_ok=True)
        overrides = dict(base_overrides or {})
        overrides.update({"precision": precision, "db_path": str(db), "count_db_path": str(db),
                          "line_settings_path": "", "roi": []})
        t0 = time.perf_counter()
        result = DetectionService().run(DetectionRequest(
            cfg_path=Path(root) / "colab" / "app_config_colab.json", video_path=clip, overrides=overrides,
            session_id=f"check {precision}"))
        elapsed = time.perf_counter() - t0
        with closing(sqlite3.connect(db)) as conn:
            tracks = conn.execute("select count(*) from track_trajs").fetchone()[0]
            by_type = dict(conn.execute("select vehicle_type, count(*) from track_trajs group by 1"))
        rows.append((precision, elapsed, tracks, by_type, result.tracker_info.get("runtime", "")))
        _free_gpu_memory()
    print(f"\n영상 앞 {minutes:g}분 비교 (모델 준비 시간 포함)")
    for precision, elapsed, tracks, by_type, runtime in rows:
        print(f"  {precision.upper()}: {elapsed:6.1f}초 | 궤적 {tracks}개 | {by_type} | {runtime}")
    (fp32, t32, n32, _, _), (_, t16, n16, _, _) = rows
    print(f"  -> FP16 이 {t32 / max(t16, 1e-6):.2f}배 빠름, 궤적 수 차이 {n16 - n32:+d}개 ({100 * (n16 - n32) / max(n32, 1):+.1f}%)")
