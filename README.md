Count Car Ver5 (Rebuilt)
========================

두 단계 파이프라인:
1) 탐지+추적 → SQLite 로그 저장
2) 저장된 로그를 불러 분석(시간/방향/차종 카운팅)

주요 경로
- `config/app_config.json`: 기본 설정, DB 경로, ROI/라인 파일 위치
- `config/line_settings.sample.json`: 라인/ROI 정의 예시(`bound` 필드로 north/south 등 방향 그룹 지정)
- `config/category_mapping.json`: YOLO 클래스 → 내부 차종 매핑
- `src/ui/`: PySide6 기반 영상 선택, 라인 설정 UI
- `src/pipeline/`: 탐지·추적 처리 및 DB 저장
- `src/db/`: SQLite 스키마와 writer

빠른 실행 흐름
- `python -m src.app` (PySide6 GUI) → 영상 선택 → 라인 설정 → 탐지·추적 실행 → DB 저장
- CLI: `python -m src.pipeline.detect_to_db --video <video_path> --config config/app_config.json`

가상환경/설치(Windows 예시)
```


.venv\Scripts\activate
pip install -r requirements.txt
```

다음 단계
- YOLO 모델 파일을 `models/`에 배치하고 `config/app_config.json`의 `model_path`를 수정하세요.
- 라인/ROI를 `config/line_settings.sample.json` 복사본으로 저장 후 `app_config.json`에서 경로를 지정하세요.
- 스키마를 확장할 때 `schema_version`을 올리고 `src/db/schema.py`를 업데이트하세요.

실행명령
- python -m src.app

리눅스에서 가상환경 활성화 명령
- source /home/shinho/envs/countcar5.0/bin/activate