# -*- coding: utf-8 -*-
"""VS Code에서 이 파일을 열고 ▶(Run) 버튼만 누르면 같은 폴더에 중복파일정리기.exe 를 만들어 줍니다."""
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)


def run(step, args):
    print(f"\n{step}")
    r = subprocess.run([sys.executable, "-m"] + args)
    if r.returncode != 0:
        print("\n[오류] 위 메시지를 캡처해서 보내주세요.")
        sys.exit(1)


print("=" * 44)
print("  중복파일 정리기 exe 만들기")
print("=" * 44)
run("[1/3] PyInstaller, 미리보기 라이브러리 설치 중...",
    ["pip", "install", "--upgrade", "pyinstaller", "pillow"])
run("[2/3] exe 만드는 중... (1~2분 걸려요)",
    ["PyInstaller", "--onefile", "--windowed", "--clean", "--noconfirm",
     "--add-data", f"fonts{os.pathsep}fonts", "--name", "중복파일정리기", "중복파일정리기.py"])
print("\n[3/3] 정리 중...") 
target = "중복파일정리기.exe"
try:
    shutil.copy2(os.path.join("dist", "중복파일정리기.exe"), target)
except PermissionError:                 # 예전 exe가 실행 중이면 덮어쓸 수 없음
    target = "중복파일정리기_새버전.exe"
    shutil.copy2(os.path.join("dist", "중복파일정리기.exe"), target)
    print("\n(예전 중복파일정리기.exe가 실행 중이라 새 파일 이름으로 저장했어요)")
shutil.rmtree("build", ignore_errors=True)
shutil.rmtree("dist", ignore_errors=True)
if os.path.exists("중복파일정리기.spec"):
    os.remove("중복파일정리기.spec")
print(f"\n완료! 이 폴더에 '{target}' 가 생겼어요.")
