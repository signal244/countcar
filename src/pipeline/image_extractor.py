"""영상 프레임 추출 + YOLO 라벨 생성의 순수 로직.

원래 src/app.py 의 MainWindow.on_extract_images (300줄 넘는 단일 메서드)에
GUI 다이얼로그와 섞여 있던 로직을 분리한 것이다. GUI 는 입력 수집만 하고
이 모듈의 함수를 호출한다. 여기 로직은 self·위젯에 의존하지 않아 단위
테스트가 가능하다.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Iterable, List, Optional

# 클래스 이름 -> YOLO 클래스 id. 메인 파이프라인과 같은 정책을 쓴다.
CLASS_NAME_TO_ID = {
    "person": 0,
    "small_bus": 1,
    "passenger_car": 2,
    "medium_truck": 3,
    "large_truck": 4,
    "large_bus": 5,
    "etc": 6,
    "small_truck": 7,
    # 범용 모델용 폴백 이름
    "car": 2,
    "bus": 5,
    "truck": 4,
}

# 한글 매핑 이름 -> YOLO 클래스 id.
MAPPED_NAME_TO_ID = {
    "소형버스": 1,
    "승용차": 2,
    "중형화물": 3,
    "대형화물": 4,
    "대형버스": 5,
    "기타": 6,
    "소형화물": 7,
}

# 라벨을 만들지 않고 건너뛸 이름(사람 포함).
IGNORE_LABELS = {"ignore", "ignored", "none", "", "person"}

LogCallback = Callable[[str], None]


def next_start_index(images_dir: Path, prefix: str) -> int:
    """images_dir 에 이미 있는 {prefix}_NNNNNN.jpg 다음 번호를 돌려준다.

    이어받기(resume) 용. 파일이 없으면 1 부터 시작한다.
    """
    import re

    start_idx = 1
    try:
        pat = re.compile(rf"^{re.escape(prefix)}_(\d{{6}})\.jpg$", re.IGNORECASE)
        max_idx = 0
        for p_img in images_dir.glob(f"{prefix}_*.jpg"):
            m = pat.match(p_img.name)
            if not m:
                continue
            try:
                max_idx = max(max_idx, int(m.group(1)))
            except ValueError:
                continue
        if max_idx > 0:
            start_idx = max_idx + 1
    except Exception:
        return 1
    return start_idx


def extract_frames(
    cap,
    images_dir: Path,
    prefix: str,
    step: int,
    first_n_limit: int,
    start_idx: int,
    log_cb: Optional[LogCallback] = None,
    imwrite: Optional[Callable[[str, object], bool]] = None,
) -> List[Path]:
    """cap 에서 step 간격으로 프레임을 읽어 jpg 로 저장한다.

    - first_n_limit>0 이면 그만큼 저장 후 멈춘다.
    - imwrite 를 주입하면 cv2 없이도 테스트할 수 있다(기본은 cv2.imwrite).
    - cap 은 항상 release() 된다.
    반환: 이번 실행에서 저장한 이미지 경로 목록.
    """
    def log(msg: str) -> None:
        if log_cb is not None:
            log_cb(msg)

    if imwrite is None:
        import cv2

        imwrite = cv2.imwrite

    frame_id = 0
    saved = 0
    img_idx = start_idx
    extracted_paths: List[Path] = []
    try:
        while True:
            ok, frame = cap.read()
            if not ok or frame is None:
                break
            if frame_id % step == 0:
                out_path = images_dir / f"{prefix}_{img_idx:06d}.jpg"
                if bool(imwrite(str(out_path), frame)):
                    saved += 1
                    img_idx += 1
                    extracted_paths.append(out_path)
                    if first_n_limit > 0 and saved >= first_n_limit:
                        break
                else:
                    log(f"[extract:warn] save failed: {out_path}")
            frame_id += 1
            if frame_id % 2000 == 0:
                log(f"[extract:progress] frame={frame_id} saved={saved}")
    finally:
        cap.release()
    return extracted_paths


def detection_to_yolo_lines(
    boxes: Iterable,
    names: dict,
    class_mapping: dict,
) -> List[str]:
    """YOLO 탐지 박스들을 YOLO 라벨 txt 줄 목록으로 변환한다.

    - names: 모델의 class id -> 이름 사전(label_model.names).
    - class_mapping: 원본 이름 -> 매핑 이름(category_mapping.json).
    - person(id 0)과 IGNORE_LABELS 는 제외한다.
    - box 는 .cls[0], .xywhn[0].tolist() 를 제공하는 객체(ultralytics Box).
    """
    lines: List[str] = []
    for box in boxes:
        try:
            cls_raw = int(box.cls[0])
            cls_name = str(names.get(cls_raw, str(cls_raw))).strip().lower()
            mapped_name = class_mapping.get(cls_name, cls_name)
            if mapped_name in IGNORE_LABELS:
                continue
            cls = CLASS_NAME_TO_ID.get(cls_name)
            if cls is None:
                cls = CLASS_NAME_TO_ID.get(mapped_name)
            if cls is None:
                cls = MAPPED_NAME_TO_ID.get(mapped_name, cls_raw)
            if cls == 0:
                continue
            x_c, y_c, w, h = box.xywhn[0].tolist()
        except Exception:
            continue
        lines.append(f"{cls} {x_c:.6f} {y_c:.6f} {w:.6f} {h:.6f}\n")
    return lines
