@echo off
chcp 65001 >nul
title Android 仅日志采集（不录屏）
echo.
echo ============================================
echo   仅采集 Logcat 日志（不录屏）
echo ============================================
echo.
pause
if exist "%~dp0AndroidLogger.exe" (
    "%~dp0AndroidLogger.exe" --no-record
) else (
    python "%~dp0android_logger.py" --no-record
)
pause
