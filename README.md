# Count Car Ver6.0

영상에서 차량을 탐지·추적하여 SQLite에 저장하고, 분석라인 교차 결과를 시간대·방향·차종별 Excel/CSV로 집계하는 프로그램입니다.

GUI와 Colab/CLI는 서로 다른 탐지 코드를 사용하지 않습니다. 모두 공통 `DetectionService`를 호출하므로, 로컬 PC에서는 GUI로 설정하고 연산 성능이 필요한 작업은 Colab GPU에서 같은 로직으로 실행할 수 있습니다.

## 빠른 시작

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m src.app
```

CLI 예시:

```powershell
python -m src.pipeline.detect_to_db --video "영상.mp4" --config config/app_config.json --line-settings config/lines/line_settings.sample.json
```

GUI, CLI, Colab, 분석라인 설정, 카운팅 및 문제 해결 방법은 [사용설명서.md](사용설명서.md)를 확인하세요.

## 주요 경로

- `src/services/detection_service.py`: GUI·CLI·Colab 공통 탐지 서비스
- `src/services/colab_export.py`: 현재 GUI 설정을 Colab 설정·실행 셀로 생성
- `src/config/model_profiles.py`: 모델별 차종 프로파일 (`config/model_profiles.json`)
- `src/pipeline/`: 탐지, 추적, 병합, 가상 이벤트, 카운팅
- `src/db/`: SQLite 스키마와 저장기
- `src/ui/`: PySide6 GUI
- `config/`: 공용 기본 설정
- `config/lines/`: 현장별 분석라인
- `colab/`: Colab 노트북과 전용 설정
- `tests/`: 핵심 회귀 테스트

모델, 결과 DB, Excel 파일은 용량이 크거나 현장 데이터일 수 있으므로 Git에 올리기 전에 반드시 포함 범위를 확인하세요.
