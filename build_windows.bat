@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul

echo ============================================================
echo  win-rec Windows 打包脚本
echo ============================================================
echo.

:: ---- 检查 Python ----
python --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 找不到 Python，请先安装 Python 3.11 或 3.12
    echo   下载地址：https://www.python.org/downloads/
    pause & exit /b 1
)

:: ---- 查找 ffmpeg：优先用项目本地的 tools\ffmpeg\，其次用 PATH ----
:: 推荐做法：把静态编译版 ffmpeg.exe/ffprobe.exe 放在 tools\ffmpeg\ 目录下
:: 下载地址：https://www.gyan.dev/ffmpeg/builds/ → ffmpeg-release-full.zip
if exist "tools\ffmpeg\ffmpeg.exe" (
    set FFMPEG_PATH=%~dp0tools\ffmpeg\ffmpeg.exe
    set FFPROBE_PATH=%~dp0tools\ffmpeg\ffprobe.exe
    echo [OK] 使用本地 tools\ffmpeg\ffmpeg.exe
) else (
    where ffmpeg >nul 2>&1
    if errorlevel 1 (
        echo [错误] 找不到 ffmpeg！
        echo.
        echo   推荐做法：
        echo     1. 下载 https://www.gyan.dev/ffmpeg/builds/ 中的 ffmpeg-release-full.zip
        echo     2. 解压后将 ffmpeg.exe 和 ffprobe.exe 复制到本项目的 tools\ffmpeg\ 目录
        echo     3. 重新运行此脚本
        pause & exit /b 1
    )
    for /f "tokens=*" %%i in ('where ffmpeg') do set FFMPEG_PATH=%%i
    for /f "tokens=*" %%i in ('where ffprobe') do set FFPROBE_PATH=%%i
    echo [OK] 使用系统 PATH 中的 ffmpeg: %FFMPEG_PATH%
)

:: ---- 验证 ffmpeg 文件大小（必须大于 10MB，排除 shim/包装器）----
for %%F in ("%FFMPEG_PATH%") do set FFMPEG_SIZE=%%~zF
if %FFMPEG_SIZE% LSS 10485760 (
    echo [错误] ffmpeg.exe 文件过小（%FFMPEG_SIZE% 字节），可能是 shim 或包装器，不是真正的静态编译版。
    echo   请下载 https://www.gyan.dev/ffmpeg/builds/ 中的 ffmpeg-release-full.zip
    echo   解压后将 ffmpeg.exe 和 ffprobe.exe 放入 tools\ffmpeg\ 目录
    pause & exit /b 1
)
echo [OK] ffmpeg 大小验证通过（%FFMPEG_SIZE% 字节）

:: ---- 验证 ffmpeg 支持 dshow（Windows 录音必须）----
"%FFMPEG_PATH%" -devices 2>&1 | findstr /i "dshow" >nul
if errorlevel 1 (
    echo [错误] 此 ffmpeg 不支持 dshow，无法在 Windows 上录音。
    echo   请下载 full 版本：https://www.gyan.dev/ffmpeg/builds/ → ffmpeg-release-full.zip
    pause & exit /b 1
)
echo [OK] ffmpeg 支持 dshow

for %%i in ("%FFMPEG_PATH%") do set FFMPEG_DIR=%%~dpi
set FFMPEG_DIR=%FFMPEG_DIR:~0,-1%
echo [INFO] ffmpeg 目录: %FFMPEG_DIR%

:: ---- 创建虚拟环境 ----
if not exist "venv_build" (
    echo [1/5] 创建构建虚拟环境...
    python -m venv venv_build
) else (
    echo [1/5] 复用已有构建虚拟环境
)

call venv_build\Scripts\activate.bat

:: ---- 安装依赖 ----
echo [2/5] 安装项目依赖...
pip install -e . --quiet
if errorlevel 1 ( echo [错误] 依赖安装失败 & pause & exit /b 1 )

echo [3/5] 安装 PyInstaller...
pip install pyinstaller --quiet
if errorlevel 1 ( echo [错误] PyInstaller 安装失败 & pause & exit /b 1 )

:: ---- PyInstaller 打包主程序（onedir）----
:: ffmpeg 打包到根目录（.），PyInstaller onedir 会把它放进 _internal\
:: config.py 的 _bundled_bin() 会依次查找根目录和 _internal\ 两个位置
echo [4/5] 打包主程序 win-rec.exe ...
pyinstaller ^
    --onedir ^
    --name win-rec ^
    --console ^
    --noconfirm ^
    --add-binary "%FFMPEG_DIR%\ffmpeg.exe;." ^
    --add-binary "%FFMPEG_DIR%\ffprobe.exe;." ^
    --add-data "win_rec;win_rec" ^
    --hidden-import win_rec.cli ^
    --hidden-import win_rec.recorder ^
    --hidden-import win_rec.recorder_win ^
    --hidden-import win_rec.transcribe ^
    --hidden-import win_rec.summarize ^
    --hidden-import win_rec.refine ^
    --hidden-import win_rec.store ^
    --hidden-import win_rec.config ^
    --hidden-import win_rec.glossary ^
    --hidden-import win_rec.diarize ^
    --hidden-import typer ^
    --hidden-import rich ^
    --hidden-import faster_whisper ^
    --hidden-import ctranslate2 ^
    --hidden-import anthropic ^
    --hidden-import openai ^
    --hidden-import filelock ^
    --hidden-import send2trash ^
    --hidden-import pydub ^
    --hidden-import mutagen ^
    --hidden-import yaml ^
    --collect-all faster_whisper ^
    --collect-all ctranslate2 ^
    --collect-all silero_vad ^
    main.py 2>build_main.log
if errorlevel 1 (
    echo [错误] 主程序打包失败，查看 build_main.log
    pause & exit /b 1
)

:: ---- PyInstaller 打包子进程 recorder_win（onefile，同时带入 ffmpeg）----
:: recorder 是独立进程，需要自带 ffmpeg，打包为 onefile
echo [4b/5] 打包子进程 win-rec-recorder.exe ...
pyinstaller ^
    --onefile ^
    --name win-rec-recorder ^
    --console ^
    --noconfirm ^
    --add-binary "%FFMPEG_DIR%\ffmpeg.exe;." ^
    --add-binary "%FFMPEG_DIR%\ffprobe.exe;." ^
    win_rec\recorder_win.py 2>build_recorder.log
if errorlevel 1 (
    echo [错误] recorder 打包失败，查看 build_recorder.log
    pause & exit /b 1
)

:: ---- PyInstaller 打包托盘程序（onefile，noconsole）----
echo [4c/5] 打包托盘程序 tray.exe ...
pyinstaller ^
    --onefile ^
    --name tray ^
    --noconsole ^
    --noconfirm ^
    --hidden-import win_rec.tray ^
    --hidden-import win_rec.config ^
    --hidden-import win_rec.store ^
    --hidden-import pystray ^
    --hidden-import PIL ^
    tray_main.py 2>build_tray.log
if errorlevel 1 (
    echo [错误] tray 打包失败，查看 build_tray.log
    pause & exit /b 1
)

:: ---- 整理输出目录 ----
echo [5/5] 整理输出目录...
copy /y dist\win-rec-recorder.exe dist\win-rec\win-rec-recorder.exe >nul
copy /y dist\tray.exe dist\win-rec\tray.exe >nul

:: ---- 验证 ffmpeg 确实打包进了 _internal\ ----
if not exist "dist\win-rec\_internal\ffmpeg.exe" (
    echo [警告] dist\win-rec\_internal\ffmpeg.exe 不存在，ffmpeg 可能未正确打包！
) else (
    echo [OK] ffmpeg 已打包到 dist\win-rec\_internal\ffmpeg.exe
)
if not exist "dist\win-rec\_internal\ffprobe.exe" (
    echo [警告] dist\win-rec\_internal\ffprobe.exe 不存在，ffprobe 可能未正确打包！
) else (
    echo [OK] ffprobe 已打包到 dist\win-rec\_internal\ffprobe.exe
)

:: ---- 复制使用手册 ----
if exist "使用手册.html" (
    copy /y "使用手册.html" "dist\win-rec\使用手册.html" >nul
    echo [OK] 使用手册已复制到 dist\win-rec\
) else (
    echo [警告] 未找到使用手册.html，跳过
)

:: ---- 完成 ----
echo.
echo ============================================================
echo  打包完成！输出目录：dist\win-rec\
echo ============================================================
echo.
echo  dist\win-rec\ 目录包含：
echo    win-rec.exe              主程序
echo    win-rec-recorder.exe     录音子程序
echo    tray.exe                 系统托盘程序
echo    _internal\ffmpeg.exe     音频工具（已内置）
echo    _internal\ffprobe.exe    音频分析工具（已内置）
echo    使用手册.html             用户手册（双击在浏览器打开）
echo.
echo  分发给用户时，将整个 dist\win-rec\ 文件夹压缩成 zip 即可。
echo  用户解压后，在文件夹内打开 cmd，运行 win-rec.exe start
echo.

call venv_build\Scripts\deactivate.bat
pause
