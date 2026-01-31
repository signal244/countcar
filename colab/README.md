Colab 실행 가이드
=================

이 폴더는 GUI 없이 Colab에서 탐지+추적→DB 저장을 빠르게 돌리기 위한 최소 설정입니다.

0) 런타임 설정  
- 런타임 유형: GPU
- 새 런타임에서 시작

1) 코드 준비  
```bash
%cd /content
!git clone --depth 1 https://github.com/<your-repo>/count_car_ver5.0.git
%cd count_car_ver5.0
```
또는 zip 업로드 후 `/content/count_car_ver5.0`에 풀어둡니다.

2) 의존성 설치(Colab용 최소)  
```bash
%cd /content/count_car_ver5.0
!pip install -r colab/requirements_colab.txt
```

3) 모델/데이터 배치  
- 모델: `/content/models/yolov8m.pt` 위치에 두세요. (경로를 `colab/app_config_colab.json`의 `model_path`와 일치시킵니다.)
- 라인 설정: 기본 `config/line_settings.sample.json`을 사용하거나 필요에 맞게 수정.
- 입력 영상: 예) `/content/test.mp4`

4) 실행 예시 (CLI)  
```bash
%cd /content/count_car_ver5.0
!python -m src.pipeline.detect_to_db \
    --video /content/test.mp4 \
    --config colab/app_config_colab.json \
    --line-settings config/line_settings.sample.json
```
실행 후 SQLite 로그는 `/content/output/tracks.sqlite`에 생성됩니다.

참고  
- Colab에서는 PySide6 GUI를 사용하지 않습니다. CLI만 실행하세요.  
- `allowed_classes`와 `resize_height`(32 배수 736) 등은 `colab/app_config_colab.json`에서 조정 가능합니다.  
- tracker_config는 `config/botsort_stable.yaml`을 사용하도록 설정되어 있습니다.
