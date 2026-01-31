"""
(Colab) 영상 -> 이미지 프레임 추출 (탐지/추적/DB 없이)

- 추출 FPS = (사용자 입력 FPS) / 10
- 저장 위치 = 결과폴더/images
- 파일명 = 교차로명_000001.jpg ...
"""

from __future__ import annotations

import os
from pathlib import Path

import cv2
from google.colab import drive


def main() -> None:
    drive.mount("/content/drive", force_remount=True)

    project_name = (input("프로젝트명(예: 창원공사중(분석용)): ").strip() or "창원공사중(분석용)").strip()
    junction_name = (input("교차로명(예: 상남사거리): ").strip() or "상남사거리").strip()
    session_name = (input("첨두시간대(예: 점심첨두): ").strip() or "점심첨두").strip()

    # 입력 FPS(분석설정) 기준으로 1/10로 추출
    try:
        target_fps = float(input("분석 FPS(예: 10): ").strip() or "10")
    except Exception:
        target_fps = 10.0
    extract_fps = max(0.1, target_fps / 10.0)

    video_filename = f"{junction_name} {session_name}.mp4"
    video_path = Path(f"/content/drive/MyDrive/video/{project_name}/{video_filename}")
    print("VIDEO:", video_path, "exists:", video_path.exists())
    if not video_path.exists():
        raise FileNotFoundError(f"Video not found: {video_path}")

    out_root = (input("결과 저장폴더(Drive 경로, 예: /content/drive/MyDrive/output): ").strip() or "").strip()
    if not out_root:
        out_root = str(Path("/content/drive/MyDrive") / "output")
    out_dir = Path(out_root).expanduser()
    images_dir = out_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)

    safe_junction = "".join("_" if c in '<>:"/\\|?*' else c for c in junction_name).strip()
    safe_session = "".join("_" if c in '<>:"/\\|?*' else c for c in session_name).strip()
    safe_junction = "_".join(safe_junction.split())
    safe_session = "_".join(safe_session.split())
    if safe_junction and safe_session:
        prefix = f"{safe_junction}_{safe_session}"
    else:
        prefix = safe_junction or safe_session or "junction"

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"Failed to open: {video_path}")
    src_fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
    if src_fps <= 0:
        src_fps = 30.0
    step = max(1, int(round(src_fps / extract_fps)))

    # 이어서 저장 (기존 파일이 있으면 다음 번호부터)
    start_idx = 1
    try:
        import re

        pat = re.compile(rf"^{re.escape(prefix)}_(\d{{6}})\.jpg$", re.IGNORECASE)
        max_idx = 0
        for p in images_dir.glob(f"{prefix}_*.jpg"):
            m = pat.match(p.name)
            if not m:
                continue
            try:
                max_idx = max(max_idx, int(m.group(1)))
            except Exception:
                continue
        if max_idx > 0:
            start_idx = max_idx + 1
    except Exception:
        pass

    print(f"[extract] src_fps={src_fps:.2f} target_fps={target_fps:.2f} extract_fps={extract_fps:.2f} step={step}")
    print(f"[extract] out_dir={images_dir}")

    frame_id = 0
    saved = 0
    img_idx = start_idx
    while True:
        ok, frame = cap.read()
        if not ok or frame is None:
            break
        if frame_id % step == 0:
            out_path = images_dir / f"{prefix}_{img_idx:06d}.jpg"
            cv2.imwrite(str(out_path), frame)
            saved += 1
            img_idx += 1
        frame_id += 1
        if frame_id % 2000 == 0:
            print(f"[extract] frame={frame_id} saved={saved}")
    cap.release()
    print(f"[extract] done. saved={saved}")


if __name__ == "__main__":
    main()
