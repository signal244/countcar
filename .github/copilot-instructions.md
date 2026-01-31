# Copilot instructions for Count Car Ver5

요약
- 이 레포는 2단계 파이프라인 구조: (1) 탐지+추적 → SQLite 저장, (2) 저장된 로그 불러 분석(카운팅/엑셀 출력).
- GUI: `src/app.py` (PySide6). 배치/CLI 파이프라인: `src/pipeline/detect_to_db.py` 및 `src/pipeline/detect_track.py`.

핵심 설계(빅픽처)
- 입력 → YOLO(모델: `models/*`) → 트래커(botsort/bytetrack 설정, `config/*.yaml`) → DB(`config/app_config.json`의 `db_path`).
- 좌표계/ROI 보정은 `src/pipeline/detect_to_db.py:build_tracker`에 집중되어 있음 — ROI가 "리사이즈 좌표계"로 저장된 경우 자동 스케일 업 처리.
- DB 스키마와 압축된 궤적 처리는 `src/db/schema.py` 및 `src/db/writer.py`를 확인. `track_trajs` 테이블 유무에 따라 처리 분기 존재.
- 카운팅 로직(재연결·외삽·슬로팅)은 `src/pipeline/count_tracks.py`에 구현되어 있음. 라인 정규화, bound 기반 in/out 판단 등 도메인 규칙이 포함됨.

개발자 워크플로우(실행/디버깅)
- GUI 실행: `python -m src.app`
- CLI 탐지+추적 → DB: `python -m src.pipeline.detect_to_db --video <video> --config config/app_config.json`
  - `--line-settings`로 라인 파일 지정 가능
  - `--overwrite-session`로 기존 session 덮어쓰기 가능 (또는 config의 `overwrite_session`).
- 모델 파일은 `models/`에 배치하고 `config/app_config.json`의 `model_path`에 경로 지정.
- 가상환경(리눅스 예): `source /home/shinho/envs/countcar5.0/bin/activate` 후 `pip install -r requirements.txt`.

프로젝트 관례/주의사항(프로젝트 특이점)
- 라인/ROI 좌표계 혼동 주의: `line_settings`에 저장된 `image_width`/`image_height`와 `yolo_imgsz`/resize 설정을 함께 고려해야 함. 관련 코드는 `build_tracker`에 주석과 보정 로직이 있음.
- session_id 충돌 처리: 기본은 충돌 시 타임스탬프 접미하여 새 세션 생성. 덮어쓰려면 `--overwrite-session` 사용.
- 클래스 매핑: `config/category_mapping.json`을 통해 YOLO 클래스→내부 `vehicle_type` 매핑 사용. `config/app_config.json`의 `class_mapping_path`를 확인.
- DB 쓰기: `TrackTrajDBWriter` 컨텍스트 사용, 주기적 flush 설정(`flush_interval_minutes`)이 config에 있음.

통합 포인트(외부 의존성)
- YOLO 모델 파일(예: `models/yolov8m.pt`) — 모델이 없으면 `build_tracker`에서 FileNotFoundError 발생.
- 트래커 구성 파일: `config/botsort_stable.yaml` 또는 `config/bytetrack.yaml`.
- GUI 의존성: `PySide6`.
- 동영상 IO: 코드에서 `cv2`(OpenCV)를 사용해 프레임 카운트 탐침.

코드 작성·수정 시 체크리스트(구체적)
- 새 config 키 추가 시 `config/app_config.json` 기본값과 `src/app.py` UI 바인딩을 같이 업데이트할 것.
- 라인/ROI 변화는 `line_settings` 파일 포맷(예: `points`, `bound`, `image_width`/`image_height`)을 준수할 것.
- DB 스키마 변경은 `src/db/schema.py`의 `schema_version` 규약을 따르고 마이그레이션 필요 시 `init_db` 동작을 점검.
- 성능 관련 파라미터는 `config/app_config.json`에서 제어(예: `max_idle_frames`, `yolo_imgsz`, `target_fps`).

예제(자주 쓰는 명령)
- GUI: `python -m src.app`
- 탐지→DB(라인 파일 지정):
  `python -m src.pipeline.detect_to_db --video /path/to/video.mp4 --config config/app_config.json --line-settings config/line_settings.sample.json`

실행 시나리오 (빠른 시작 예제)

- 1) 개발자용 — GUI로 빠르게 확인
  - 가상환경 활성화 및 의존성 설치:
    `source /home/shinho/envs/countcar5.0/bin/activate && pip install -r requirements.txt`
  - 모델 배치: 원하는 YOLO 파일을 `models/`에 넣고 `config/app_config.json`의 `model_path`를 맞춥니다. (또는 GUI의 `모델 선택` 버튼 사용)
  - GUI 실행:
    `python -m src.app`

- 2) 배치 탐지→DB (무인 실행)
  - 기본 실행(라인 설정 포함):
    `python -m src.pipeline.detect_to_db --video /path/to/video.mp4 --config config/app_config.json --line-settings config/line_settings.sample.json`
  - 선택적 플래그:
    - `--session-id "Junction 20260131"` : 세션 명시
    - `--overwrite-session` : 같은 session_id 기존 데이터 덮어쓰기
    - `--flush-minutes 5|15|30|60` : DB 쓰기 flush 간격 강제 지정

- 3) 저장된 DB로 카운팅/엑셀 출력
  - 기본 실행:
    `python -m src.pipeline.count_tracks --db output/tracks.sqlite --lines config/line_settings.sample.json --interval-min 15 --out-csv output/counts.csv`
  - 결과: 엑셀(`.xlsx`)과 선택적 CSV가 생성됩니다. `--mode approach`로 접근로 모드 선택 가능.

- 4) Colab/노트북 사용
  - `colab/` 폴더의 노트북은 모델/데이터 경로가 Colab에 맞게 설정되어 있으므로, 로컬 모델을 Drive로 올리거나 `app_config_colab.json`을 사용해 경로를 맞춰주세요.

참고 파일
- [config/app_config.json](config/app_config.json) — 앱 기본 설정
- [src/app.py](src/app.py) — GUI 진입점, `PipelineWorker`가 파이프라인 실행
- [src/pipeline/detect_to_db.py](src/pipeline/detect_to_db.py) — 탐지·추적 빌드/실행
- [src/pipeline/count_tracks.py](src/pipeline/count_tracks.py) — 카운팅/후처리 핵심 알고리듬
- [src/db/writer.py](src/db/writer.py) & [src/db/schema.py](src/db/schema.py) — DB 관련

원하시면 다음으로 아래 항목을 업데이트하겠습니다:
- GUI/CLI 실행 가이드의 예시 명령(사용자 환경 맞춤)
- `config/app_config.json` 필드 설명(주요 필드 문서화)

피드백 요청: 이 파일의 어느 부분을 더 구체화할까요? (예: config 필드별 설명, 주요 함수별 코드 예시 등)

**config/app_config.json 필드 설명**
- **`schema_version`**: DB/설정 스키마 버전 표시. 마이그레이션 시 증가.
- **`camera_id`**: 트래커에서 사용하는 카메라 식별자(예: `cam01`).
- **`db_path`**: 탐지→트래킹 결과를 저장할 SQLite 파일 경로. 기본 예: `output/tracks.sqlite`.
- **`model_path`**: YOLO 모델 파일 경로(예: `models/best.pt`). `build_tracker`에서 존재 여부 확인함.
- **`tracker_config`**: 트래커 설정 파일(`config/botsort_stable.yaml` 또는 `config/bytetrack.yaml`).
- **`yolo_imgsz`**: YOLO 입력 이미지 크기(정수). `build_tracker`에서 ROI/라인 스케일 추정에 사용.
- **`yolo_rect`**: YOLO에서 rect(정사각) 입력 사용 여부(boolean).
- **`target_fps`**: 탐지/트래킹 목표 FPS (float). 타임스탬프/속도 계산에 참고.
- **`confidence_threshold`**: 탐지 신뢰도 컷오프(0.0-1.0).
- **`allowed_classes`**: YOLO 클래스 인덱스 리스트(예: [1,2,3...]) — 탐지 필터링에 사용.
- **`device`**: 디바이스 지정 (`cpu`, `cuda:0`, `auto` 등). `src/config/device.py`의 `resolve_device`로 처리됨.
- **`line_settings_path`**: 기본 라인/ROI JSON 경로(에디터/GUI에서 사용).
- **`class_mapping_path`**: YOLO 클래스명을 내부 `vehicle_type`으로 매핑하는 JSON 경로(`config/category_mapping.json`).
- **`roi`**: 기본 ROI 배열(리스트). `build_tracker`에서 `line_settings`와 함께 좌표계 보정 로직이 있음.
- **`flush_interval_minutes`**: DB 쓰기(TrackTrajDBWriter) flush 간격(분).
- **`max_idle_frames`**: 트랙 종료 판단을 위한 최대 무동작 프레임 수(프레임 단위).
- **카운팅 관련 옵션들**:
  - **`count_db_path` / `count_db_dir`**: 카운팅/엑셀 생성 시 기본 DB 스냅샷 경로/디렉토리.
  - **`count_lines_path`**: 카운팅에 사용할 라인 파일 경로(다른 경로를 지정할 수 있음).
  - **`count_interval_minutes`**: 집계 슬롯 길이(분). (`run_count`의 `--interval-min`)
  - **`count_reconnect_enabled` / `count_reconnect_dist` / `count_reconnect_gap` / `count_reconnect_passes`**: 단일 교차 트랙 재연결(reconnect) 동작 제어 (거리(px), 시간(초), 시도 횟수).
  - **`count_extrap_enabled` / `count_extrap_horizon`**: 외삽 옵션(궤적 앞뒤로 연장해 라인 교차 판단), horizon은 픽셀 단위 거리.
- **세션/메타 정보**:
  - **`project_name`**, **`junction_name`**, **`session_name`**, **`session_id`**: GUI와 DB에서 세션 식별용으로 조합되어 사용됨. `PipelineWorker`는 `session_id` 충돌 시 타임스탬프를 붙여 새 ID를 생성하거나 `--overwrite-session`으로 덮어쓰기함.
  - **`head_seconds` / `tail_seconds`**: 프레임 앞뒤 보정(초) — UI/후처리에서 사용.
- **YOLO/뷰어 옵션**:
  - **`yolo_version`**: 표시형 옵션(v8/v11 등). 코드 여러 위치에서 UI 표시용으로 쓰임.
  - **`trajectory_window_geometry` / `trajectory_splitter_sizes` / `trajectory_viewer_options`**: GUI의 궤적 뷰어 레이아웃 및 뷰어 기본값. `trajectory_viewer_options` 내부에는 `db_path`, `lines_path`, `video_path`, `track_width`, `frame_step`, `max_tracks`, `bg_enabled`, `session_id`, `slot_index` 등이 포함되어 있음.
- **`count_output_dir`**: 카운팅 결과(`.xlsx`)의 기본 출력 디렉토리.

참고: 이 파일의 경로 값은 Windows 스타일 절대경로(예: `G:/...`)가 포함될 수 있으므로 리눅스/서버 환경에서는 workspace 상대 경로 또는 프로젝트 내 경로로 교체하는 것이 권장됩니다. `build_tracker`와 `run_count`는 `line_settings`의 `image_width`/`image_height`와 `yolo_imgsz`/resize 설정의 조합으로 라인/ROI 좌표계를 스케일링하므로, 라인 파일의 좌표계(원본 vs 리사이즈)를 항상 확인하세요.
