@echo off
cd /d "%~dp0"
python --version >nul 2>&1
if errorlevel 1 (
  echo 파이썬이 없습니다. https://www.python.org 에서 설치하세요.
  echo 설치할 때 "Add python.exe to PATH" 를 꼭 체크하세요.
  pause
  exit /b
)
rem 사진/영상 미리보기용 Pillow - 처음 한 번만 설치됨 (없어도 실행은 됨)
python -c "import PIL" >nul 2>&1 || python -m pip install -q pillow
start "" pythonw "중복파일정리기.py"
