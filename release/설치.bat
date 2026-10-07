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

echo ============================================
echo  Count Car 설치 (인터넷 연결 필요, 10~20분)
echo ============================================

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
where nvidia-smi > nul 2> nul
if %errorlevel%==0 (
  echo   NVIDIA GPU 를 찾았습니다. GPU 용 PyTorch 를 설치합니다.
  "%UV%" pip install --python "%PY%" torch==2.9.1 torchvision==0.24.1 --index-url https://download.pytorch.org/whl/cu126 || goto :fail
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
pause
exit /b 0

:fail
echo.
echo [설치 실패] 위의 오류 메시지를 확인하세요. 인터넷 연결을 확인한 뒤 설치.bat 을 다시 실행하면 이어서 설치합니다.
pause
exit /b 1
