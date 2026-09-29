@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

rem ============================================================================
rem  TimeSeries Studio 后端一键打包（PyInstaller one-file）
rem
rem  跑法：双击本文件，或在任意目录执行 444\build-backend.bat
rem  产出：timeseries-studio-server\release\timeseries-backend.exe
rem  打完自动跑一次冒烟测试：真的把 exe 拉起来打接口，跑不过就返回非 0
rem
rem  可选：set TSS_SKIP_SMOKE=1   只打包不冒烟
rem        set TSS_PY=<python>    指定解释器（默认用 .venv）
rem ============================================================================

set "ROOT=%~dp0"
set "SERVER=%ROOT%timeseries-studio-server"
set "PACK=%SERVER%\packaging"
set "RELEASE=%SERVER%\release"
set "EXE=%RELEASE%\timeseries-backend.exe"

echo.
echo [1/4] 定位 Python 解释器
set "PY=%TSS_PY%"
if not defined PY if exist "%SERVER%\.venv\Scripts\python.exe" set "PY=%SERVER%\.venv\Scripts\python.exe"
if not defined PY set "PY=python"
"%PY%" -V 2>nul || (echo    找不到可用的 Python，装一个 3.10+ 或设 TSS_PY 指向解释器 & exit /b 2)
echo    解释器：%PY%

echo.
echo [2/4] 检查 PyInstaller
"%PY%" -m PyInstaller --version >nul 2>nul
if errorlevel 1 (
    echo    未安装，正在 pip install pyinstaller...
    "%PY%" -m pip install --disable-pip-version-check pyinstaller || (echo    安装失败 & exit /b 2)
)
for /f "delims=" %%v in ('"%PY%" -m PyInstaller --version 2^>nul') do set "PYI_VER=%%v"
echo    PyInstaller %PYI_VER%

echo.
echo [3/4] 打包 one-file exe
if exist "%RELEASE%" rmdir /s /q "%RELEASE%"
if exist "%SERVER%\build" rmdir /s /q "%SERVER%\build"
set "T0=%TIME%"
"%PY%" -m PyInstaller --noconfirm --clean ^
    --distpath "%RELEASE%" ^
    --workpath "%SERVER%\build\pyi" ^
    "%PACK%\timeseries-backend.spec"
if not exist "%EXE%" (echo    没打出 %EXE% & exit /b 1)
echo    产物：%EXE%

echo.
if defined TSS_SKIP_SMOKE (
    echo [4/4] 已设 TSS_SKIP_SMOKE，跳过冒烟测试
    goto :report
)

echo [4/4] 冒烟测试（拉起 exe 打真实接口）
"%PY%" -X utf8 "%PACK%\smoke_test.py" --exe "%EXE%"
set "SMOKE=%ERRORLEVEL%"
if not "%SMOKE%"=="0" (
    echo.
    echo 冒烟未通过（exit %SMOKE%），产物保留在 %EXE% 供排查
    exit /b 1
)

:report
echo.
echo ============================================================
for %%f in ("%EXE%") do echo  产物  timeseries-backend.exe   %%~zf 字节
echo  用时  %T0% - %TIME%
echo  前端已认这个 exe：Electron 启动时优先跑它，没有则回落到 python -m uvicorn
echo ============================================================
endlocal
