# Count Car Ver6.0 — 작업 지침

차량 영상 → 탐지·추적 → SQLite 궤적 → 라인 교차 → 시간대·방향·차종별 Excel/CSV를 만드는 PySide6 앱.

## 먼저 확인
- 작업 전 `git status --short`로 기존 변경을 확인하고 보존한다.
- 인수인계·환경·Git 주의사항은 `CLAUDE.md`, 사용자 기능은 `사용설명서.md`의 관련 절만 확인한다. 문서의 파일명·환경 설명은 현재 파일과 대조한다.
- 일반적인 코드 작업에서는 이 파일을 기준으로 관련 모듈부터 읽는다. 매번 전체 문서·소스·노트북을 출력하지 않는다.
- `rg --files`, `rg -n`으로 범위를 좁힌다. 모델, 영상, 결과 DB, Excel, 노트북 출력은 필요할 때만 확인한다.

## 실행·검증
현재 PC의 기존 환경은 `C:\envs\countcar5.0` (Python 3.10.8)이다. 시스템 `python` 대신 명시적 경로를 쓴다. 다른 PC에서는 사용 가능한 호환 환경부터 확인한다.

```powershell
& 'C:\envs\countcar5.0\Scripts\python.exe' -m src.app
$env:QT_QPA_PLATFORM = 'offscreen'
& 'C:\envs\countcar5.0\Scripts\python.exe' -m unittest discover -s tests -t . -v
```

- v5와 공유하는 환경이므로 패키지를 임의 업그레이드하지 않는다. 테스트는 `unittest`를 사용한다.
- 변경한 기능의 회귀 테스트부터 실행한다. 탐지·DB·집계 공통 변경은 전체 테스트도 확인한다.
- 실제 모델·영상 없이 한 검증과 실제 영상 분석을 구분해 보고한다. 코드 변경 없는 문서 수정에 전체 영상 분석은 필요 없다.

## 작업별 시작점
| 작업 | 코드 | 관련 테스트 |
|---|---|---|
| 공통 탐지 실행·세션 | `src/services/detection_service.py` | `test_detection_single_entry.py`, `test_detection_overwrite.py` |
| 프레임·추적·차종 안정화 | `src/pipeline/detect_track.py` | `test_track_storage.py`, `test_detection_lifecycle.py` |
| 구형/신형 DB 조회 | `src/db/trajectories.py`, `queries.py` | `test_legacy_trajectories.py` |
| 집계·교차·방향 | `src/pipeline/count_tracks.py`, `geometry.py` | `test_counting_logic.py`, `test_counting_aggregation.py` |
| 병합·가상 이벤트·되돌리기 | `src/pipeline/track_merge.py`, `virtual_events.py`, `src/ui/trajectory_viewer2.py` | `test_track_storage.py`, `test_trajectory_undo.py` |
| 설정·모델·Colab 내보내기 | `src/config/`, `src/services/colab_export.py` | `test_config_validation.py`, `test_model_profiles.py` |
| 이미지 추출 | `src/pipeline/image_extractor.py` | `test_image_extractor.py` |
| GUI·백그라운드 작업 | `src/app.py`, `src/ui/workers.py`, `widgets.py`, `theme.py` | 관련 로직 테스트와 offscreen 확인 |

테스트 경로는 모두 `tests/` 아래다.

## 유지해야 할 원칙
- GUI·CLI·Colab 탐지는 반드시 `DetectionService`를 거친다. 탐지 로직을 UI에 복제하지 않는다.
- 탐지 좌표는 원본 영상 기준이다. ROI·라인·`in_point`·해상도를 함께 확인하고 중복 스케일링을 피한다. 2점 라인과 폴리라인 호환성을 유지한다.
- 새 궤적은 `TrackTrajDBWriter`로 저장한다. 구형 `tracks` 읽기도 보존한다. SQLite 연결은 명시적으로 닫고 스키마 변경에는 버전·마이그레이션을 반영한다.
- 원본/후처리 기준, 세션별 데이터, 자동/수동 병합·가상 이벤트의 소유 범위를 구분한다. 삭제·되돌리기가 다른 범위를 침범하면 안 된다.
- 공유 기본값은 `config/app_config.json`의 상대경로, PC별 경로·UI 상태는 Git 제외 대상 `config/user_state.json`에 저장한다.
- UI 크기는 `fit_to_screen`, 테마는 `theme.py`의 공통 스타일을 사용한다. 스크롤은 필요한 패널에만 적용한다.
- 예외를 억제할 때 `logger.debug(..., exc_info=True)`로 근거를 남긴다. 텍스트는 UTF-8을 유지한다.
- `git stash`를 사용하지 않는다. 줄바꿈만 바뀐 수정은 `git diff --numstat <파일>`로 확인한다. 커밋·push는 요청 범위에서만 수행한다.
- 모델·영상·결과 DB·Excel·로컬 상태를 임의 삭제하거나 커밋하지 않는다.

이 파일에는 안정적인 규칙과 경로만 유지한다. 상세 검토 결과·작업 이력·긴 로그는 별도 문서에 두고 필요할 때 읽는다.
