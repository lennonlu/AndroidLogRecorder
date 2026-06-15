@echo off
chcp 65001 >nul
title Android 日志采集 & 录屏工具
echo.
echo ============================================
echo   Android 自动日志采集 ^& 录屏工具
echo ============================================
echo.
echo 使用方法：
echo   1. 手机插上 USB 数据线，开启 USB 调试
echo   2. 手机弹窗点击"允许调试"
echo   3. 本工具自动开始采集日志和录屏
echo   4. 按 Ctrl+C 停止采集
echo.
echo 输出目录：captures\session_型号_日期_时间\
echo.
pause
if exist "%~dp0AndroidLogger.exe" (
    "%~dp0AndroidLogger.exe"
) else (
    python "%~dp0android_logger.py"
)
pause
