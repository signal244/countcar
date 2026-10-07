@echo off
chcp 65001 > nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo 먼저 설치.bat 을 실행하세요.
  pause
  exit /b 1
)
echo 샘플 영상으로 이 PC 의 처리 속도와 정확도를 확인합니다.
echo 다른 모델이나 추적 설정으로 비교하려면 설치_및_사용법.md 의 "5. 샘플로 성능 확인" 을 보세요.
echo.
".venv\Scripts\python.exe" scripts\check_sample.py %*
echo.
pause
