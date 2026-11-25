@echo off
cd /d "%~dp0"
echo ============================================
echo   중복파일 정리기 실행파일(exe) 만들기
echo ============================================
python --version >nul 2>&1
if errorlevel 1 (
  echo [오류] 파이썬이 없습니다. https://www.python.org 에서 설치하세요.
  echo        설치 첫 화면에서 "Add python.exe to PATH" 를 꼭 체크하세요.
  pause
  exit /b
)
echo [1/3] PyInstaller, 미리보기 라이브러리 설치 중...
python -m pip install --upgrade pyinstaller pillow
echo [2/3] exe 만드는 중... (1~2분 걸려요)
python -m PyInstaller --onefile --windowed --clean --add-data "fonts;fonts" --name "중복파일정리기" "중복파일정리기.py"
if errorlevel 1 (
  echo [오류] 빌드에 실패했어요. 위 메시지를 캡처해서 보내주세요.
  pause
  exit /b
)
echo [3/3] 정리 중...
copy /y "dist\중복파일정리기.exe" "중복파일정리기.exe" >nul
rmdir /s /q build >nul 2>&1
rmdir /s /q dist >nul 2>&1
del /q "중복파일정리기.spec" >nul 2>&1
echo.
echo 완료! 이 폴더에 "중복파일정리기.exe" 가 생겼어요.
echo 이 exe 파일 하나만 있으면 파이썬 없는 PC에서도 실행됩니다.
pause
