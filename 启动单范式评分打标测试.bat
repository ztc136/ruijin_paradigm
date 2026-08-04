@echo off
chcp 65001 >nul
cd /d "%~dp0"

set "EMOTION_PSYCHOPY_PY=C:\Program Files\PsychoPy\python.exe"
if not exist "%EMOTION_PSYCHOPY_PY%" set "EMOTION_PSYCHOPY_PY=python"

echo ============================================================
echo 单范式评分打标测试：范式2、2个trial、窗口模式、串口marker
echo ============================================================
echo.
echo 正在自动识别打标盒串口并启动PsychoPy，请稍候……
echo 评分页面出现后再将示波器设为Single；空格/回车确认时发送评分marker 41-49。
echo.
"%EMOTION_PSYCHOPY_PY%" -B emotion_paradigm.py ^
    --no-gui --windowed --paradigm 2 --test ^
    --participant SCORETEST --session 1 ^
    --marker serial --endpoint auto --seed 20260804 ^
    2> "%~dp0评分打标测试错误.log"

if errorlevel 1 (
    echo.
    echo 测试运行失败，请检查COM口是否正确、是否被其他程序占用。
    echo 详细错误保存在：%~dp0评分打标测试错误.log
    pause
    exit /b 1
)

echo.
echo 测试结束。请核对示波器波形及data目录中的SCORETEST事件日志。
pause
