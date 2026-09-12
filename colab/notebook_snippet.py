"""
(Colab) 영상 탐지/추적 -> SQLite(DB) 저장 (교차로 DB 파일에 누적)

- Drive(/content/drive)는 느리고 timeout/disk I/O error가 발생할 수 있어
  기본은 VIDEO/DB를 Colab 로컬(/content)로 복사/저장한 뒤,
  flush(예: 15분) 시점과 종료 시에 Drive로 스냅샷(backup)을 저장합니다.

요구사항:
- DB 파일명은 사용자가 지정(교차로명 기반)하고, 파일이 이미 있으면 "기존 DB에 추가(append)"합니다.
- session_id가 이미 존재하면 자동으로 _YYYYMMDD_HHMMSS가 붙어 새 세션으로 저장됩니다(파이프라인 기본 동작).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import pytz
from google.colab import drive, runtime

tz = pytz.timezone("Asia/Seoul")

# 0) 드라이브 마운트
drive.mount("/content/drive", force_remount=True)
start_time = datetime.now(tz)
print(f"🚀 분석 시작 시간: {start_time.strftime('%Y-%m-%d %H:%M:%S')}")

# 입력 UI 방식 선택
# - True : ipywidgets (환경에 따라 클릭 이벤트가 안 먹는 경우가 있어 비권장)
# - False: input() (가장 안정적)
USE_WIDGET_UI = False

def prompt_project_junction_session(
    default_project: str = "창원공사중(분석용)",
    default_junction: str = "상남사거리",
    default_session: str = "오후첨두",
) -> tuple[str, str, str]:
    """
    Colab에서 프로젝트명/교차로명/첨두시간대를 UI로 입력받습니다.
    - ipywidgets 사용 가능하면 위젯 UI
    - 불가능하면 input()으로 폴백
    """
    if USE_WIDGET_UI:
        try:
            import ipywidgets as widgets  # type: ignore
            from IPython.display import display  # type: ignore
            import threading

            project = widgets.Text(value=default_project, description="프로젝트명", layout=widgets.Layout(width="500px"))
            junction = widgets.Text(value=default_junction, description="교차로명", layout=widgets.Layout(width="500px"))
            session = widgets.Text(value=default_session, description="첨두시간대", layout=widgets.Layout(width="500px"))
            btn = widgets.Button(description="적용", button_style="success")
            out = widgets.Output()
            box = widgets.VBox([project, junction, session, btn, out])

            evt = threading.Event()
            result = {"project": default_project, "junction": default_junction, "session": default_session}

            def on_click(_):
                result["project"] = project.value.strip() or default_project
                result["junction"] = junction.value.strip() or default_junction
                result["session"] = session.value.strip() or default_session
                with out:
                    print(
                        f"✅ 적용됨: 프로젝트명='{result['project']}', 교차로명='{result['junction']}', 첨두시간대='{result['session']}'"
                    )
                evt.set()

            btn.on_click(on_click)
            display(box)

            # 버튼 클릭을 기다림(짧은 sleep로 UI 이벤트 처리 여지 확보)
            while not evt.is_set():
                time.sleep(0.1)

            return result["project"], result["junction"], result["session"]
        except Exception:
            pass

    try:
        import ipywidgets as widgets  # type: ignore
        from IPython.display import display  # type: ignore
        import threading

        # widgets가 있어도, 클릭 이벤트가 안 먹는 환경이 있으니 기본은 input() 사용
        _ = widgets  # keep import for environments that require it
        _ = display
        _ = threading
    except Exception:
        pass

    project = (input(f"프로젝트명 입력(기본:{default_project}): ").strip() or default_project).strip()
    junction = (input(f"교차로명 입력(기본:{default_junction}): ").strip() or default_junction).strip()
    session = (input(f"첨두시간대 입력(기본:{default_session}): ").strip() or default_session).strip()
    print(f"✅ 적용됨: 프로젝트명='{project}', 교차로명='{junction}', 첨두시간대='{session}'", flush=True)
    return project, junction, session


##################################
# 프로젝트명: Drive VIDEO 경로의 폴더명
# 교차로명: Drive DB 파일명(고정)
# 세션명: session_id(권장: 첨두명/날짜 등). DB 안에서 중복이면 자동 suffix됨.
PROJECT_NAME, JUNCTION_NAME, SESSION_NAME = prompt_project_junction_session("창원공사중(분석용)", "상남사거리", "점심첨두")
# Drive 비디오 파일명(자유롭게 지정 가능)
VIDEO_FILENAME = f"{JUNCTION_NAME} {SESSION_NAME}.mp4"
##################################

# ===== 저장/입력 위치 선택 =====
USE_LOCAL_VIDEO = True   # True: VIDEO를 /content 로컬로 복사해서 사용(권장)
USE_LOCAL_DB = True      # True: DB를 /content 로컬에 저장(권장)
COPY_DB_TO_DRIVE = True  # USE_LOCAL_DB일 때, flush/종료 시 Drive로 스냅샷 저장

# 체크포인트: DetectionTracker가 DB flush를 할 때([flush] 로그) Drive로 스냅샷 저장
CHECKPOINT_ENABLED = True
CHECKPOINT_MIN_SECONDS = 60  # 너무 자주 저장되는 것 방지(최소 간격)

# 1) 경로 설정
ROOT = Path("/content/drive/MyDrive/vm/count_car_ver6.0")
DRIVE_VIDEO = Path(f"/content/drive/MyDrive/video/{PROJECT_NAME}/{VIDEO_FILENAME}")
print("VIDEO(Drive):", DRIVE_VIDEO, "exists:", DRIVE_VIDEO.exists())

# 로컬 VIDEO/DB 경로
LOCAL_VIDEO = Path("/content/input_video.mp4")
LOCAL_DB = Path("/content/tracks.sqlite")
RUNTIME_CFG = Path("/content/app_config_runtime.json")


def _safe_filename(name: str) -> str:
    name = (name or "").strip()
    if not name:
        return "junction"
    name = re.sub(r"[<>:\"/\\\\|?*]", "_", name)
    name = re.sub(r"\\s+", "_", name)
    return name[:120]


SNAP_DIR = ROOT / "output" / "db_snapshots"
DRIVE_DB = SNAP_DIR / f"tracks_{_safe_filename(JUNCTION_NAME)}.sqlite"

# 같은 교차로 DB를 2개 런타임에서 동시에 append하면 "마지막 저장이 승리"로 누락이 생길 수 있어
# Drive DB 파일 단위로 락을 잡아 1개 런타임만 작업하도록 직렬화합니다(락을 런 전체 동안 유지).
LOCK_ENABLED = True
LOCK_TIMEOUT_SECONDS = 6 * 60 * 60  # 최대 6시간 대기
LOCK_STALE_SECONDS = 1 * 60 * 60    # 1시간 이상 갱신 없는 락은 stale로 보고 해제 시도
LOCK_HEARTBEAT_SECONDS = 60         # 작업 중 60초마다 락 갱신(touch)
LOCK_DIR = SNAP_DIR / f".lock_{_safe_filename(JUNCTION_NAME)}"

# 2) 작업 디렉터리 이동
os.chdir(ROOT)
print("CWD:", Path.cwd())

# 3) 의존성 설치 (필요 시)
subprocess.run([sys.executable, "-m", "pip", "install", "-r", "colab/requirements_colab.txt"], check=True)


def backup_sqlite(src_path: Path, dst_path: Path, retries: int = 5, sleep_sec: float = 1.0) -> bool:
    """SQLite backup API로 '일관된 스냅샷'을 Drive에 저장."""
    if not src_path.exists():
        return False
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst_path.with_suffix(dst_path.suffix + ".tmp")
    for i in range(max(1, retries)):
        try:
            if tmp.exists():
                tmp.unlink()
            with sqlite3.connect(str(src_path)) as src, sqlite3.connect(str(tmp)) as dst:
                src.backup(dst)
            tmp.replace(dst_path)
            return True
        except Exception as exc:  # noqa: BLE001
            if i == retries - 1:
                print(f"[checkpoint][warn] 저장 실패: {exc}", flush=True)
                return False
            time.sleep(sleep_sec)
    return False


def _touch(path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(datetime.now()), encoding="utf-8")
    except Exception:
        try:
            path.touch()
        except Exception:
            return


def acquire_drive_lock(lock_dir: Path) -> None:
    """
    Drive에 락 디렉터리를 mkdir로 생성(원자적)해서 동시 실행을 직렬화합니다.
    - Colab 런타임이 죽으면 락이 남을 수 있어 stale 판단 후 제거를 시도합니다.
    """
    start = time.time()
    hb = lock_dir / "heartbeat.txt"
    info = lock_dir / "owner.txt"
    while True:
        try:
            lock_dir.parent.mkdir(parents=True, exist_ok=True)
            lock_dir.mkdir()
            info.write_text(f"pid={os.getpid()} time={datetime.now()}\n", encoding="utf-8")
            _touch(hb)
            print(f"[lock] 획득: {lock_dir}", flush=True)
            return
        except FileExistsError:
            # stale lock 처리
            try:
                mtime = hb.stat().st_mtime if hb.exists() else lock_dir.stat().st_mtime
                if (time.time() - mtime) > float(LOCK_STALE_SECONDS):
                    print(f"[lock][warn] stale 락 감지 -> 해제 시도: {lock_dir}", flush=True)
                    for p in (hb, info):
                        try:
                            if p.exists():
                                p.unlink()
                        except Exception:
                            pass
                    try:
                        lock_dir.rmdir()
                        continue
                    except Exception:
                        pass
            except Exception:
                pass

            waited = time.time() - start
            if waited > float(LOCK_TIMEOUT_SECONDS):
                raise TimeoutError(f"락 대기 시간 초과: {lock_dir}")
            print(f"[lock] 다른 작업 실행 중... 대기 {int(waited)}s", flush=True)
            time.sleep(5)
        except Exception as exc:
            raise RuntimeError(f"락 생성 실패: {exc}")


def heartbeat_drive_lock(lock_dir: Path) -> None:
    _touch(lock_dir / "heartbeat.txt")


def release_drive_lock(lock_dir: Path) -> None:
    for p in (lock_dir / "heartbeat.txt", lock_dir / "owner.txt"):
        try:
            p.unlink(missing_ok=True)
        except Exception:
            pass
    try:
        lock_dir.rmdir()
        print(f"[lock] 해제: {lock_dir}", flush=True)
    except Exception:
        return


def safe_copy_drive_to_local(src: Path, dst: Path, retries: int = 3) -> None:
    """
    Drive(/content/drive) -> Colab 로컬(/content) 복사.
    - Drive 마운트가 중간에 끊기면(Transport endpoint...) 재마운트 후 재시도
    - sendfile 기반 fast copy 실패 시 스트리밍 복사로 fallback
    """
    last_exc: Exception | None = None
    for i in range(max(1, int(retries))):
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists():
                dst.unlink()
            try:
                shutil.copy2(src, dst)  # fast path (may use sendfile)
                return
            except OSError:
                # fallback: stream copy (no sendfile)
                with src.open("rb") as fsrc, dst.open("wb") as fdst:
                    shutil.copyfileobj(fsrc, fdst, length=16 * 1024 * 1024)
                return
        except OSError as exc:
            last_exc = exc
            print(f"[warn] Drive->local 복사 실패 ({i+1}/{retries}): {exc}", flush=True)
            print("[info] Drive 재마운트 시도...", flush=True)
            drive.mount("/content/drive", force_remount=True)
            time.sleep(2)
    raise RuntimeError(f"Drive->local 복사 실패: {last_exc}")


# 4) VIDEO를 로컬로 복사(선택)
VIDEO_TO_USE = DRIVE_VIDEO
if USE_LOCAL_VIDEO:
    VIDEO_TO_USE = LOCAL_VIDEO
    if not LOCAL_VIDEO.exists():
        print("Copy Drive video -> local ...", flush=True)
        safe_copy_drive_to_local(DRIVE_VIDEO, LOCAL_VIDEO, retries=3)
    print("VIDEO(local):", VIDEO_TO_USE, "exists:", VIDEO_TO_USE.exists())

# 5) runtime config 생성 (DB 위치 선택)
cfg_src_path = ROOT / "colab" / "app_config_drive.json"
cfg = json.loads(cfg_src_path.read_text(encoding="utf-8"))
cfg["project_name"] = PROJECT_NAME
cfg["junction_name"] = JUNCTION_NAME
cfg["session_name"] = SESSION_NAME
cfg["session_id"] = f"{JUNCTION_NAME} {SESSION_NAME}".strip()

db_path_effective = LOCAL_DB if USE_LOCAL_DB else DRIVE_DB
cfg["db_path"] = str(db_path_effective)
if "count_db_path" in cfg:
    cfg["count_db_path"] = str(db_path_effective)
RUNTIME_CFG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

print("Runtime config:", RUNTIME_CFG)
print("[info] runtime model_path:", cfg.get("model_path"))
print("[info] runtime yolo_imgsz:", cfg.get("yolo_imgsz"))
print("DB(local):", LOCAL_DB)
print("DB(drive):", DRIVE_DB)
print("[info] Drive DB는 교차로 단위 고정 파일로 누적 저장합니다.", flush=True)

lock_held = False
try:
    # 6) 동시 실행 방지: Drive DB에 쓰는 경우, 런 전체 동안 락 유지(append 누락 방지)
    if LOCK_ENABLED and USE_LOCAL_DB and COPY_DB_TO_DRIVE:
        acquire_drive_lock(LOCK_DIR)
        lock_held = True

    # 7) 누적 저장을 위해 기존 Drive DB를 로컬로 복사 (append)
    if USE_LOCAL_DB:
        if LOCAL_DB.exists():
            # 이전 런타임 찌꺼기 제거(의도치 않은 혼합 방지)
            try:
                LOCAL_DB.unlink()
            except Exception:
                pass
        if DRIVE_DB.exists():
            print("Copy existing Drive DB -> local (append) ...", flush=True)
            safe_copy_drive_to_local(DRIVE_DB, LOCAL_DB, retries=3)

    # 8) 실행
    cmd = [
        sys.executable,
        "-u",  # unbuffered stdout for live logs
        "-m",
        "src.pipeline.detect_to_db",
        "--video",
        str(VIDEO_TO_USE),
        "--config",
        str(RUNTIME_CFG),
    ]
    print("Running:", " ".join(cmd))

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    _last_checkpoint_ts = 0.0
    _last_hb_ts = 0.0
    assert proc.stdout is not None
    for line in proc.stdout:
        print(line, end="")
        now = time.time()
        if lock_held and (now - _last_hb_ts) >= float(LOCK_HEARTBEAT_SECONDS):
            _last_hb_ts = now
            heartbeat_drive_lock(LOCK_DIR)
        if CHECKPOINT_ENABLED and USE_LOCAL_DB and COPY_DB_TO_DRIVE and line.startswith("[flush]"):
            if now - _last_checkpoint_ts >= CHECKPOINT_MIN_SECONDS:
                _last_checkpoint_ts = now
                ok = backup_sqlite(LOCAL_DB, DRIVE_DB)
                if ok:
                    print(
                        f"[checkpoint] {datetime.now().strftime('%H:%M:%S')} Drive에 스냅샷 저장: {DRIVE_DB}",
                        flush=True,
                    )

    ret = proc.wait()
    if ret != 0:
        raise subprocess.CalledProcessError(ret, cmd)

    # 9) 최종 저장 (로컬 DB -> Drive)
    if USE_LOCAL_DB and COPY_DB_TO_DRIVE:
        DRIVE_DB.parent.mkdir(parents=True, exist_ok=True)
        backup_sqlite(LOCAL_DB, DRIVE_DB)
        print("DB created/updated (Drive):", DRIVE_DB)
    print("DB created (Local/Effective):", db_path_effective)
finally:
    if lock_held:
        release_drive_lock(LOCK_DIR)

# 9) 소요 시간 계산
end_time = datetime.now(tz)
print(f"🏁 분석 종료 시간: {end_time.strftime('%Y-%m-%d %H:%M:%S')}")
duration = end_time - start_time
runtime.unassign()
