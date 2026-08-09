@echo off
cd /d "%~dp0"
echo ============================================================
echo  ENGLISH LEARNING APP - KIEM TRA LOI
echo ============================================================
echo.
echo Thu muc app:
echo %CD%
echo.
echo Kiem tra Python...
where py.exe >nul 2>&1
if %errorlevel%==0 (
  py.exe -3 --version
  echo.
  echo Dang chay server de hien loi truc tiep...
  echo Neu thay dong "Mo: http://127.0.0.1:8767" thi server van tot.
  echo Nhan Ctrl+C de dung.
  echo.
  py.exe -3 server.py --port 8767
  pause
  exit /b
)
where python.exe >nul 2>&1
if %errorlevel%==0 (
  python.exe --version
  echo.
  echo Dang chay server de hien loi truc tiep...
  echo Neu thay dong "Mo: http://127.0.0.1:8767" thi server van tot.
  echo Nhan Ctrl+C de dung.
  echo.
  python.exe server.py --port 8767
  pause
  exit /b
)
echo KHONG TIM THAY PYTHON 3.
echo Cai Python 3 va bat "Add Python to PATH".
pause
