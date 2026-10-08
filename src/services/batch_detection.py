"""여러 영상을 차례로 탐지·궤적 저장한다. GUI·CLI·Colab 공통.

- 영상 이름 `<교차로명>_<세션명>` (마지막 밑줄 기준)에서 교차로·세션을 정한다.
- 교차로마다 SQLite 하나(output/db_snapshots/tracks_<교차로명>.sqlite), 영상마다 세션 `<교차로명> <세션명>`.
- 분석라인은 쓰지 않는다(화면 전체 탐지).
- 항상 DetectionService 의 덮어쓰기 모드로 돈다: 임시 세션에 쓰고 성공해야 본 세션과 바꾼다.
  중간에 꺼져도 기존 데이터는 남고, 남은 임시 세션은 다음 실행 때 지운다.
- 완료한 영상은 detection_runs 에 기록해 다시 실행하면 건너뛴다(이어서 하기).
"""
from __future__ import annotations

import logging
import re
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional

from src.db.schema import init_db
from src.services.detection_service import DetectionRequest, DetectionService, _delete_session

logger = logging.getLogger(__name__)

DEFAULT_DB_DIR = Path("output/db_snapshots")
EXISTING_POLICIES = ("skip", "overwrite")
VIDEO_SUFFIXES = (".mp4", ".avi", ".mkv", ".mov", ".m4v", ".ts")


@dataclass
class BatchJob:
    video: Path
    junction: str
    session: str
    model_path: str = ""   # 빈 값이면 현재 설정
    imgsz: str = ""        # 빈 값이면 현재 설정, 아니면 "auto" 또는 숫자

    @property
    def session_id(self) -> str:
        return f"{self.junction.strip()} {self.session.strip()}".strip()


@dataclass
class JobResult:
    job: BatchJob
    status: str            # done | skipped | failed | stopped
    db_path: Path
    message: str = ""
    elapsed_sec: float = 0.0
    sampled_frames: int = 0


def parse_video_name(video: str | Path) -> tuple[str, str]:
    """`경원교차로_오전첨두` -> ("경원교차로", "오전첨두"). 밑줄이 없으면 세션명은 비운다."""
    stem = Path(str(video)).stem.strip()
    if "_" not in stem:
        return stem, ""
    junction, session = stem.rsplit("_", 1)
    return junction.strip(), session.strip()


def jobs_from_folder(folder: str | Path, names: Optional[List[str]] = None) -> tuple[List[BatchJob], List[str]]:
    """폴더의 `<교차로명>_<세션명>` 영상으로 작업 목록을 만든다(이름순). names 를 주면 그 파일만.

    돌려주는 두 번째 값은 이름 형식이 맞지 않아 뺀 파일들.
    """
    folder = Path(folder)
    if names:
        videos = [folder / n for n in names]
    else:
        videos = sorted(p for p in folder.iterdir() if p.suffix.lower() in VIDEO_SUFFIXES)
    jobs, rejected = [], []
    for video in videos:
        junction, session = parse_video_name(video)
        if junction and session:
            jobs.append(BatchJob(video, junction, session))
        else:
            rejected.append(video.name)
    return jobs, rejected


def jobs_to_dicts(jobs: List[BatchJob], video_path_fn: Callable[[Path], str] = str,
                  model_path_fn: Callable[[str], str] = str) -> List[Dict[str, str]]:
    """작업 목록을 JSON 으로 저장할 수 있게 바꾼다(GUI -> Colab 내보내기)."""
    return [{"video": video_path_fn(j.video), "junction": j.junction, "session": j.session,
             "model_path": model_path_fn(j.model_path) if j.model_path else "", "imgsz": j.imgsz} for j in jobs]


def jobs_from_dicts(items: List[Mapping[str, object]]) -> List[BatchJob]:
    return [BatchJob(Path(str(it["video"])), str(it["junction"]), str(it["session"]),
                     str(it.get("model_path") or ""), str(it.get("imgsz") or "")) for it in items]


def safe_filename(name: str) -> str:
    cleaned = "".join("_" if c in '<>:"/\\|?*' else c for c in (name or "").strip()).strip().strip(".")
    return cleaned or "junction"


def db_path_for(junction: str, db_dir: str | Path = DEFAULT_DB_DIR) -> Path:
    return Path(db_dir) / f"tracks_{safe_filename(junction)}.sqlite"


def _session_has_data(conn: sqlite3.Connection, session_id: str) -> bool:
    for table in ("track_trajs", "tracks"):
        try:
            if conn.execute(f"select 1 from {table} where session_id=? limit 1", (session_id,)).fetchone():
                return True
        except sqlite3.OperationalError:
            continue
    return False


def session_state(db_path: Path, session_id: str) -> str:
    """new(없음) | done(일괄 탐지로 완료) | existing(완료 기록 없이 데이터만 있음)."""
    if not Path(db_path).is_file():
        return "new"
    with closing(sqlite3.connect(db_path)) as conn:
        if not _session_has_data(conn, session_id):
            return "new"
        try:
            row = conn.execute("select status from detection_runs where session_id=?", (session_id,)).fetchone()
        except sqlite3.OperationalError:
            row = None
    return "done" if row and row[0] == "done" else "existing"


def cleanup_partial_sessions(db_path: Path, session_id: str) -> List[str]:
    """이전 실행이 남긴 `<세션>_partial_<id>` 임시 세션을 지운다."""
    if not Path(db_path).is_file():
        return []
    pattern = re.compile(re.escape(session_id) + r"_partial_[0-9a-f]+$")
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute("PRAGMA busy_timeout = 30000")
        found = set()
        for table in ("track_trajs", "tracks"):
            try:
                found |= {r[0] for r in conn.execute(
                    f"select distinct session_id from {table} where session_id like ?", (f"{session_id}_partial_%",))}
            except sqlite3.OperationalError:
                continue
        removed = sorted(s for s in found if s and pattern.match(s))
        for sid in removed:
            _delete_session(conn, sid)
    return removed


def _record(db_path: Path, job: BatchJob, model: str, status: str, started: str,
            frames: int = 0, message: str = "") -> None:
    finished = datetime.now().isoformat(timespec="seconds") if status != "running" else None
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute(
            "insert or replace into detection_runs(session_id, video_path, model_path, status, started_at, "
            "finished_at, sampled_frames, message) values(?,?,?,?,?,?,?,?)",
            (job.session_id, str(job.video), model, status, started, finished, frames, message),
        )


def job_overrides(job: BatchJob, base: Mapping[str, object], db_path: Path) -> Dict[str, object]:
    overrides = dict(base)
    overrides.update({
        "db_path": str(db_path),
        "count_db_path": str(db_path),
        "session_id": job.session_id,
        "junction_name": job.junction,
        "session_name": job.session,
        "line_settings_path": "",   # 탐지·궤적만: 라인(ROI) 없이 화면 전체
        "roi": [],
    })
    if job.model_path:
        overrides["model_path"] = job.model_path
        # 메인 화면의 차종 번호는 메인 모델 기준이다. 다른 모델이면 모델의 차종 이름으로 다시 고른다.
        overrides["allowed_classes"] = "auto"
    if job.imgsz:
        overrides["yolo_imgsz"] = "auto" if str(job.imgsz).lower() == "auto" else int(job.imgsz)
    return overrides


def run_batch(
    jobs: List[BatchJob],
    *,
    cfg_path: Path,
    base_overrides: Optional[Mapping[str, object]] = None,
    db_dir: str | Path = DEFAULT_DB_DIR,
    existing: str = "skip",
    progress_cb: Optional[Callable[[int, str], None]] = None,
    status_cb: Optional[Callable[[int, str, str], None]] = None,
    stop_now_cb: Optional[Callable[[], bool]] = None,
    stop_after_current_cb: Optional[Callable[[], bool]] = None,
    service: Optional[DetectionService] = None,
) -> List[JobResult]:
    """jobs 를 차례로 처리한다. 하나가 실패해도 다음으로 넘어가고, 즉시 중지면 남은 것은 처리하지 않는다."""
    if existing not in EXISTING_POLICIES:
        raise ValueError(f"existing 은 {EXISTING_POLICIES} 중 하나여야 합니다: {existing}")
    service = service or DetectionService()
    base = dict(base_overrides or {})
    results: List[JobResult] = []

    def status(i: int, state: str, detail: str = "") -> None:
        if status_cb is not None:
            status_cb(i, state, detail)

    for i, job in enumerate(jobs):
        if (stop_now_cb and stop_now_cb()) or (i > 0 and stop_after_current_cb and stop_after_current_cb()):
            break
        db_path = db_path_for(job.junction, db_dir)
        state = session_state(db_path, job.session_id)
        if state == "done" or (state == "existing" and existing == "skip"):
            reason = "이미 완료됨" if state == "done" else "기존 세션이 있어 건너뜀"
            status(i, "skipped", reason)
            results.append(JobResult(job, "skipped", db_path, reason))
            continue

        init_db(db_path)
        for sid in cleanup_partial_sessions(db_path, job.session_id):
            if progress_cb:
                progress_cb(i, f"[batch] 이전 실행의 임시 세션 정리: {sid}")
        overrides = job_overrides(job, base, db_path)
        model = str(overrides.get("model_path") or "")
        started = datetime.now().isoformat(timespec="seconds")
        _record(db_path, job, model, "running", started)
        status(i, "running", "")
        frames = 0

        def on_progress(message: str, i: int = i) -> None:
            nonlocal frames
            m = re.search(r"total sampled frames: (\d+)", message)
            if m:
                frames = int(m.group(1))
            if progress_cb:
                progress_cb(i, message)

        t0 = time.perf_counter()
        try:
            result = service.run(
                DetectionRequest(cfg_path=Path(cfg_path), video_path=Path(job.video), overrides=overrides,
                                 session_id=job.session_id, overwrite_session=True),
                progress_cb=on_progress,
                should_stop_cb=stop_now_cb,
            )
        except Exception as exc:  # noqa: BLE001 — 한 영상 실패로 전체를 멈추지 않는다
            logger.error("Batch job failed: %s", job.video, exc_info=True)
            elapsed = time.perf_counter() - t0
            _record(db_path, job, model, "failed", started, frames, str(exc))
            cleanup_partial_sessions(db_path, job.session_id)
            status(i, "failed", str(exc))
            results.append(JobResult(job, "failed", db_path, str(exc), elapsed, frames))
            continue
        elapsed = time.perf_counter() - t0
        if result.stopped:
            # 즉시 중지: 덮어쓰기 모드라 기존 세션은 그대로이고, 반쪽 임시 세션은 버린다.
            cleanup_partial_sessions(db_path, job.session_id)
            _record(db_path, job, model, "stopped", started, frames, "사용자 중지")
            status(i, "stopped", "")
            results.append(JobResult(job, "stopped", db_path, "사용자 중지", elapsed, frames))
            break
        _record(db_path, job, model, "done", started, frames)
        status(i, "done", f"{elapsed / 60:.1f}분")
        results.append(JobResult(job, "done", db_path, "", elapsed, frames))
    return results
