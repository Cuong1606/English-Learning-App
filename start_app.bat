@echo off
setlocal
cd /d "%~dp0"

where pyw.exe >nul 2>&1
if not errorlevel 1 (
  pyw.exe -3 "%~dp0launcher.pyw"
  if not errorlevel 1 exit /b 0
)

where pythonw.exe >nul 2>&1
if not errorlevel 1 (
  pythonw.exe "%~dp0launcher.pyw"
  if not errorlevel 1 exit /b 0
)

where py.exe >nul 2>&1
if not errorlevel 1 (
  py.exe -3 "%~dp0launcher.pyw"
  if not errorlevel 1 exit /b 0
)

where python.exe >nul 2>&1
if not errorlevel 1 (
  python.exe "%~dp0launcher.pyw"
  if not errorlevel 1 exit /b 0
)

echo.
echo KHONG MO DUOC APP BANG PYTHON 3.
echo Hay chay "Kiem tra loi.bat" de xem chi tiet.
echo Neu chua cai Python: cai Python 3 va chon "Add Python to PATH".
echo.
pause
exit /b 10
