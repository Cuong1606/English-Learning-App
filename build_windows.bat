@echo off
setlocal EnableExtensions
cd /d "%~dp0"

title Build English Learning App

echo ============================================================
echo  BUILD ENGLISH LEARNING APP - WINDOWS 64 BIT
echo ============================================================
echo.

if not exist "desktop_app.py" goto missing
if not exist "server.py" goto missing
if not exist "web\index.html" goto missing
if not exist "data\content.sqlite" goto missing
if not exist "audio" goto missing
if not exist "course_audio" goto missing

set "PY_CMD="
py -3 --version >nul 2>nul
if not errorlevel 1 set "PY_CMD=py -3"
if not defined PY_CMD (
    python --version >nul 2>nul
    if not errorlevel 1 set "PY_CMD=python"
)
if not defined PY_CMD goto nopython

echo [1/4] Tao moi truong build...
if not exist ".build_env\Scripts\python.exe" (
    %PY_CMD% -m venv .build_env
    if errorlevel 1 goto failed
)
call ".build_env\Scripts\activate.bat"

echo [2/4] Cai PyInstaller + pywebview...
python -m pip install -r requirements-build.txt
if errorlevel 1 goto failed

echo [3/4] Don build cu...
if exist build rmdir /s /q build
if exist "dist\English Learning App" rmdir /s /q "dist\English Learning App"

echo [4/4] Dong goi app. Buoc nay co the mat vai phut...
python -m PyInstaller --noconfirm --clean EnglishLearningApp.spec
if errorlevel 1 goto failed

echo.
echo ============================================================
echo  HOAN TAT
echo  File chay:
echo  dist\English Learning App\English Learning App.exe
echo ============================================================
echo.
echo Hay mo file EXE tren de test truoc khi phat hanh.
pause
exit /b 0

:missing
echo LOI: Thu muc build khong du file app/data/audio.
echo Hay chep bo dong goi nay vao thu muc English Learning App V2.4.3 day du.
pause
exit /b 2

:nopython
echo LOI: Khong tim thay Python tren Windows.
echo Cai Python 3.13 64-bit, sau do chay lai build_windows.bat.
pause
exit /b 3

:failed
echo.
echo BUILD THAT BAI. Hay chup man hinh loi gui lai cho ChatGPT.
pause
exit /b 1
