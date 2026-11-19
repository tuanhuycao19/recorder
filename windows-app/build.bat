@echo off
REM Đóng gói WifiRecorder.exe (cần Python 3.10+ đã cài trên Windows)
cd /d %~dp0
python -m pip install -r requirements.txt pyinstaller || exit /b 1
pyinstaller --noconfirm --onefile --windowed --collect-all imageio_ffmpeg --name WifiRecorder main.py || exit /b 1
echo.
echo Xong: %~dp0dist\WifiRecorder.exe
