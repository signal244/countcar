@echo off
chcp 65001 > nul
setlocal
cd /d "%~dp0"

rem Python 과 패키지를 이 폴더 안(tools, .venv)에 설치한다. 시스템 Python 은 건드리지 않는다.
set "UV=%~dp0tools\uv.exe"
set "UV_LINK_MODE=copy"
set "UV_PYTHON_INSTALL_DIR=%~dp0tools\python"
set "UV_CACHE_DIR=%~dp0tools\uv-cache"
set "PY=%~dp0.venv\Scripts\python.exe"
rem /auto: 설치 프로그램(exe)이 실행할 때. 성공하면 키 입력을 기다리지 않는다(실패 시에는 오류를 읽도록 멈춘다).
set "AUTO="
if /i "%~1"=="/auto" set "AUTO=1"

echo ============================================
echo  Count Car 설치 (인터넷 연결 필요, 10~20분)
echo ============================================

rem 동기화 폴더에서는 링크(junction)를 만들 수 없어 Python 설치가 실패하고 DB 도 꼬일 수 있다.
powershell -NoProfile -Command "if ('%~dp0' -match '내 드라이브|My Drive|Google Drive|GoogleDrive|OneDrive') { exit 1 }"
if %errorlevel%==1 (
  echo.
  echo [중단] 지금 위치는 Google Drive 또는 OneDrive 동기화 폴더입니다:
  echo   %~dp0
  echo   이 폴더째 C:\CountCar 같은 로컬 폴더로 옮기거나 압축을 다시 푼 뒤, 그곳에서 설치.bat 을 실행하세요.
  goto :fail
)

if not exist "%UV%" (
  echo [오류] tools\uv.exe 가 없습니다. 배포판 압축을 다시 풀어 주세요.
  goto :fail
)

echo.
echo [1/4] Python 3.10 준비...
"%UV%" python install 3.10 || goto :fail

echo.
echo [2/4] 가상환경 만들기 (.venv)...
"%UV%" venv .venv --python 3.10 --allow-existing || goto :fail

echo.
echo [3/4] 패키지 설치...
set "HAS_NVIDIA="
where nvidia-smi > nul 2> nul && set "HAS_NVIDIA=1"
rem 32비트 프로그램에서 실행되면 System32 가 SysWOW64 로 바뀌어 보여 nvidia-smi 를 못 찾는다.
if exist "%SystemRoot%\Sysnative\nvidia-smi.exe" set "HAS_NVIDIA=1"
if defined HAS_NVIDIA (
  echo   NVIDIA GPU 를 찾았습니다. GPU 용 PyTorch 를 설치합니다.
  rem +cu126 까지 지정해야 이미 깔린 CPU 용 torch 2.9.1 을 GPU 용으로 바꾼다(번호만 쓰면 같은 버전으로 보고 건너뛴다).
  "%UV%" pip install --python "%PY%" torch==2.9.1+cu126 torchvision==0.24.1+cu126 --index-url https://download.pytorch.org/whl/cu126 || goto :fail
) else (
  echo   NVIDIA GPU 가 없습니다. CPU 용으로 설치합니다.
)
"%UV%" pip install --python "%PY%" -r requirements.txt -c constraints-windows-py310.txt || goto :fail

echo.
echo [4/4] 설치 확인...
"%PY%" -c "import torch, ultralytics, openvino, PySide6; print('  ultralytics', ultralytics.__version__, '/ openvino', openvino.__version__); print('  GPU 사용 가능:', torch.cuda.is_available())" || goto :fail

echo.
echo ============================================
echo  설치 완료. 실행.bat 으로 프로그램을 시작하세요.
echo ============================================
if not defined AUTO pause
exit /b 0

:fail
echo.
echo [설치 실패] 위의 오류 메시지를 확인하세요. 인터넷 연결을 확인한 뒤 설치.bat 을 다시 실행하면 이어서 설치합니다.
pause
exit /b 1
