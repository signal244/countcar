"""
(Colab) 추적 설정 A/B 테스트: 같은 테스트 영상을 A(현재 설정)와 B(변경 설정)로 각각 돌려 비교한다.

- 결과 DB는 output/ab_test/<TEST_NAME>/ 에 따로 저장하며, 현장 DB(output/db_snapshots)에는 손대지 않는다.
- 이미 끝난 변형(DB 파일이 있는 것)은 건너뛰므로, 세션이 끊기면 다시 실행하면 이어서 진행된다.
  새 테스트를 할 때는 TEST_NAME 을 바꾼다.

이력: 2026-10-03 추적 설정 테스트(proximity_thresh 0.3, max_idle_frames 90, confidence 0.1)
      -> B 채택, 기본 설정에 반영. 결과는 output/ab_test/ 바로 아래에 있다.
      2026-10-08 02_reid_onoff: GPU 에서 ReID 켬(A) vs 끔(B). 모델·크기·정밀도는 app_config_colab.json
      (yolo8m_v1, 1920, fp16). 정확도 차이 없이 끔이 19% 빨라 기본값을 ReID 끔(botsort_noreid)으로 바꿈.
      이제 A(설정 그대로)는 ReID 끔이다. 다시 비교하려면 TEST_NAME 을 새 이름으로 바꾸고
      B 를 {"tracker_config": "config/botsort_stable.yaml"} (ReID 켬)으로 둔다.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from google.colab import drive, runtime

drive.mount("/content/drive")

# ===== 테스트 설정 =====
VIDEO_REL = "AB테스트/경원교사거리_오후첨두_10-35분.mp4"   # MyDrive/video/ 아래 경로
LINES_REL = "config/lines/경원교/경원교(3방향).json"
TEST_NAME = "02_reid_onoff"
VARIANTS = {
    "A": {},  # app_config_colab.json 그대로 (BoT-SORT + ReID)
    "B": {"tracker_config": "config/botsort_noreid.yaml"},  # 같은 BoT-SORT 에서 ReID 만 끔
}
TRUTH_REL = "release/sample/정답_15-30분.json"  # 이 클립 5~20분 구간의 확정 교통량
UNASSIGN_AT_END = True  # 끝나면 GPU 런타임 반납
# =======================

if not VARIANTS["B"]:
    raise SystemExit("VARIANTS['B'] 에 시험할 변경을 먼저 넣으세요.")

ROOT = Path("/content/drive/MyDrive/vm/count_car_ver6.0")
DRIVE_VIDEO = Path("/content/drive/MyDrive/video") / VIDEO_REL
OUT_DIR = ROOT / "output" / "ab_test" / TEST_NAME
LOCAL_VIDEO = Path("/content/ab_input.mp4")

os.chdir(ROOT)
sys.path[:0] = [str(ROOT), str(ROOT / "colab")]
from colab_io import backup_sqlite  # noqa: E402

subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-r", "colab/requirements_colab.txt", "lap"], check=True)
OUT_DIR.mkdir(parents=True, exist_ok=True)

print("VIDEO:", DRIVE_VIDEO, "exists:", DRIVE_VIDEO.exists(), flush=True)
if not LOCAL_VIDEO.exists():
    shutil.copy2(DRIVE_VIDEO, LOCAL_VIDEO)

base_cfg = json.loads((ROOT / "colab" / "app_config_colab.json").read_text(encoding="utf-8"))


def run_streaming(cmd: list[str]) -> None:
    # Colab 셀은 자식 프로세스의 stdout 을 직접 보여주지 않아 파이프로 받아 출력한다.
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    assert proc.stdout is not None
    for line in proc.stdout:
        print(line, end="", flush=True)
    if proc.wait() != 0:
        raise subprocess.CalledProcessError(proc.returncode, cmd)


def run_variant(name: str, overrides: dict) -> Path:
    drive_db = OUT_DIR / f"ab_{name}.sqlite"
    if drive_db.exists():
        print(f"[{name}] 이미 완료됨 -> 건너뜀: {drive_db}", flush=True)
        return drive_db

    local_db = Path(f"/content/ab_{name}.sqlite")
    local_db.unlink(missing_ok=True)
    cfg = dict(base_cfg)
    cfg.update(overrides)
    cfg["db_path"] = str(local_db)
    cfg["count_db_path"] = str(local_db)
    cfg_path = Path(f"/content/ab_config_{name}.json")
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT_DIR / f"ab_config_{name}.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n===== [{name}] 시작 {datetime.now():%H:%M:%S}  변경: {overrides or '없음(현재 설정)'} =====", flush=True)
    cmd = [
        sys.executable, "-u", "-m", "src.pipeline.detect_to_db",
        "--video", str(LOCAL_VIDEO),
        "--config", str(cfg_path),
        "--line-settings", LINES_REL,
        "--session-id", f"AB_{name}",
    ]
    t0 = time.time()
    run_streaming(cmd)
    times_path = OUT_DIR / "ab_times.json"
    times = json.loads(times_path.read_text(encoding="utf-8")) if times_path.exists() else {}
    times[name] = round(time.time() - t0, 1)
    times_path.write_text(json.dumps(times), encoding="utf-8")

    if not backup_sqlite(local_db, drive_db):
        raise RuntimeError(f"Drive 저장 실패: {drive_db}")
    print(f"[{name}] 완료 {datetime.now():%H:%M:%S} ({times[name] / 60:.1f}분) -> {drive_db}", flush=True)
    return drive_db


dbs = {name: run_variant(name, ov) for name, ov in VARIANTS.items()}

print("\n===== 비교 결과 =====", flush=True)
run_streaming(
    [
        sys.executable, "colab/ab_compare.py",
        "--a", str(dbs["A"]), "--b", str(dbs["B"]),
        "--lines", LINES_REL,
        "--truth", TRUTH_REL,
        "--config", "colab/app_config_colab.json",
        "--times", str(OUT_DIR / "ab_times.json"),
        "--out", str(OUT_DIR / "ab_result.txt"),
    ]
)
print(f"\n결과 저장: {OUT_DIR / 'ab_result.txt'}")

if UNASSIGN_AT_END:
    runtime.unassign()
