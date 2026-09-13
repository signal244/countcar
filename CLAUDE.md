# CLAUDE.md

영상에서 차량을 탐지·추적해 SQLite에 저장하고, 분석라인 교차 결과를
시간대·방향·차종별로 집계하는 PySide6 데스크톱 프로그램.

GUI·CLI·Colab 이 **서로 다른 탐지 코드를 쓰지 않는다.** 모두 공통
`src/services/detection_service.py` 를 호출한다. 탐지 로직을 고칠 때
GUI 쪽에만 반영하는 실수를 하지 말 것.

## 실행 환경

가상환경은 프로젝트 밖에 있다. **`python` 을 그냥 쓰면 안 된다** —
시스템 기본이 Python 3.14 인데 ultralytics/PySide6 가 지원하지 않는다.

```powershell
C:\envs\countcar5.0\Scripts\Activate.ps1     # Python 3.10.8
python -m src.app
```

- 이 환경은 v5.0 과 **공유**한다. 패키지를 올리면 v5.0 이 깨질 수 있다.
- torch 는 **CPU 빌드**다. 무거운 연산은 `colab/` 노트북에서 돌리는 것이 설계 의도.

### 테스트

`pytest` 는 설치되어 있지 않다. unittest 를 쓴다.

```powershell
python -m unittest discover -s tests -t .
```

GUI 코드를 검증할 때는 오프스크린으로 띄운다:

```powershell
$env:QT_QPA_PLATFORM = "offscreen"
```

## Git — 작업 환경이 두 곳이다

```
[A] 이 폴더 (G:\내 드라이브\... , VS Code)   [B] 다른 환경의 Claude
    커밋 작성자: signal244                      커밋 작성자: Claude
                    \                          /
                     >  github.com/signal244/countcar  <
                        브랜치: claude/unknown-session-l2oio4
```

VS Code 안의 Claude Code 는 [A] 와 **같은 폴더**에서 작업한다. 파일을
고치면 즉시 사용자 디스크에 반영되므로, 그 변경을 받으려고 pull 할
필요가 없다. `pull` 이 필요한 건 [B] 가 푸시했을 때뿐이다.

어느 환경 커밋인지 확인: `git log --pretty='%h %an %s' -5`

### 반드시 지킬 것

- **`git stash` 를 쓰지 말 것.** 작업이 유실되기 쉽다. pull 전에 변경이
  남아 있으면 stash 가 아니라 **먼저 커밋**한다.
- **push 는 사용자 확인을 받고 한다.** 외부로 나가는 작업이다.
- `git add -A` 전에 `git status` 로 무엇이 올라가는지 확인한다.

### 유령 수정 파일

`core.autocrlf=true` + 구글드라이브 동기화 때문에 **내용 변경이 없는데도**
`git status` 에 `M` 으로 뜨는 파일이 자주 생긴다. 줄바꿈(CRLF/LF) 차이일 뿐이다.

```powershell
git diff --numstat <파일>     # 출력이 비면 실제 변경 없음
```

사용자에게 "변경이 유실된 것 같다"고 보고하기 전에 **반드시 이것부터 확인할 것.**
`git add -A` 하면 이런 파일은 스테이징에서 알아서 빠진다.

### 주의

`.git` 폴더가 구글드라이브 안에 있다. 동기화 중 git 명령 실행 시 드물게
레포가 손상될 수 있다.

## 코드 관례

### UI 크기 대응

새 창을 만들 때는 `src/ui/widgets.py` 의 공통 헬퍼를 쓴다. 직접
`resize()` 하거나 큰 `setMinimumSize()` 를 걸지 말 것 — 작은 노트북
화면이나 125%+ 배율에서 창이 화면 밖으로 나가거나 잘린다.

```python
from src.ui.widgets import fit_to_screen, wrap_in_scroll

fit_to_screen(self, 1100, 800)            # resize() 대신
self.setCentralWidget(wrap_in_scroll(central))
```

- `QTableWidget`/`QListWidget` 이 주 내용인 창은 **감싸지 않는다.**
  자체 스크롤이 있어서 중첩되면 오히려 불편해진다. `fit_to_screen` 만 적용.
- `QSplitter` 로 짜인 창(`trajectory_viewer2.py`)은 창 전체를 감싸면
  스플리터 조절이 망가진다. 잘리는 **하위 패널만** 감싼다.

### 스타일

다크 테마 문자열을 각 파일에 복붙하지 않는다. `src/ui/theme.py` 의
`MAIN_WINDOW_STYLE` / `DARK_DIALOG_STYLE` 을 쓴다.

### 예외 처리

`except Exception` 으로 삼킬 때는 `logger.debug(..., exc_info=True)` 를
남기는 것이 이 코드베이스의 관례다.

## 주요 경로

- `src/services/detection_service.py` — GUI·CLI·Colab 공통 탐지 서비스
- `src/pipeline/` — 탐지, 추적, 병합, 가상 이벤트, 카운팅
- `src/db/` — SQLite 스키마와 저장기
- `src/ui/` — PySide6 GUI (`widgets.py` 에 공통 위젯·헬퍼)
- `config/lines/` — 현장별 분석라인 JSON
- `colab/` — Colab 노트북과 전용 설정

모델(`models/`), 결과 DB, Excel 파일은 용량이 크거나 현장 데이터일 수
있다. 커밋 전에 포함 범위를 확인할 것.
